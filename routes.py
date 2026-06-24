"""HTTP-API модуля Sales. Монтируется ядром под префиксом ``/sales``."""
from __future__ import annotations

import os
import re
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from core.domain.models import Approval, Contact, Counterparty, Sku
from core.runtime.core import Core
from core.runtime.deps import get_core, get_session
from core.services.approvals import ApprovalOut, ApprovalRequest
from core.services.auth import CurrentUser, get_current_user, require_permission
from modules.sales.ai import draft_reply, next_step, summarize
from modules.sales.models import (
    Activity,
    ContractTemplate,
    Deal,
    DealDocument,
    DealItem,
    DealStageEvent,
    DealTask,
    KpiTarget,
    LossReason,
    Message,
    PriceQuote,
)
from modules.sales.repository import DealRepository, record_stage
from modules.sales.schemas import (
    ActivityCreate,
    AiAssistRequest,
    AiDraftOut,
    AiTextOut,
    BoardOut,
    CallCommentIn,
    CallLinkDealIn,
    CallOut,
    CallResultIn,
    ChatOut,
    ContactCreate,
    ContactOut,
    ContractPrepareIn,
    ContractTemplateCreate,
    ContractTemplateOut,
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
    LoseRequest,
    LossReasonOut,
    MessageCreate,
    MessageOut,
    PackageSentOut,
    PriceInfo,
    PriceQuoteCreate,
    SkuOut,
    StageBoard,
    StageEventOut,
    TaskCreate,
    TaskOut,
    TaskUpdate,
)
from modules.sales.stages import PROBABILITY_BY_STAGE, STAGES, TERMINAL_STAGES

router = APIRouter(tags=["sales"])

# Префикс номера и человекочитаемое название документа по типу.
DOC_NUMBER_PREFIX = {"invoice": "СЧ", "contract": "ДГ", "order": "ЗК"}
DOC_TITLES = {"invoice": "Счёт", "contract": "Договор", "order": "Заказ"}
# Типы документов, требующие согласования до записи в 1С (договор → юрист, ч.4).
REQUIRES_APPROVAL = {"contract"}
# Типы документов, резервирующие складские остатки при проведении (счёт и заказ, SALES-51).
RESERVES_STOCK = {"invoice", "order"}
# План/факт по периодам (sales-34): окно факта (дней) и множитель плана (рабочих дней).
PERIOD_DAYS = {"day": 1, "week": 7, "month": 30, "quarter": 90, "year": 365}
PERIOD_MULT = {"day": 1, "week": 5, "month": 22, "quarter": 65, "year": 250}


def _deal_weight(deal: Deal) -> float:
    """Взвешенная сумма сделки: amount × вероятность (своя или дефолт стадии)."""
    p = deal.probability if deal.probability is not None else PROBABILITY_BY_STAGE.get(deal.stage, 0)
    return float(deal.amount) * p / 100


def _utcnow() -> datetime:
    # наивный UTC — единообразно для SQLite и PostgreSQL
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _task_out(task: DealTask) -> TaskOut:
    """Представление задачи с вычисляемым флагом просрочки (SALES-41)."""
    overdue = task.status == "open" and task.due_at is not None and task.due_at < _utcnow()
    return TaskOut(
        id=task.id,
        deal_id=task.deal_id,
        title=task.title,
        kind=task.kind,
        assignee_id=task.assignee_id,
        due_at=task.due_at,
        status=task.status,
        result=task.result,
        overdue=overdue,
    )


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


async def _counterparty_for_deal(
    session: AsyncSession, deal: Deal, create: bool = False
) -> Counterparty | None:
    """Найти контрагента сделки по имени (связь по названию); опц. создать."""
    cp = (
        await session.execute(select(Counterparty).where(Counterparty.name == deal.counterparty))
    ).scalars().first()
    if cp is None and create:
        cp = Counterparty(name=deal.counterparty)
        session.add(cp)
        await session.flush()
    return cp


async def _clear_primary(session: AsyncSession, counterparty_id: int) -> None:
    """Снять признак основного со всех контактов контрагента."""
    rows = (
        await session.execute(
            select(Contact).where(Contact.counterparty_id == counterparty_id, Contact.is_primary)
        )
    ).scalars().all()
    for contact in rows:
        contact.is_primary = False


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
            "counterparty": counterparty,
            "amount": float(doc.amount),
            "entity_ref": f"deal:{doc.deal_id}",
        },
    )


@router.get("/ping")
async def ping() -> dict:
    """Проверка, что модуль смонтирован."""
    return {"module": "sales", "status": "ok"}


@router.get("/board", response_model=BoardOut)
async def board(owner: str = "", session: AsyncSession = Depends(get_session)) -> BoardOut:
    """Доска сделок: сделки по стадиям с агрегатами. ``owner`` — фильтр по
    ответственному (видимость «по менеджеру», SALES-42)."""
    deals = await DealRepository(session).list()
    if owner:
        deals = [d for d in deals if d.owner == owner]
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
            weighted=float(sum(_deal_weight(d) for d in by_stage.get(s["id"], []))),
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
async def list_deals(
    stuck_days: int = 0,
    has_open_task: bool | None = None,
    session: AsyncSession = Depends(get_session),
):
    """Плоский список сделок. ``stuck_days>0`` — только «висяки» (SALES-43): открытые
    сделки без смены стадии дольше N дней. ``has_open_task=false`` — открытые сделки
    без единой открытой задачи (отчёт «брошенные», SALES-41)."""
    if stuck_days > 0:
        cutoff = _utcnow() - timedelta(days=stuck_days)
        return (
            await session.execute(
                select(Deal)
                .where(Deal.stage.notin_(TERMINAL_STAGES), Deal.stage_changed_at < cutoff)
                .order_by(Deal.id)
            )
        ).scalars().all()
    if has_open_task is False:
        open_deal_ids = (
            select(DealTask.deal_id).where(DealTask.status == "open").distinct()
        )
        return (
            await session.execute(
                select(Deal)
                .where(Deal.stage.notin_(TERMINAL_STAGES), Deal.id.notin_(open_deal_ids))
                .order_by(Deal.id)
            )
        ).scalars().all()
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
    user: CurrentUser = Depends(get_current_user),
):
    """Частично обновить сделку. Смена стадии (drag&drop) пишется в историю и
    обновляет ``stage_changed_at`` через единый хелпер ``record_stage`` (SALES-43)."""
    repo = DealRepository(session)
    deal = await repo.get(deal_id)
    if deal is None:
        raise HTTPException(status_code=404, detail="Сделка не найдена")
    data = payload.model_dump(exclude_unset=True)
    new_stage = data.pop("stage", None)
    if new_stage is not None and new_stage != deal.stage:
        record_stage(session, deal, new_stage, by=user.username)
    await repo.update(deal, data)
    await session.commit()
    return deal


@router.get("/loss-reasons", response_model=list[LossReasonOut])
async def loss_reasons(session: AsyncSession = Depends(get_session)):
    """Справочник активных причин отказа (для выпадашки модалки «Отказ», SALES-40)."""
    return (
        await session.execute(
            select(LossReason).where(LossReason.active).order_by(LossReason.sort_order)
        )
    ).scalars().all()


@router.post("/deals/{deal_id}/lose", response_model=DealRead)
async def lose_deal(
    deal_id: int,
    payload: LoseRequest,
    core: Core = Depends(get_core),
    session: AsyncSession = Depends(get_session),
    user: CurrentUser = Depends(get_current_user),
):
    """Закрыть сделку в отказ с обязательной причиной (SALES-40).

    Причина обязательна; если справочник заполнен — должна быть активным кодом.
    Ставит стадию ``lost`` (через ``record_stage`` → история + ``stage_changed_at``),
    дату закрытия и публикует ``sales.deal.lost`` (→ audit)."""
    deal = await DealRepository(session).get(deal_id)
    if deal is None:
        raise HTTPException(status_code=404, detail="Сделка не найдена")
    if deal.stage == "lost":
        raise HTTPException(status_code=409, detail="Сделка уже закрыта в отказ")
    code = (payload.reason_code or "").strip()
    if not code:
        raise HTTPException(status_code=422, detail="Нужна причина отказа")
    active_codes = set(
        (await session.execute(select(LossReason.code).where(LossReason.active))).scalars().all()
    )
    if active_codes and code not in active_codes:
        raise HTTPException(status_code=422, detail="Неизвестная причина отказа")

    deal.lost_reason_code = code
    deal.lost_comment = payload.comment
    deal.closed_date = date.today().strftime("%d.%m.%Y")
    record_stage(session, deal, "lost", by=user.username)
    core.event_bus.emit(
        session,
        "sales.deal.lost",
        {
            "deal_id": deal.id, "number": deal.number, "reason_code": code,
            "amount": float(deal.amount), "owner": deal.owner, "entity_ref": f"deal:{deal.id}",
        },
    )
    await session.commit()
    return deal


@router.post("/deals/{deal_id}/win", response_model=DealRead)
async def win_deal(
    deal_id: int,
    core: Core = Depends(get_core),
    session: AsyncSession = Depends(get_session),
    user: CurrentUser = Depends(get_current_user),
):
    """Закрыть сделку успешно (SALES-40). Единый путь с логистикой (`record_stage`):
    стадия ``won``, дата закрытия, событие ``sales.deal.won`` (→ audit)."""
    deal = await DealRepository(session).get(deal_id)
    if deal is None:
        raise HTTPException(status_code=404, detail="Сделка не найдена")
    if deal.stage == "won":
        raise HTTPException(status_code=409, detail="Сделка уже выиграна")
    deal.closed_date = date.today().strftime("%d.%m.%Y")
    record_stage(session, deal, "won", by=user.username)
    core.event_bus.emit(
        session,
        "sales.deal.won",
        {
            "deal_id": deal.id, "number": deal.number, "amount": float(deal.amount),
            "owner": deal.owner, "entity_ref": f"deal:{deal.id}",
        },
    )
    await session.commit()
    return deal


@router.get("/deals/{deal_id}/history", response_model=list[StageEventOut])
async def deal_history(deal_id: int, session: AsyncSession = Depends(get_session)):
    """Хронология смен стадий сделки (SALES-43)."""
    return (
        await session.execute(
            select(DealStageEvent)
            .where(DealStageEvent.deal_id == deal_id)
            .order_by(DealStageEvent.id)
        )
    ).scalars().all()


@router.get("/deals/{deal_id}/tasks", response_model=list[TaskOut])
async def list_tasks(deal_id: int, session: AsyncSession = Depends(get_session)):
    """Задачи по сделке (SALES-41): открытые — первыми, по сроку."""
    rows = (
        await session.execute(
            select(DealTask)
            .where(DealTask.deal_id == deal_id)
            .order_by((DealTask.status == "open").desc(), DealTask.due_at, DealTask.id)
        )
    ).scalars().all()
    return [_task_out(t) for t in rows]


@router.post("/deals/{deal_id}/tasks", response_model=TaskOut, status_code=201)
async def create_task(
    deal_id: int,
    payload: TaskCreate,
    core: Core = Depends(get_core),
    session: AsyncSession = Depends(get_session),
):
    """Поставить задачу по сделке (SALES-41) — событие ``sales.task.created`` (→ audit)."""
    deal = await DealRepository(session).get(deal_id)
    if deal is None:
        raise HTTPException(status_code=404, detail="Сделка не найдена")
    task = DealTask(
        deal_id=deal_id,
        title=payload.title,
        kind=payload.kind,
        assignee_id=payload.assignee_id,
        due_at=payload.due_at,
    )
    session.add(task)
    await session.flush()
    core.event_bus.emit(
        session,
        "sales.task.created",
        {"task_id": task.id, "deal_id": deal_id, "entity_ref": f"deal:{deal_id}"},
    )
    await session.commit()
    return _task_out(task)


@router.patch("/tasks/{task_id}", response_model=TaskOut)
async def update_task(
    task_id: int,
    payload: TaskUpdate,
    core: Core = Depends(get_core),
    session: AsyncSession = Depends(get_session),
):
    """Изменить задачу: перенос срока / исполнение / отмена. При закрытии (``done``)
    ставит ``done_at`` и публикует ``sales.task.completed`` (→ audit, SALES-41)."""
    task = await session.get(DealTask, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Задача не найдена")
    data = payload.model_dump(exclude_unset=True)
    becoming_done = data.get("status") == "done" and task.status != "done"
    for key, value in data.items():
        setattr(task, key, value)
    if becoming_done:
        task.done_at = _utcnow()
        core.event_bus.emit(
            session,
            "sales.task.completed",
            {"task_id": task.id, "deal_id": task.deal_id, "entity_ref": f"deal:{task.deal_id}"},
        )
    await session.commit()
    return _task_out(task)


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


@router.get("/deals/{deal_id}/contacts", response_model=list[ContactOut])
async def list_contacts(deal_id: int, session: AsyncSession = Depends(get_session)):
    """Контакты контрагента сделки (основной — первым), sales-13."""
    deal = await DealRepository(session).get(deal_id)
    if deal is None:
        return []
    cp = await _counterparty_for_deal(session, deal)
    if cp is None:
        return []
    return (
        await session.execute(
            select(Contact)
            .where(Contact.counterparty_id == cp.id)
            .order_by(Contact.is_primary.desc(), Contact.id)
        )
    ).scalars().all()


@router.post("/deals/{deal_id}/contacts", response_model=ContactOut, status_code=201)
async def add_contact(
    deal_id: int, payload: ContactCreate, session: AsyncSession = Depends(get_session)
):
    """Добавить контакт контрагенту сделки (контрагент создаётся по имени при нужде)."""
    deal = await DealRepository(session).get(deal_id)
    if deal is None:
        raise HTTPException(status_code=404, detail="Сделка не найдена")
    cp = await _counterparty_for_deal(session, deal, create=True)
    assert cp is not None
    if payload.is_primary:
        await _clear_primary(session, cp.id)
    contact = Contact(
        counterparty_id=cp.id,
        full_name=payload.full_name,
        phone=payload.phone,
        email=payload.email,
        is_primary=payload.is_primary,
    )
    session.add(contact)
    await session.flush()
    await session.commit()
    return contact


@router.get("/chats", response_model=list[ChatOut])
async def list_chats(session: AsyncSession = Depends(get_session)):
    """Диалоги для панели «Чаты и дела»: сделки с последним сообщением переписки."""
    msgs = (
        await session.execute(select(Message).order_by(Message.id.desc()).limit(100))
    ).scalars().all()
    deals = {d.id: d for d in (await session.execute(select(Deal))).scalars().all()}
    # SALES-49: непрочитанные входящие по сделкам (для бейджа в панели чатов)
    unread_map = {
        deal_id: int(n)
        for deal_id, n in (
            await session.execute(
                select(Message.deal_id, func.count())
                .where(Message.direction == "in", Message.read_at.is_(None))
                .group_by(Message.deal_id)
            )
        ).all()
    }
    chats: list[ChatOut] = []
    seen: set[int] = set()
    for m in msgs:
        if m.deal_id in seen or m.deal_id not in deals:
            continue
        seen.add(m.deal_id)
        deal = deals[m.deal_id]
        chats.append(
            ChatOut(
                deal_id=m.deal_id,
                number=deal.number,
                company=deal.counterparty,
                last_text=m.text,
                channel=m.channel,
                direction=m.direction,
                unread=unread_map.get(m.deal_id, 0),
            )
        )
        if len(chats) >= 20:
            break
    return chats


@router.patch("/contacts/{contact_id}/primary", response_model=ContactOut)
async def set_primary_contact(contact_id: int, session: AsyncSession = Depends(get_session)):
    """Назначить контакт основным (снимая признак с остальных контактов контрагента)."""
    contact = await session.get(Contact, contact_id)
    if contact is None:
        raise HTTPException(status_code=404, detail="Контакт не найден")
    if contact.counterparty_id is not None:
        await _clear_primary(session, contact.counterparty_id)
    contact.is_primary = True
    await session.commit()
    return contact


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
        await _submit_contract_for_approval(core, session, doc, deal, payload.requested_by)
    else:
        # счёт/заказ: пишем в 1С сразу; счёт и заказ дополнительно резервируют остатки (SALES-51)
        if payload.kind in RESERVES_STOCK and core.services.stock is not None:
            reserved = await core.services.stock.reserve(
                session, await _deal_stock_items(session, deal_id)
            )
            if reserved:
                # фиксируем резерв на документе + срок действия счёта (5 дней по счёт-протоколу)
                valid_days = int(os.getenv("AIOS_INVOICE_VALID_DAYS", "5"))
                doc.reserve_status = "reserved"
                doc.reserved_at = _utcnow()
                doc.valid_until = _utcnow().date() + timedelta(days=valid_days)
                core.event_bus.emit(
                    session,
                    "sales.stock.reserved",
                    {
                        "document_id": doc.id,
                        "deal_id": deal_id,
                        "items": reserved,
                        "valid_until": doc.valid_until.isoformat(),
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


# ──────────────────────── Договор по шаблону (SALES-53) ────────────────────────

# Плейсхолдеры тела шаблона: {{seller.name}}, {{buyer.unp}}, {{items}}, {{total}}, …
_PLACEHOLDER = re.compile(r"\{\{\s*([\w.]+)\s*\}\}")


def _seller_requisites(core: Core) -> dict[str, str]:
    """Реквизиты своей организации (продавца) из конфига (ТЗ C.5, не shared-схема)."""
    c = core.config
    return {
        "name": c.seller_name, "unp": c.seller_unp, "address": c.seller_address,
        "director": c.seller_director, "phone": c.seller_phone, "email": c.seller_email,
    }


async def _buyer_requisites(
    session: AsyncSession, core: Core, deal: Deal, unp: str
) -> dict[str, str]:
    """Реквизиты покупателя: из ЕГР по УНП (graceful) + обогащение Counterparty.unp.

    Реестр выключен или УНП не найден → минимум из сделки (вводится вручную). Имя
    контрагента в Counterparty не перезатираем (связь сделки — по имени). Внешний
    вызов реестра делаем ДО создания контрагента, чтобы не держать открытую запись/блок
    в БД на время сетевого запроса в ЕГР.
    """
    info = None
    if unp and core.services.registry is not None:
        info = await core.services.registry.lookup(unp)
    cp = await _counterparty_for_deal(session, deal, create=True)
    buyer = {"name": deal.counterparty, "unp": unp or (cp.unp if cp else "") or ""}
    if info:
        buyer.update({k: str(v) for k, v in info.items() if v})
    if cp is not None and unp:
        cp.unp = cp.unp or unp
    return buyer


async def _contract_items(session: AsyncSession, deal_id: int) -> list[str]:
    """Строки спецификации договора из позиций сделки (title — qty unit)."""
    rows = (
        await session.execute(
            select(DealItem, Sku)
            .join(Sku, Sku.id == DealItem.sku_id, isouter=True)
            .where(DealItem.deal_id == deal_id)
            .order_by(DealItem.id)
        )
    ).all()
    lines = []
    for item, sku in rows:
        title = sku.title if sku else f"позиция #{item.sku_id}"
        unit = sku.unit if sku else "шт"
        lines.append(f"{title} — {item.qty} {unit}")
    return lines


def _render_contract(body: str, ctx: dict[str, str]) -> str:
    """Подставить плейсхолдеры {{key}} (плоские ключи seller.name/buyer.unp/…)."""
    return _PLACEHOLDER.sub(lambda m: ctx.get(m.group(1), ""), body)


async def _submit_contract_for_approval(
    core: Core,
    session: AsyncSession,
    doc: DealDocument,
    deal: Deal,
    requested_by: str,
    extra_event: dict | None = None,
) -> None:
    """Договор → на согласование юристу (ч.4) + событие ``sales.document.created``.

    Общий путь для обоих способов создания договора: универсального
    ``POST /documents`` и ``POST /deals/{id}/contract`` (SALES-53) — чтобы маршрут
    согласования и форма события не разъезжались.
    """
    doc.status = "pending_approval"
    await core.services.approvals.request(
        session,
        "deal.contract",
        f"document:{doc.id}",
        f"{deal.number} — {DOC_TITLES['contract']} ({deal.counterparty})",
        requested_by,
    )
    payload = {
        "document_id": doc.id,
        "deal_id": deal.id,
        "kind": "contract",
        "number": doc.number,
        "entity_ref": f"deal:{deal.id}",
    }
    if extra_event:
        payload.update(extra_event)
    core.event_bus.emit(session, "sales.document.created", payload)


@router.get("/contract-templates", response_model=list[ContractTemplateOut])
async def list_contract_templates(session: AsyncSession = Depends(get_session)):
    """Активные шаблоны договора для окна «Подготовить договор» (SALES-53)."""
    return (
        await session.execute(
            select(ContractTemplate)
            .where(ContractTemplate.is_active.is_(True))
            .order_by(ContractTemplate.name)
        )
    ).scalars().all()


@router.post("/contract-templates", response_model=ContractTemplateOut, status_code=201)
async def create_contract_template(
    payload: ContractTemplateCreate,
    session: AsyncSession = Depends(get_session),
    _: object = Depends(require_permission("sales.deal.write")),
):
    """Завести/сидировать шаблон договора (SALES-53)."""
    tpl = ContractTemplate(code=payload.code, name=payload.name, body=payload.body)
    session.add(tpl)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(status_code=409, detail="Шаблон с таким кодом уже есть")
    return tpl


@router.post("/deals/{deal_id}/contract", response_model=DocumentOut, status_code=201)
async def prepare_contract(
    deal_id: int,
    payload: ContractPrepareIn,
    core: Core = Depends(get_core),
    session: AsyncSession = Depends(get_session),
    _: object = Depends(require_permission("sales.deal.write")),
):
    """SALES-53: подготовить договор по шаблону + реквизиты покупателя по УНП.

    Реквизиты покупателя подтягиваются из ЕГР по УНП (graceful при выкл реестра),
    Counterparty обогащается УНП, условия частично предзаполнены из сделки. Договор
    создаётся как DealDocument(kind=contract) и уходит на согласование; запись в 1С —
    после одобрения (/documents/{id}/decide), поэтому шлюз 1С здесь не требуется.
    """
    deal = await DealRepository(session).get(deal_id)
    if deal is None:
        raise HTTPException(status_code=404, detail="Сделка не найдена")
    tpl = (
        await session.execute(
            select(ContractTemplate).where(
                ContractTemplate.code == payload.template_code,
                ContractTemplate.is_active.is_(True),
            )
        )
    ).scalars().first()
    if tpl is None:
        raise HTTPException(status_code=404, detail="Шаблон договора не найден")
    # один активный договор на сделку: номер ДГ-{deal} не уникален в БД, дубль создал бы
    # два договора с одинаковым номером (отклонённый можно перевыставить).
    existing = (
        await session.execute(
            select(DealDocument).where(
                DealDocument.deal_id == deal_id,
                DealDocument.kind == "contract",
                DealDocument.status != "rejected",
            )
        )
    ).scalars().first()
    if existing is not None:
        raise HTTPException(status_code=409, detail="Договор по сделке уже подготовлен")

    buyer = await _buyer_requisites(session, core, deal, payload.unp.strip())
    doc = DealDocument(
        deal_id=deal_id,
        kind="contract",
        number=f"{DOC_NUMBER_PREFIX['contract']}-{deal.number}",
        amount=deal.amount,
        template_id=tpl.id,
        payment_terms=payload.payment_terms or None,
        delivery_terms=payload.delivery_terms or None,
        terms_json={"buyer": buyer, "custom": payload.terms or {}},
    )
    session.add(doc)
    await session.flush()
    await _submit_contract_for_approval(
        core, session, doc, deal, payload.requested_by,
        extra_event={"template": tpl.code, "buyer_unp": buyer.get("unp", "")},
    )
    await session.commit()
    return doc


@router.get("/documents/{doc_id}/render", response_class=HTMLResponse)
async def render_contract(
    doc_id: int,
    core: Core = Depends(get_core),
    session: AsyncSession = Depends(get_session),
    _: object = Depends(require_permission("sales.deal.read")),
):
    """Рендер договора по шаблону в HTML (печатная форма, ТЗ C.2). Только kind=contract.

    Гард ``sales.deal.read``: форма содержит реквизиты продавца и покупателя (ЕГР) —
    не отдаём анонимно (прод публичен).
    """
    doc = await session.get(DealDocument, doc_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="Документ не найден")
    if doc.kind != "contract":
        raise HTTPException(status_code=400, detail="Рендер по шаблону — только для договора")
    tpl = await session.get(ContractTemplate, doc.template_id) if doc.template_id else None
    if tpl is None:
        raise HTTPException(status_code=409, detail="У договора не задан шаблон")
    deal = await DealRepository(session).get(doc.deal_id)
    buyer = (doc.terms_json or {}).get("buyer", {})
    ctx = {
        "number": doc.number,
        "items": "; ".join(await _contract_items(session, doc.deal_id)),
        "total": f"{float(doc.amount):.2f} BYN",
        "payment_terms": doc.payment_terms or "",
        "delivery_terms": doc.delivery_terms or "",
        "valid_until": doc.valid_until.isoformat() if doc.valid_until else "",
        "deal": deal.number if deal else "",
    }
    ctx.update({f"seller.{k}": v for k, v in _seller_requisites(core).items()})
    ctx.update({f"buyer.{k}": str(v) for k, v in buyer.items()})
    return HTMLResponse(_render_contract(tpl.body, ctx))


@router.post("/deals/{deal_id}/send-package", response_model=PackageSentOut)
async def send_package(
    deal_id: int,
    core: Core = Depends(get_core),
    session: AsyncSession = Depends(get_session),
    _: object = Depends(require_permission("sales.deal.write")),
):
    """SALES-53 C.4: отправить клиенту пакет «счёт + договор» одной записью.

    Берём последний проведённый счёт и последний согласованный (проведённый) договор
    сделки — по ТЗ пакет уходит ПОСЛЕ согласования договора. Эмитим ``sales.package.sent``
    и пишем ОДНУ запись в историю переписки. Реальная доставка (email/Telegram, B.3) —
    отдельный слой; здесь фиксируем факт отправки пакета.
    """
    deal = await DealRepository(session).get(deal_id)
    if deal is None:
        raise HTTPException(status_code=404, detail="Сделка не найдена")
    docs = (
        await session.execute(
            select(DealDocument)
            .where(
                DealDocument.deal_id == deal_id,
                DealDocument.status.in_(("posted", "paid")),
            )
            .order_by(DealDocument.id.desc())
        )
    ).scalars().all()
    # счёт — проведённый/оплаченный; договор — проведённый (после согласования)
    invoice = next((d for d in docs if d.kind == "invoice"), None)
    contract = next((d for d in docs if d.kind == "contract"), None)
    if invoice is None or contract is None:
        raise HTTPException(
            status_code=409, detail="Нужны проведённый счёт и согласованный договор"
        )

    channel = "email"
    session.add(
        Message(
            deal_id=deal_id,
            channel=channel,
            direction="out",
            author="Система",
            text=f"Отправлен пакет: счёт {invoice.number} + договор {contract.number}",
        )
    )
    core.event_bus.emit(
        session,
        "sales.package.sent",
        {
            "deal_id": deal_id,
            "invoice_number": invoice.number,
            "contract_number": contract.number,
            "channel": channel,
            "entity_ref": f"deal:{deal_id}",
        },
    )
    await session.commit()
    return PackageSentOut(
        deal_id=deal_id,
        invoice_number=invoice.number,
        contract_number=contract.number,
        channel=channel,
    )


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


@router.post("/deals/{deal_id}/messages/read")
async def mark_messages_read(deal_id: int, session: AsyncSession = Depends(get_session)):
    """Пометить входящие сообщения сделки прочитанными (обнуляет счётчик, SALES-49)."""
    deal = await DealRepository(session).get(deal_id)
    if deal is None:
        raise HTTPException(status_code=404, detail="Сделка не найдена")
    rows = (
        await session.execute(
            select(Message).where(
                Message.deal_id == deal_id,
                Message.direction == "in",
                Message.read_at.is_(None),
            )
        )
    ).scalars().all()
    now = _utcnow()
    for m in rows:
        m.read_at = now
    await session.commit()
    return {"ok": True, "read": len(rows)}


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


# --- Окно входящего звонка (SALES-50): SSE-поток, журнал, действия --------------------
@router.get("/calls/stream")
async def calls_stream(
    owner: str | None = None,
    user: CurrentUser = Depends(require_permission("sales.deal.read")),
):
    """SSE-поток карточек звонков продавца (всплывающее окно входящего звонка).

    Подписка по ``owner`` (= ``Deal.owner``, ФИО продавца); по умолчанию — текущий
    пользователь. ponytail: маппинг username↔Deal.owner закроется реальной
    аутентификацией (Keycloak, P1); сейчас фронт передаёт ``?owner=<ФИО>``.
    """
    import asyncio
    import json

    from fastapi.responses import StreamingResponse

    from modules.sales import calls as calls_mod

    target = owner or user.username
    queue = calls_mod.subscribe(target)

    async def _gen():
        try:
            yield ": connected\n\n"
            while True:
                try:
                    card = await asyncio.wait_for(queue.get(), timeout=15.0)
                    yield f"data: {json.dumps(card, ensure_ascii=False)}\n\n"
                except asyncio.TimeoutError:
                    yield ": ping\n\n"  # heartbeat против обрыва простаивающего соединения
        finally:
            calls_mod.unsubscribe(target, queue)

    return StreamingResponse(_gen(), media_type="text/event-stream")


@router.get("/calls", response_model=list[CallOut])
async def list_calls(
    status: str | None = None,
    owner: str | None = None,
    date: str | None = None,
    session: AsyncSession = Depends(get_session),
    _: CurrentUser = Depends(require_permission("sales.deal.read")),
):
    """Журнал звонков с фильтрами: статус / продавец / дата (``YYYY-MM-DD``)."""
    from modules.sales.models import CallLog

    stmt = select(CallLog).order_by(CallLog.started_at.desc())
    if status:
        stmt = stmt.where(CallLog.status == status)
    if owner:
        stmt = stmt.where(CallLog.owner == owner)
    if date:
        try:
            day = datetime.fromisoformat(date)  # param `date` затеняет datetime.date — берём datetime
        except ValueError:
            raise HTTPException(status_code=400, detail="date: ожидается YYYY-MM-DD")
        start = datetime(day.year, day.month, day.day)
        stmt = stmt.where(CallLog.started_at >= start, CallLog.started_at < start + timedelta(days=1))
    return (await session.execute(stmt)).scalars().all()


@router.get("/calls/{cid}", response_model=CallOut)
async def get_call(
    cid: int,
    session: AsyncSession = Depends(get_session),
    _: CurrentUser = Depends(require_permission("sales.deal.read")),
):
    """Карточка одного звонка."""
    from modules.sales.models import CallLog

    call = await session.get(CallLog, cid)
    if call is None:
        raise HTTPException(status_code=404, detail="Звонок не найден")
    return call


@router.post("/calls/{cid}/comment", response_model=CallOut)
async def call_comment(
    cid: int,
    payload: CallCommentIn,
    session: AsyncSession = Depends(get_session),
    _: CurrentUser = Depends(require_permission("sales.deal.write")),
):
    """Заметка по звонку."""
    from modules.sales.models import CallLog

    call = await session.get(CallLog, cid)
    if call is None:
        raise HTTPException(status_code=404, detail="Звонок не найден")
    call.comment = payload.comment
    await session.commit()
    return call


@router.post("/calls/{cid}/result", response_model=CallOut)
async def call_result(
    cid: int,
    payload: CallResultIn,
    session: AsyncSession = Depends(get_session),
    _: CurrentUser = Depends(require_permission("sales.deal.write")),
):
    """Отметить итог/классификацию звонка."""
    from modules.sales.models import CallLog

    call = await session.get(CallLog, cid)
    if call is None:
        raise HTTPException(status_code=404, detail="Звонок не найден")
    call.result = payload.result
    await session.commit()
    return call


@router.post("/calls/{cid}/link-deal", response_model=CallOut)
async def call_link_deal(
    cid: int,
    payload: CallLinkDealIn,
    session: AsyncSession = Depends(get_session),
    core: Core = Depends(get_core),
    _: CurrentUser = Depends(require_permission("sales.deal.write")),
):
    """Привязать звонок к существующей сделке (``deal_id``) или создать новую (``create``)."""
    from modules.sales.models import CallLog

    call = await session.get(CallLog, cid)
    if call is None:
        raise HTTPException(status_code=404, detail="Звонок не найден")
    if payload.deal_id is not None:
        deal = await session.get(Deal, payload.deal_id)
        if deal is None:
            raise HTTPException(status_code=404, detail="Сделка не найдена")
        call.deal_id = deal.id
    elif payload.create:
        cp_name = ""
        if call.counterparty_id is not None:
            cp = await session.get(Counterparty, call.counterparty_id)
            cp_name = cp.name if cp is not None else ""
        deal = Deal(
            number=f"CRM-CALL-{call.id}",
            title=f"Звонок {call.phone_e164 or call.call_id}",
            counterparty=cp_name or (call.phone_e164 or "Неизвестный номер"),
            owner=call.owner,
            stage="new",
        )
        session.add(deal)
        try:
            await session.flush()
        except IntegrityError:
            await session.rollback()
            raise HTTPException(status_code=409, detail="Сделка по этому звонку уже создана")
        call.deal_id = deal.id
        core.event_bus.emit(session, "sales.deal.created", {"number": deal.number, "title": deal.title})
    else:
        raise HTTPException(status_code=400, detail="Укажите deal_id или create=true")
    await session.commit()
    return call


@router.post("/telephony/incoming")
async def telephony_incoming(
    payload: dict,
    session: AsyncSession = Depends(get_session),
    core: Core = Depends(get_core),
    _: CurrentUser = Depends(require_permission("sales.deal.write")),
):
    """Прямой приём нормализованного события звонка (fallback/тест, если не через шину).

    Обрабатывает синхронно (апсерт записи + push карточки), минуя задержку relay.
    ``payload`` — как от коннектора + ``event_type`` (по умолчанию incoming).
    """
    from core.services.eventbus import EventContext
    from modules.sales import calls as calls_mod

    event_type = payload.get("event_type", "telephony.call.incoming")
    handler = calls_mod.EVENT_HANDLERS.get(event_type)
    if handler is None:
        raise HTTPException(status_code=400, detail=f"Неизвестный тип события: {event_type}")
    await handler(payload, EventContext(session=session, services=core.services))
    await session.commit()
    return {"ok": True, "event_type": event_type, "call_id": payload.get("call_id")}
