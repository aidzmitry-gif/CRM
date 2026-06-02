"""HTTP-API модуля Sales. Монтируется ядром под префиксом ``/sales``."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from core.runtime.core import Core
from core.runtime.deps import get_core, get_session
from modules.sales.repository import DealRepository
from modules.sales.schemas import DealCreate, DealRead

router = APIRouter(tags=["sales"])


@router.get("/ping")
async def ping() -> dict:
    """Проверка, что модуль смонтирован."""
    return {"module": "sales", "status": "ok"}


@router.get("/deals", response_model=list[DealRead])
async def list_deals(session: AsyncSession = Depends(get_session)):
    """Список сделок из БД."""
    return await DealRepository(session).list()


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
