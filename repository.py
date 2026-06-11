"""Репозиторий сделок модуля Sales."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from core.db.repository import Repository
from modules.sales.models import Deal, DealStageEvent
from modules.sales.schemas import DealCreate


class DealRepository(Repository[Deal]):
    model = Deal

    async def create(self, data: DealCreate) -> Deal:
        deal = Deal(**data.model_dump())
        return await self.add(deal)


def _utcnow() -> datetime:
    # наивный UTC — единообразно для SQLite и PostgreSQL
    return datetime.now(timezone.utc).replace(tzinfo=None)


def record_stage(session: AsyncSession, deal: Deal, to_stage: str, by: str = "") -> None:
    """Единая точка смены стадии сделки (SALES-43): пишет историю и денормализует
    ``stage_changed_at``. Не коммитит — границей транзакции владеет вызывающий код.

    Используется из роутов (drag&drop / win / lose) и из обработчика
    ``logistics.shipment.delivered`` — чтобы стадия, дата и история не разъехались.
    """
    session.add(
        DealStageEvent(
            deal_id=deal.id,
            from_stage=deal.stage,
            to_stage=to_stage,
            changed_by=by,
            changed_at=_utcnow(),
        )
    )
    deal.stage = to_stage
    deal.stage_changed_at = _utcnow()
