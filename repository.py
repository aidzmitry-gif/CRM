"""Репозиторий сделок модуля Sales."""
from __future__ import annotations

from core.db.repository import Repository
from modules.sales.models import Deal
from modules.sales.schemas import DealCreate


class DealRepository(Repository[Deal]):
    model = Deal

    async def create(self, data: DealCreate) -> Deal:
        deal = Deal(
            number=data.number,
            title=data.title,
            counterparty=data.counterparty,
            amount=data.amount,
            stage=data.stage.value,
            priority=data.priority,
        )
        return await self.add(deal)
