"""Репозиторий сделок модуля Sales."""
from __future__ import annotations

from core.db.repository import Repository
from modules.sales.models import Deal
from modules.sales.schemas import DealCreate


class DealRepository(Repository[Deal]):
    model = Deal

    async def create(self, data: DealCreate) -> Deal:
        deal = Deal(**data.model_dump())
        return await self.add(deal)
