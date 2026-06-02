"""HTTP-API модуля Sales. Монтируется ядром под префиксом ``/sales``."""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timezone
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from core.domain.models import Sku
from core.runtime.core import Core
from core.runtime.deps import get_core, get_session
from core.services.approvals import ApprovalOut, ApprovalRequest
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
    DocumentOut,
    KpiOut,
    StageBoard,
)
from modules.sales.stages import STAGES

router = APIRouter(tags=["sales"])

# Префикс номера документа по типу (счёт / договор / заказ).
DOC_NUMBER_PREFIX = {"invoice": "СЧ", "contract": "ДГ", "order": "ЗК"}


def _utcnow() -> datetime:
    # наивный UTC — единообразно для SQLite и PostgreSQL
    return datetime.now(timezone.utc).replace(tzinfo=None)


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
    """Сформировать документ сделки и записать его в 1С (часть 9).

    Счёт пишется в 1С сразу (``draft`` → ``posted``) через фасад
    ``core.services.onec``; о записи сообщается событием ``sales.document.posted``
    (→ audit log). Договор позже пойдёт через согласование (часть 4) до записи.
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

    # запись в 1С через фасад ядра (в прототипе — mock-коннектор модуля integrations)
    result = await core.services.onec.post_document(
        payload.kind,
        {"number": number, "counterparty": deal.counterparty, "amount": float(deal.amount)},
    )
    doc.onec_ref = result.get("ref")
    doc.status = "posted"
    doc.posted_at = _utcnow()

    core.event_bus.emit(
        session,
        "sales.document.posted",
        {
            "document_id": doc.id,
            "deal_id": deal_id,
            "kind": payload.kind,
            "number": number,
            "onec_ref": doc.onec_ref,
            "entity_ref": f"deal:{deal_id}",
        },
    )
    await session.commit()
    return doc
