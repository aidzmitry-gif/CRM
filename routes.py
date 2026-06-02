"""HTTP-API модуля Sales. Монтируется ядром под префиксом ``/sales``."""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timezone
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
from modules.sales.models import Activity, Deal, DealDocument, DealItem, KpiTarget
from modules.sales.repository import DealRepository
from modules.sales.schemas import (
    ActivityCreate,
    BoardOut,
    DealCreate,
    DealDetailOut,
    DealItemOut,
    DealRead,
    DealUpdate,
    DocumentCreate,
    DocumentDecision,
    DocumentOut,
    KpiOut,
    StageBoard,
)
from modules.sales.stages import STAGES

router = APIRouter(tags=["sales"])

# Префикс номера и человекочитаемое название документа по типу.
DOC_NUMBER_PREFIX = {"invoice": "СЧ", "contract": "ДГ", "order": "ЗК"}
DOC_TITLES = {"invoice": "Счёт", "contract": "Договор", "order": "Заказ"}
# Типы документов, требующие согласования до записи в 1С (договор → юрист, ч.4).
REQUIRES_APPROVAL = {"contract"}


def _utcnow() -> datetime:
    # наивный UTC — единообразно для SQLite и PostgreSQL
    return datetime.now(timezone.utc).replace(tzinfo=None)


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
async def kpis(session: AsyncSession = Depends(get_session)):
    """Показатели «План на сегодня»: факт (за последнюю дату активностей) vs план."""
    targets = (
        await session.execute(select(KpiTarget).order_by(KpiTarget.sort_order))
    ).scalars().all()

    latest = (await session.execute(select(func.max(Activity.date)))).scalar()
    actuals: dict[str, float] = {}
    if latest is not None:
        rows = await session.execute(
            select(Activity.kpi_key, func.coalesce(func.sum(Activity.value), 0))
            .where(Activity.date == latest)
            .group_by(Activity.kpi_key)
        )
        actuals = {key: float(total) for key, total in rows.all()}

    result: list[KpiOut] = []
    for t in targets:
        actual = actuals.get(t.key, 0.0)
        target = float(t.target)
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
    items = [
        DealItemOut(
            sku_id=r.sku_id,
            code=skus[r.sku_id].code if r.sku_id in skus else "",
            title=skus[r.sku_id].title if r.sku_id in skus else "",
            unit=skus[r.sku_id].unit if r.sku_id in skus else "",
            qty=float(r.qty),
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
        # счёт/заказ: пишем в 1С сразу
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
