"""HTTP-API модуля Sales. Монтируется ядром под префиксом ``/sales``."""
from __future__ import annotations

from fastapi import APIRouter, Depends

from core.runtime.core import Core
from core.runtime.deps import get_core
from modules.sales.models import Deal

router = APIRouter(tags=["sales"])

# демо-данные каркаса (в части 7 заменяются на хранение в БД)
_DEMO_DEALS: list[Deal] = [
    Deal(
        id=1,
        number="CRM-2024-0156",
        title="Поставка аккумуляторов",
        counterparty="ООО Пример",
        amount=1_750_000,
        priority="Высокий",
    ),
]


@router.get("/ping")
async def ping() -> dict:
    """Проверка, что модуль смонтирован."""
    return {"module": "sales", "status": "ok"}


@router.get("/deals", response_model=list[Deal])
async def list_deals() -> list[Deal]:
    """Список сделок (демо-данные каркаса)."""
    return _DEMO_DEALS


@router.post("/deals", response_model=Deal, status_code=201)
async def create_deal(deal: Deal, core: Core = Depends(get_core)) -> Deal:
    """Создать сделку и опубликовать доменное событие через шину ядра."""
    deal.id = len(_DEMO_DEALS) + 1
    _DEMO_DEALS.append(deal)
    await core.event_bus.publish("sales.deal.created", deal.model_dump())
    return deal
