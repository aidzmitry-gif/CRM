"""SALES-51: фоновый шаг жизненного цикла резерва под счёт.

Срез 2. За день до конца срока счёта — однократное напоминание; после срока (если не
оплачен) — аннулирование счёта и снятие резерва. Идемпотентно по флагам
(``reminded_at`` / ``reserve_status``). Регистрируется через ``core.on_tick`` и
вызывается фоновым циклом ядра; в тестах — напрямую. Транзакцией владеет вызывающий.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from sqlalchemy import select

from modules.sales.models import DealDocument
from modules.sales.routes import _deal_stock_items, _utcnow

logger = logging.getLogger("aios.sales")


async def tick_invoice_reserve(session, services) -> None:
    """Один проход по активным зарезервированным счетам: напоминание / аннулирование."""
    today = _utcnow().date()
    docs = (
        await session.execute(
            select(DealDocument).where(
                DealDocument.reserve_status == "reserved",
                DealDocument.status != "paid",
                DealDocument.valid_until.is_not(None),
            )
        )
    ).scalars().all()

    for doc in docs:
        if today > doc.valid_until:
            # срок истёк и счёт не оплачен → аннулировать и снять резерв
            items = await _deal_stock_items(session, doc.deal_id)
            if getattr(services, "stock", None) is not None and items:
                await services.stock.release(session, items)
            doc.status = "cancelled"
            doc.reserve_status = "released"
            doc.cancelled_at = _utcnow()
            services.event_bus.emit(
                session,
                "sales.invoice.cancelled",
                {
                    "document_id": doc.id,
                    "deal_id": doc.deal_id,
                    "number": doc.number,
                    "entity_ref": f"deal:{doc.deal_id}",
                },
            )
            services.event_bus.emit(
                session,
                "sales.stock.released",
                {
                    "document_id": doc.id,
                    "deal_id": doc.deal_id,
                    "items": items,
                    "entity_ref": f"deal:{doc.deal_id}",
                },
            )
            logger.info("Sales: счёт %s аннулирован (срок истёк), резерв снят", doc.number)
        elif doc.reminded_at is None and today >= doc.valid_until - timedelta(days=1):
            # за день до конца срока — однократное напоминание клиенту/продавцу
            doc.reminded_at = _utcnow()
            services.event_bus.emit(
                session,
                "sales.invoice.expiring",
                {
                    "document_id": doc.id,
                    "deal_id": doc.deal_id,
                    "number": doc.number,
                    "valid_until": doc.valid_until.isoformat(),
                    "entity_ref": f"deal:{doc.deal_id}",
                },
            )
            logger.info("Sales: счёт %s истекает завтра — напоминание", doc.number)
