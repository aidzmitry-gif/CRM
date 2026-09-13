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

from modules.sales.accounting_ownership import DealOwnership
from modules.sales.documents import lock_deal
from modules.sales.models import DealDocument
from modules.sales.routes import _utcnow

logger = logging.getLogger("aios.sales")


async def remind_erp_invoice(session, services, doc, today):
    """Queue an internal reminder; expiry never authorizes warehouse release."""
    from modules.sales.invoice_issuance import verified_receipt
    from modules.sales.invoice_settlements import receipt_query
    from modules.sales.reservation_source import SalesReservationSource

    source = SalesReservationSource()
    org = await source.invoice_organization(session, doc.id)
    await services.accounting.lock_event_organization(session, org)
    await lock_deal(session, doc.deal_id)
    await session.refresh(doc)
    receipt = await verified_receipt(session, doc, org)
    if receipt is None:
        raise ValueError("Invoice reminder requires a verified issuance receipt")
    eligible_reserve = doc.reserve_status == "reserved" or (
        doc.reserve_mode == "on_order" and doc.reserve_status == "unreserved")
    if (doc.status not in {"issued", "posted"} or not eligible_reserve
            or doc.superseded_by_id is not None or doc.reminded_at is not None
            or doc.valid_until is None or today < doc.valid_until - timedelta(days=1)):
        return
    if await session.scalar(receipt_query(doc.id)):
        return
    doc.reminded_at = _utcnow()
    services.event_bus.emit(session, "sales.invoice.expiring", {
        "organization_id": org, "document_id": doc.id, "deal_id": doc.deal_id,
        "document_version": doc.version, "content_sha256": doc.content_sha256,
        "number": doc.number, "valid_until": doc.valid_until.isoformat(),
        "expiry_state": "review_required" if today > doc.valid_until else "expiring",
        "customer_notification": "requires_authorized_send",
        "entity_ref": f"deal:{doc.deal_id}",
    })


async def tick_invoice_reserve(session, services) -> None:
    """Один проход по активным зарезервированным счетам: напоминание / аннулирование."""
    today = _utcnow().date()
    candidates = (
        await session.execute(
            select(DealDocument, DealOwnership.organization_id).outerjoin(
                DealOwnership, DealOwnership.deal_id == DealDocument.deal_id,
            ).where(
                DealDocument.reserve_status.in_({"reserved", "unreserved"}),
                DealDocument.status != "paid",
                DealDocument.valid_until.is_not(None),
            ).order_by(DealOwnership.organization_id, DealDocument.id)
        )
    ).all()

    # A legacy row can take a deal lock too. Acquire every known company lock
    # first, so a later ERP row cannot invert the organization -> deal order.
    for org in sorted({org for _, org in candidates if org is not None}):
        await services.accounting.lock_event_organization(session, org)

    for doc, organization_id in candidates:
        from modules.sales.invoice_issuance import is_erp_invoice
        if await is_erp_invoice(session, doc):
            await remind_erp_invoice(session, services, doc, today)
            continue
        await lock_deal(session, doc.deal_id)
        await session.refresh(doc)
        if doc.reserve_status != "reserved" or doc.status == "paid":
            continue
        from modules.sales.invoice_settlements import receipt_query

        if await session.scalar(receipt_query(doc.id)):
            continue
        if today > doc.valid_until:
            # срок истёк и счёт не оплачен → аннулировать и снять резерв
            if not doc.snapshot_json:
                logger.warning("Legacy reserve needs review for document %s; original items unknown", doc.id)
                continue
            items = [{"sku_code": line["sku_code"], "qty": line["qty"]}
                     for line in doc.snapshot_json["items"] if line.get("sku_code")]
            if getattr(services, "stock", None) is None or not items:
                logger.warning("Invoice %s cannot expire safely: reservation release is unavailable", doc.id)
                continue
            await services.stock.release(session, items)
            doc.status = "cancelled"
            doc.reserve_status = "released"
            doc.cancelled_at = _utcnow()
            services.event_bus.emit(
                session,
                "sales.invoice.cancelled",
                {
                    "organization_id": organization_id,
                    "document_id": doc.id,
                    "deal_id": doc.deal_id,
                    "document_version": doc.version,
                    "content_sha256": doc.content_sha256,
                    "number": doc.number,
                    "entity_ref": f"deal:{doc.deal_id}",
                    "customer_notification": "requires_authorized_send",
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
                    "organization_id": organization_id,
                    "document_id": doc.id,
                    "deal_id": doc.deal_id,
                    "document_version": doc.version,
                    "content_sha256": doc.content_sha256,
                    "number": doc.number,
                    "valid_until": doc.valid_until.isoformat(),
                    "expiry_state": "expiring",
                    "customer_notification": "requires_authorized_send",
                    "entity_ref": f"deal:{doc.deal_id}",
                },
            )
            logger.info("Sales: счёт %s истекает завтра — напоминание", doc.number)
