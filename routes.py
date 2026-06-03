"""HTTP-API модуля Sales. Монтируется ядром под префиксом ``/sales``."""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from core.domain.models import Approval, Sku
from core.runtime.core import Core
from core.runtime.deps import get_core, get_session
from core.services.approvals import ApprovalOut, ApprovalRequest
from core.services.auth import require_permission
from modules.sales.ai import draft_reply, next_step, summarize
from modules.sales.models import (
    Activity,
    Deal,
    DealDocument,
    DealItem,
    KpiTarget,
    Message,
    PriceQuote,
)
from modules.sales.repository import DealRepository
from modules.sales.schemas import (
    ActivityCreate,
    AiAssistRequest,
    AiDraftOut,
    AiTextOut,
    BoardOut,
    DealCreate,
    DealDetailOut,
    DealItemCreate,
    DealItemOut,
    DealItemUpdate,
    DealRead,
    DealUpdate,
    DocumentCreate,
    DocumentDecision,
    DocumentOut,
    KpiOut,
    MessageCreate,
    MessageOut,
    PriceInfo,
    PriceQuoteCreate,
    SkuOut,
    StageBoard,
)
from modules.sales.stages import STAGES

router = APIRouter(tags=["sales"])

# Префикс номера и человекочитаемое название документа по типу.
DOC_NUMBER_PREFIX = {"invoice": "СЧ", "contract": "ДГ", "order": "ЗК"}
DOC_TITLES = {"invoice": "Счёт", "contract": "Договор", "order": "Заказ"}
# Типы документов, требующие согласования до записи в 1С (договор → юрист, ч.4).
REQUIRES_APPROVAL = {"contract"}
# Типы документов, резервирующие складские остатки при проведении (заказ).
RESERVES_STOCK = {"order"}
# План/факт по периодам (sales-34): окно факта (дней) и множитель плана (рабочих дней).
PERIOD_DAYS = {"day": 1, "week": 7, "month": 30, "quarter": 90, "year": 365}
PERIOD_MULT = {"day": 1, "week": 5, "month": 22, "quarter": 65, "year": 250}


def _utcnow() -> datetime:
    # наивный UTC — единообразно для SQLite и PostgreSQL
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def _deal_stock_items(session: AsyncSession, deal_id: int) -> list[dict]:
    """Позиции сделки как ``[{sku_code, qty}]`` (для резервирования остатков в 1С)."""
    rows = (
        await session.execute(select(DealItem).where(DealItem.deal_id == deal_id))
    ).scalars().all()
    if not rows:
        return []
    skus = {
        s.id: s
        for s in (
            await session.execute(select(Sku).where(Sku.id.in_([r.sku_id for r in rows])))
        ).scalars().all()
    }
    return [{"sku_code": skus[r.sku_id].code, "qty": float(r.qty)} for r in rows if r.sku_id in skus]


async def _price_summary(session: AsyncSession, sku_code: str, counterparty: str = "") -> PriceInfo:
    """Сводка цен по SKU (для клиента, если задан): последняя и минимальная цена."""
    query = select(PriceQuote.price).where(PriceQuote.sku_code == sku_code)
    if counterparty:
        query = query.where(PriceQuote.counterparty == counterparty)
    prices = [
        float(p) for p in (await session.execute(query.order_by(PriceQuote.id))).scalars().all()
    ]
    if not prices:
        return PriceInfo(sku_code=sku_code)
    return PriceInfo(sku_code=sku_code, last_price=prices[-1], min_price=min(prices), count=len(prices))


async def _build_item_out(session: AsyncSession, item: DealItem, counterparty: str) -> DealItemOut:
    """Представление позиции с данными SKU и ценами клиенту (Price Engine)."""
    sku = await session.get(Sku, item.sku_id)
    price = await _price_summary(session, sku.code if sku else "", counterparty)
    return DealItemOut(
        id=item.id,
        sku_id=item.sku_id,
        code=sku.code if sku else "",
        title=sku.title if sku else "",
        unit=sku.unit if sku else "",
        qty=float(item.qty),
        last_price=price.last_price,
        min_price=price.min_price,
    )


async def _post_document_to_1c(
    core: Core, session: AsyncSession, doc: DealDocument, counterparty: str
) -> None:
    """Записать документ в 1С через фасад ядра и пометить проведённым (posted).

    Событие ``sales.document.posted`` уходит в шину (→ audit). Используется и при
    мгновенной записи счёта, и при проведении договора после согласования.
    """
    result = await core.services.onec.post_document(
        doc.kind,
        {"number": doc.number, "counterparty": counterparty, "amount": float(doc.amount)},
    )
    doc.onec_ref = result.get("ref")
    doc.status = "posted"
    doc.posted_at = _utcnow()
    core.event_bus.emit(
        session,
        "sales.document.posted",
        {
            "document_id": doc.id,
            "deal_id": doc.deal_id,
            "kind": doc.kind,
            "number": doc.number,
            "onec_ref": doc.onec_ref,
            "entity_ref": f"deal:{doc.deal_id}",
        },
    )


@router.get("/ping")
async def ping() -> dict:
    """Проверка, что модуль смонтирован."""
    return {"module": "sales", "status": "ok"}


@router.get("/board", response_model=BoardOut)
async def board(session: AsyncSession = Depends(get_session)) -> BoardOut:
    """Доска сделок: сделки сгруппированы по стадиям воронки с агрегатами."""
    deals = await DealRepository(session).list()
    by_stage: dict[str, list[Deal]] = defaultdict(list)
    for deal in deals:
        by_stage[deal.stage].append(deal)

    stages = [
        StageBoard(
            id=s["id"],
            title=s["title"],
            color=s["color"],
            count=len(by_stage.get(s["id"], [])),
            sum=float(sum(d.amount for d in by_stage.get(s["id"], []))),
            deals=[DealRead.model_validate(d) for d in by_stage.get(s["id"], [])],
        )
        for s in STAGES
    ]
    return BoardOut(stages=stages)


@router.get("/kpis", response_model=list[KpiOut])
async def kpis(period: str = "day", session: AsyncSession = Depends(get_session)):
    """Показатели «План/Факт» за период (день/неделя/месяц/квартал/год, sales-34).

    Факт — сумма активностей за окно периода (от последней даты назад); план —
    дневная цель, масштабированная на число рабочих дней периода.
    """
    targets = (
        await session.execute(select(KpiTarget).order_by(KpiTarget.sort_order))
    ).scalars().all()

    latest = (await session.execute(select(func.max(Activity.date)))).scalar()
    actuals: dict[str, float] = {}
    if latest is not None:
        start = latest - timedelta(days=PERIOD_DAYS.get(period, 1) - 1)
        rows = await session.execute(
            select(Activity.kpi_key, func.coalesce(func.sum(Activity.value), 0))
            .where(Activity.date >= start, Activity.date <= latest)
            .group_by(Activity.kpi_key)
        )
        actuals = {key: float(total) for key, total in rows.all()}

    mult = PERIOD_MULT.get(period, 1)
    result: list[KpiOut] = []
    for t in targets:
        actual = actuals.get(t.key, 0.0)
        target = float(t.target) * mult
        percent = round(min(100.0, actual / target * 100)) if target else 0
        result.append(
            KpiOut(
                key=t.key,
                title=t.title,
                target=target,
                actual=actual,
                percent=percent,
                unit=t.unit,
                icon=t.icon,
                tone=t.tone,
            )
        )
    return result


@router.post("/activities", status_code=201)
async def create_activity(payload: ActivityCreate, session: AsyncSession = Depends(get_session)):
    """Отметить активность. Без даты — добавляется в текущий отчётный день."""
    day = payload.date
    if day is None:
        day = (await session.execute(select(func.max(Activity.date)))).scalar() or date.today()
    session.add(
        Activity(
            kpi_key=payload.kpi_key,
            owner=payload.owner,
            value=Decimal(str(payload.value)),
            date=day,
        )
    )
    await session.commit()
    return {"ok": True, "date": str(day)}


@router.get("/deals", response_model=list[DealRead])
async def list_deals(session: AsyncSession = Depends(get_session)):
    """Плоский список сделок."""
    return await DealRepository(session).list()


@router.get("/deals/{deal_id}", response_model=DealDetailOut)
async def get_deal(deal_id: int, session: AsyncSession = Depends(get_session)):
    """Одна сделка по id с позициями номенклатуры (со связью к SKU)."""
    deal = await DealRepository(session).get(deal_id)
    if deal is None:
        raise HTTPException(status_code=404, detail="Сделка не найдена")

    rows = (
        await session.execute(select(DealItem).where(DealItem.deal_id == deal_id))
    ).scalars().all()
    skus: dict[int, Sku] = {}
    if rows:
        sku_ids = [r.sku_id for r in rows]
        skus = {
            s.id: s
            for s in (
                await session.execute(select(Sku).where(Sku.id.in_(sku_ids)))
            ).scalars().all()
        }
    # цены клиенту по позициям (Price Engine): последняя и минимальная
    price_map: dict[str, tuple[float, float]] = {}
    codes = [skus[r.sku_id].code for r in rows if r.sku_id in skus]
    if codes:
        quotes = (
            await session.execute(
                select(PriceQuote)
                .where(
                    PriceQuote.counterparty == deal.counterparty,
                    PriceQuote.sku_code.in_(codes),
                )
                .order_by(PriceQuote.id)
            )
        ).scalars().all()
        grouped: dict[str, list[float]] = defaultdict(list)
        for q in quotes:
            grouped[q.sku_code].append(float(q.price))
        price_map = {c: (v[-1], min(v)) for c, v in grouped.items()}

    items = [
        DealItemOut(
            id=r.id,
            sku_id=r.sku_id,
            code=skus[r.sku_id].code if r.sku_id in skus else "",
            title=skus[r.sku_id].title if r.sku_id in skus else "",
            unit=skus[r.sku_id].unit if r.sku_id in skus else "",
            qty=float(r.qty),
            last_price=price_map.get(skus[r.sku_id].code, (None, None))[0]
            if r.sku_id in skus
            else None,
            min_price=price_map.get(skus[r.sku_id].code, (None, None))[1]
            if r.sku_id in skus
            else None,
        )
        for r in rows
    ]

    docs = (
        await session.execute(
            select(DealDocument).where(DealDocument.deal_id == deal_id).order_by(DealDocument.id)
        )
    ).scalars().all()
    documents = [DocumentOut.model_validate(d) for d in docs]

    return DealDetailOut(
        **DealRead.model_validate(deal).model_dump(), items=items, documents=documents
    )


@router.patch("/deals/{deal_id}", response_model=DealRead)
async def update_deal(
    deal_id: int,
    payload: DealUpdate,
    session: AsyncSession = Depends(get_session),
):
    """Частично обновить сделку (например, сменить стадию при drag&drop)."""
    repo = DealRepository(session)
    deal = await repo.get(deal_id)
    if deal is None:
        raise HTTPException(status_code=404, detail="Сделка не найдена")
    await repo.update(deal, payload.model_dump(exclude_unset=True))
    await session.commit()
    return deal


@router.post("/deals", response_model=DealRead, status_code=201)
async def create_deal(
    payload: DealCreate,
    session: AsyncSession = Depends(get_session),
    core: Core = Depends(get_core),
):
    """Создать сделку и опубликовать доменное событие через шину ядра."""
    try:
        deal = await DealRepository(session).create(payload)
        core.event_bus.emit(session, "sales.deal.created", {"number": deal.number, "title": deal.title})
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(status_code=409, detail="Сделка с таким номером уже существует")
    return deal


@router.post("/deals/{deal_id}/request-approval", response_model=ApprovalOut, status_code=201)
async def request_approval(
    deal_id: int,
    payload: ApprovalRequest,
    core: Core = Depends(get_core),
    session: AsyncSession = Depends(get_session),
):
    """Отправить сделку на согласование (например, договор → юристу)."""
    deal = await DealRepository(session).get(deal_id)
    if deal is None:
        raise HTTPException(status_code=404, detail="Сделка не найдена")
    approval = await core.services.approvals.request(
        session,
        payload.kind,
        f"deal:{deal_id}",
        f"{deal.number} — {deal.counterparty}",
        payload.requested_by,
    )
    await session.commit()
    return approval


@router.get("/skus", response_model=list[SkuOut])
async def list_skus(session: AsyncSession = Depends(get_session)):
    """Справочник номенклатуры (для подбора позиций в сделку, sales-12)."""
    return (await session.execute(select(Sku).order_by(Sku.code))).scalars().all()


@router.get("/deals/{deal_id}/items", response_model=list[DealItemOut])
async def list_deal_items(deal_id: int, session: AsyncSession = Depends(get_session)):
    """Позиции номенклатуры сделки (с данными SKU и ценами клиенту)."""
    deal = await DealRepository(session).get(deal_id)
    counterparty = deal.counterparty if deal else ""
    rows = (
        await session.execute(
            select(DealItem).where(DealItem.deal_id == deal_id).order_by(DealItem.id)
        )
    ).scalars().all()
    return [await _build_item_out(session, r, counterparty) for r in rows]


@router.post("/deals/{deal_id}/items", response_model=DealItemOut, status_code=201)
async def add_deal_item(
    deal_id: int,
    payload: DealItemCreate,
    core: Core = Depends(get_core),
    session: AsyncSession = Depends(get_session),
):
    """Добавить позицию номенклатуры в сделку (подбор из SKU, sales-12)."""
    deal = await DealRepository(session).get(deal_id)
    if deal is None:
        raise HTTPException(status_code=404, detail="Сделка не найдена")
    if await session.get(Sku, payload.sku_id) is None:
        raise HTTPException(status_code=404, detail="Номенклатура не найдена")
    item = DealItem(deal_id=deal_id, sku_id=payload.sku_id, qty=Decimal(str(payload.qty)))
    session.add(item)
    await session.flush()
    core.event_bus.emit(
        session,
        "sales.item.changed",
        {"deal_id": deal_id, "action": "added", "entity_ref": f"deal:{deal_id}"},
    )
    await session.commit()
    return await _build_item_out(session, item, deal.counterparty)


@router.patch("/deal-items/{item_id}", response_model=DealItemOut)
async def update_deal_item(
    item_id: int,
    payload: DealItemUpdate,
    session: AsyncSession = Depends(get_session),
):
    """Изменить количество в позиции сделки."""
    item = await session.get(DealItem, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Позиция не найдена")
    item.qty = Decimal(str(payload.qty))
    deal = await DealRepository(session).get(item.deal_id)
    await session.commit()
    return await _build_item_out(session, item, deal.counterparty if deal else "")


@router.delete("/deal-items/{item_id}", status_code=204)
async def delete_deal_item(item_id: int, session: AsyncSession = Depends(get_session)):
    """Удалить позицию номенклатуры из сделки."""
    item = await session.get(DealItem, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Позиция не найдена")
    await session.delete(item)
    await session.commit()


@router.get("/deals/{deal_id}/documents", response_model=list[DocumentOut])
async def list_documents(deal_id: int, session: AsyncSession = Depends(get_session)):
    """Документы сделки (счета/договоры/заказы) с их состоянием и номерами в 1С."""
    return (
        await session.execute(
            select(DealDocument).where(DealDocument.deal_id == deal_id).order_by(DealDocument.id)
        )
    ).scalars().all()


@router.post("/deals/{deal_id}/documents", response_model=DocumentOut, status_code=201)
async def create_document(
    deal_id: int,
    payload: DocumentCreate,
    core: Core = Depends(get_core),
    session: AsyncSession = Depends(get_session),
):
    """Сформировать документ сделки (часть 9).

    Счёт/заказ пишутся в 1С сразу (``draft`` → ``posted``). Договор сначала
    уходит на согласование юристу (движок ч.4, маршрут ``deal.contract``) —
    статус ``pending_approval``; в 1С он записывается только после одобрения
    (``POST /sales/documents/{id}/decide``). Здесь ядро и CRM смыкаются в поток.
    """
    deal = await DealRepository(session).get(deal_id)
    if deal is None:
        raise HTTPException(status_code=404, detail="Сделка не найдена")
    if core.services.onec is None:
        raise HTTPException(status_code=503, detail="Интеграция 1С не подключена")

    prefix = DOC_NUMBER_PREFIX.get(payload.kind, "ДОК")
    number = f"{prefix}-{deal.number}"
    doc = DealDocument(deal_id=deal_id, kind=payload.kind, number=number, amount=deal.amount)
    session.add(doc)
    await session.flush()

    if payload.kind in REQUIRES_APPROVAL:
        # договор: на согласование юристу (ч.4); запись в 1С — после одобрения
        doc.status = "pending_approval"
        await core.services.approvals.request(
            session,
            "deal.contract",
            f"document:{doc.id}",
            f"{deal.number} — {DOC_TITLES.get(payload.kind, payload.kind)} ({deal.counterparty})",
            payload.requested_by,
        )
        core.event_bus.emit(
            session,
            "sales.document.created",
            {
                "document_id": doc.id,
                "deal_id": deal_id,
                "kind": payload.kind,
                "number": number,
                "entity_ref": f"deal:{deal_id}",
            },
        )
    else:
        # счёт/заказ: пишем в 1С сразу; заказ дополнительно резервирует остатки
        if payload.kind in RESERVES_STOCK and core.services.stock is not None:
            reserved = await core.services.stock.reserve(
                session, await _deal_stock_items(session, deal_id)
            )
            if reserved:
                core.event_bus.emit(
                    session,
                    "sales.stock.reserved",
                    {
                        "document_id": doc.id,
                        "deal_id": deal_id,
                        "items": reserved,
                        "entity_ref": f"deal:{deal_id}",
                    },
                )
        await _post_document_to_1c(core, session, doc, deal.counterparty)

    await session.commit()
    return doc


@router.post("/documents/{doc_id}/decide", response_model=DocumentOut)
async def decide_document(
    doc_id: int,
    payload: DocumentDecision,
    core: Core = Depends(get_core),
    session: AsyncSession = Depends(get_session),
    _: object = Depends(require_permission("sales.deal.approve")),
):
    """Решение по документу на согласовании (договор): провести в 1С или отклонить.

    Решает связанное согласование (движок ч.4) и, при одобрении, проводит документ
    в 1С (часть 9) — всё в одной транзакции. Согласование и проведение фиксируются
    событиями (→ audit log).
    """
    doc = await session.get(DealDocument, doc_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="Документ не найден")
    if doc.status != "pending_approval":
        raise HTTPException(status_code=409, detail="Документ не на согласовании")
    if core.services.onec is None:
        raise HTTPException(status_code=503, detail="Интеграция 1С не подключена")

    approval = (
        await session.execute(
            select(Approval).where(
                Approval.entity_ref == f"document:{doc_id}", Approval.status == "pending"
            )
        )
    ).scalars().first()
    if approval is not None:
        await core.services.approvals.decide(session, approval, payload.approved, payload.by)

    if payload.approved:
        deal = await DealRepository(session).get(doc.deal_id)
        await _post_document_to_1c(core, session, doc, deal.counterparty if deal else "")
    else:
        doc.status = "rejected"
        core.event_bus.emit(
            session,
            "sales.document.rejected",
            {
                "document_id": doc.id,
                "deal_id": doc.deal_id,
                "kind": doc.kind,
                "number": doc.number,
                "entity_ref": f"deal:{doc.deal_id}",
            },
        )

    await session.commit()
    return doc


@router.get("/deals/{deal_id}/messages", response_model=list[MessageOut])
async def list_messages(deal_id: int, session: AsyncSession = Depends(get_session)):
    """Омниканальная история переписки по сделке (часть 10)."""
    return (
        await session.execute(
            select(Message).where(Message.deal_id == deal_id).order_by(Message.id)
        )
    ).scalars().all()


@router.post("/deals/{deal_id}/messages", response_model=MessageOut, status_code=201)
async def create_message(
    deal_id: int,
    payload: MessageCreate,
    core: Core = Depends(get_core),
    session: AsyncSession = Depends(get_session),
):
    """Отправить/зафиксировать сообщение по сделке (канал + текст) — событие в шину."""
    deal = await DealRepository(session).get(deal_id)
    if deal is None:
        raise HTTPException(status_code=404, detail="Сделка не найдена")
    msg = Message(
        deal_id=deal_id,
        channel=payload.channel,
        direction=payload.direction,
        author=payload.author,
        text=payload.text,
    )
    session.add(msg)
    await session.flush()
    core.event_bus.emit(
        session,
        "sales.message.sent",
        {
            "message_id": msg.id,
            "deal_id": deal_id,
            "channel": payload.channel,
            "direction": payload.direction,
            "entity_ref": f"deal:{deal_id}",
        },
    )
    await session.commit()
    return msg


@router.get("/prices/{sku_code}", response_model=PriceInfo)
async def price_info(
    sku_code: str, counterparty: str = "", session: AsyncSession = Depends(get_session)
):
    """История цен по SKU → последняя и минимальная цена клиенту (Price Engine, sales-22)."""
    return await _price_summary(session, sku_code, counterparty)


@router.post("/prices", status_code=201)
async def create_price_quote(
    payload: PriceQuoteCreate,
    core: Core = Depends(get_core),
    session: AsyncSession = Depends(get_session),
):
    """Зафиксировать котировку цены SKU клиенту (пополняет историю Price Engine)."""
    session.add(
        PriceQuote(
            sku_code=payload.sku_code,
            counterparty=payload.counterparty,
            price=Decimal(str(payload.price)),
        )
    )
    core.event_bus.emit(
        session,
        "sales.price.quoted",
        {"sku_code": payload.sku_code, "counterparty": payload.counterparty, "price": payload.price},
    )
    await session.commit()
    return {"ok": True}


@router.post("/deals/{deal_id}/ai/draft-reply", response_model=AiDraftOut)
async def ai_draft_reply(
    deal_id: int,
    core: Core = Depends(get_core),
    session: AsyncSession = Depends(get_session),
):
    """AI-черновик ответа клиенту по истории переписки (AI-слой, Итерация 1).

    Под-фича модуля за feature-flag: при выключенном AI — 503. Генерация идёт
    через общий шлюз ``core.services.llm``; AI-действие фиксируется событием
    ``ai.draft.generated`` (→ audit, трассировка §3.3).
    """
    if not core.services.llm.enabled:
        raise HTTPException(status_code=503, detail="AI-слой выключен (feature-flag)")
    deal = await DealRepository(session).get(deal_id)
    if deal is None:
        raise HTTPException(status_code=404, detail="Сделка не найдена")

    messages = (
        await session.execute(
            select(Message).where(Message.deal_id == deal_id).order_by(Message.id)
        )
    ).scalars().all()
    text = await draft_reply(core.services.llm, deal, messages)
    model = core.services.llm.model or "mock"

    core.event_bus.emit(
        session,
        "ai.draft.generated",
        {"deal_id": deal_id, "model": model, "actor": "AI", "entity_ref": f"deal:{deal_id}"},
    )
    await session.commit()
    return AiDraftOut(text=text, model=model)


@router.post("/deals/{deal_id}/ai/assist", response_model=AiTextOut)
async def ai_assist(
    deal_id: int,
    payload: AiAssistRequest,
    core: Core = Depends(get_core),
    session: AsyncSession = Depends(get_session),
):
    """AI-ассистент сделки: резюме или следующий шаг (AI-слой, Итерация 1).

    Под-фича модуля за feature-flag (503 если AI выкл). Контекст сделки (позиции,
    документы, переписка) идёт в общий шлюз ``core.services.llm``; AI-действие
    фиксируется событием ``ai.<kind>.generated`` (→ audit, §3.3).
    """
    if not core.services.llm.enabled:
        raise HTTPException(status_code=503, detail="AI-слой выключен (feature-flag)")
    deal = await DealRepository(session).get(deal_id)
    if deal is None:
        raise HTTPException(status_code=404, detail="Сделка не найдена")

    async def _count(model, deal_col) -> int:
        return (
            await session.execute(select(func.count()).select_from(model).where(deal_col == deal_id))
        ).scalar() or 0

    context = (
        f"Позиций: {await _count(DealItem, DealItem.deal_id)}, "
        f"документов: {await _count(DealDocument, DealDocument.deal_id)}, "
        f"сообщений: {await _count(Message, Message.deal_id)}."
    )

    gateway = core.services.llm
    kind = "next_step" if payload.kind == "next_step" else "summary"
    text = await (next_step if kind == "next_step" else summarize)(gateway, deal, context)
    model = gateway.model or "mock"

    core.event_bus.emit(
        session,
        f"ai.{kind}.generated",
        {"deal_id": deal_id, "model": model, "actor": "AI", "entity_ref": f"deal:{deal_id}"},
    )
    await session.commit()
    return AiTextOut(kind=kind, text=text, model=model)
