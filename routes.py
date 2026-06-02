"""HTTP-API модуля Sales. Монтируется ядром под префиксом ``/sales``."""
from __future__ import annotations

from collections import defaultdict

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from core.runtime.core import Core
from core.runtime.deps import get_core, get_session
from modules.sales.models import Deal
from modules.sales.repository import DealRepository
from modules.sales.schemas import BoardOut, DealCreate, DealRead, StageBoard
from modules.sales.stages import STAGES

router = APIRouter(tags=["sales"])


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


@router.get("/deals", response_model=list[DealRead])
async def list_deals(session: AsyncSession = Depends(get_session)):
    """Плоский список сделок."""
    return await DealRepository(session).list()


@router.get("/deals/{deal_id}", response_model=DealRead)
async def get_deal(deal_id: int, session: AsyncSession = Depends(get_session)):
    """Одна сделка по id."""
    deal = await DealRepository(session).get(deal_id)
    if deal is None:
        raise HTTPException(status_code=404, detail="Сделка не найдена")
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
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(status_code=409, detail="Сделка с таким номером уже существует")
    await core.event_bus.publish(
        "sales.deal.created", {"number": deal.number, "title": deal.title}
    )
    return deal
