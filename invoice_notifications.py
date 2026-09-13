"""Durable, reviewable customer-notification receipts for invoice lifecycle events.

The event relay records the exact invoice identity and the recipient taken from
the immutable document snapshot.  It never sends mail by itself: an authorized
user prepares and confirms a message through the existing sales mail queue.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    event,
    func,
    select,
)
from sqlalchemy.orm import Mapped, mapped_column

from core.db.base import Base
from modules.sales.mail_transport import address

logger = logging.getLogger("aios.sales.invoice_notifications")

EXPIRING = "sales.invoice.expiring"
CANCELLED = "sales.invoice.cancelled"
EVENT_TYPES = frozenset({EXPIRING, CANCELLED})


class InvoiceNotification(Base):
    """Immutable notification decision for one exact invoice lifecycle event.

    ``pending`` means the historical snapshot contains a valid mailbox and an
    authorized user may prepare a message.  ``blocked`` is a durable review
    result, for example when the snapshot has no valid customer mailbox.
    Delivery attempts belong to ``sales.outgoing_email`` and are deliberately
    not folded into this immutable event receipt.
    """

    __tablename__ = "invoice_notification"
    __table_args__ = (
        UniqueConstraint("organization_id", "event_id", name="uq_invoice_notification_event"),
        UniqueConstraint("organization_id", "business_key", name="uq_invoice_notification_business"),
        CheckConstraint("organization_id > 0", name="invoice_notification_org_positive"),
        CheckConstraint("deal_id > 0", name="invoice_notification_deal_positive"),
        CheckConstraint("document_id > 0", name="invoice_notification_document_positive"),
        CheckConstraint("document_version > 0", name="invoice_notification_version_positive"),
        CheckConstraint("event_id > 0", name="invoice_notification_event_positive"),
        CheckConstraint("channel = 'email'", name="invoice_notification_channel_email"),
        CheckConstraint("state IN ('pending', 'blocked')", name="invoice_notification_state"),
        CheckConstraint("state = 'pending' OR reason IS NOT NULL", name="invoice_notification_block_reason"),
        Index("ix_invoice_notification_document", "organization_id", "document_id", "created_at"),
        {"schema": "sales"},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(Integer)
    deal_id: Mapped[int] = mapped_column(Integer, ForeignKey("sales.deal.id"), index=True)
    document_id: Mapped[int] = mapped_column(Integer, ForeignKey("sales.deal_document.id"), index=True)
    document_version: Mapped[int] = mapped_column(Integer)
    event_id: Mapped[int] = mapped_column(Integer)
    event_type: Mapped[str] = mapped_column(String(64))
    business_key: Mapped[str] = mapped_column(String(128))
    request_hash: Mapped[str] = mapped_column(String(64))
    channel: Mapped[str] = mapped_column(String(16), default="email", server_default="email")
    recipient: Mapped[str | None] = mapped_column(String(254))
    state: Mapped[str] = mapped_column(String(16))
    reason: Mapped[str | None] = mapped_column(String(128))
    payload: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


def _immutable(*args) -> None:
    raise ValueError("Invoice notification receipt is immutable")


event.listen(InvoiceNotification, "before_update", _immutable)
event.listen(InvoiceNotification, "before_delete", _immutable)


def _positive_int(value: object, field: str, *, default: int | None = None) -> int:
    if value is None and default is not None:
        value = default
    if type(value) is not int or value <= 0:
        raise ValueError(f"Invoice notification requires a positive {field}")
    return value


def _sha256(value: object) -> str:
    raw = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _event_id(ctx) -> int:
    return _positive_int(getattr(ctx, "event_id", None), "event id")


def _recipient(snapshot: object) -> tuple[str | None, str | None]:
    """Read only the historical buyer snapshot; never query current contacts."""
    if not isinstance(snapshot, dict):
        return None, "recipient_email_missing_or_invalid"
    buyer = snapshot.get("buyer")
    if not isinstance(buyer, dict):
        return None, "recipient_email_missing_or_invalid"
    requisites = buyer.get("requisites")
    raw = requisites.get("email") if isinstance(requisites, dict) else None
    # Legacy document captures flattened buyer requisites into ``buyer``.
    if raw is None:
        raw = buyer.get("email")
    try:
        return address(raw), None
    except (TypeError, ValueError):
        return None, "recipient_email_missing_or_invalid"


def _business_key(event_type: str, payload: dict, document_id: int, version: int, content_sha256: str) -> str:
    if event_type == CANCELLED and isinstance(payload.get("cancellation_id"), str):
        value = payload["cancellation_id"].strip()
        if value:
            key = f"cancelled:{value}"
        else:
            key = f"cancelled:{document_id}:{version}:{content_sha256}"
    elif event_type == CANCELLED:
        key = f"cancelled:{document_id}:{version}:{content_sha256}"
    else:
        key = f"expiring:{document_id}:{version}"
    if len(key) > 128:
        raise ValueError("Invoice notification business key is too long")
    return key


async def _record(payload: dict, ctx, event_type: str) -> InvoiceNotification | None:
    if ctx is None:
        return None
    if not isinstance(payload, dict):
        raise ValueError("Invoice notification payload must be an object")

    from modules.sales.accounting_ownership import DealOwnership
    from modules.sales.models import DealDocument

    event_id = _event_id(ctx)
    document_id = _positive_int(payload.get("document_id"), "document id")
    document = await ctx.session.get(DealDocument, document_id)
    if document is None or document.kind != "invoice":
        raise ValueError("Invoice notification invoice is missing")
    deal_id = _positive_int(payload.get("deal_id"), "deal id", default=document.deal_id)
    if document.deal_id != deal_id:
        raise ValueError("Invoice notification deal does not match the invoice")
    owner = await ctx.session.get(DealOwnership, deal_id)
    if owner is None:
        raise ValueError("Invoice notification requires explicit deal organization ownership")
    organization_id = _positive_int(payload.get("organization_id"), "organization id", default=owner.organization_id)
    if owner.organization_id != organization_id:
        raise ValueError("Invoice notification organization does not match deal ownership")
    version = _positive_int(payload.get("document_version"), "document version", default=document.version)
    content_sha256 = payload.get("content_sha256") or document.content_sha256
    if not isinstance(content_sha256, str) or len(content_sha256) != 64:
        raise ValueError("Invoice notification requires the invoice content digest")
    if document.version != version or document.content_sha256 != content_sha256:
        raise ValueError("Invoice notification does not match the immutable invoice version")
    if payload.get("number") is not None and payload.get("number") != document.number:
        raise ValueError("Invoice notification number does not match the immutable invoice")
    if payload.get("customer_notification") not in (None, "requires_authorized_send"):
        raise ValueError("Invoice notification requires explicit authorized sending")
    if payload.get("valid_until") is not None:
        expected_valid_until = document.valid_until.isoformat() if document.valid_until else None
        if payload.get("valid_until") != expected_valid_until:
            raise ValueError("Invoice notification validity does not match the invoice")
    if event_type == EXPIRING and document.status not in {"issued", "posted"}:
        raise ValueError("Expiring notification requires an active issued invoice")
    if event_type == CANCELLED and document.status != "cancelled":
        raise ValueError("Cancelled notification requires a cancelled invoice")

    recipient, reason = _recipient(document.snapshot_json)
    state = "pending" if recipient else "blocked"
    business_key = _business_key(event_type, payload, document_id, version, content_sha256)
    normalized = {
        "schema_version": 1,
        "event_type": event_type,
        "organization_id": organization_id,
        "deal_id": deal_id,
        "document_id": document_id,
        "document_version": version,
        "content_sha256": content_sha256,
        "number": document.number,
        "valid_until": document.valid_until.isoformat() if document.valid_until else None,
        "expiry_state": payload.get("expiry_state") if event_type == EXPIRING else None,
        "cancellation_id": payload.get("cancellation_id") if event_type == CANCELLED else None,
        "cancellation_digest": payload.get("cancellation_digest") if event_type == CANCELLED else None,
        "release_id": payload.get("release_id") if event_type == CANCELLED else None,
        "recipient": recipient,
        "state": state,
        "reason": reason,
        "customer_notification": payload.get("customer_notification") or "requires_authorized_send",
    }
    request_hash = _sha256(normalized)
    by_event = await ctx.session.scalar(select(InvoiceNotification).where(
        InvoiceNotification.organization_id == organization_id,
        InvoiceNotification.event_id == event_id,
    ))
    by_business = await ctx.session.scalar(select(InvoiceNotification).where(
        InvoiceNotification.organization_id == organization_id,
        InvoiceNotification.business_key == business_key,
    ))
    for existing in (by_event, by_business):
        if existing is not None:
            if existing.request_hash != request_hash:
                raise ValueError("Invoice notification replay has different immutable content")
            return existing

    row = InvoiceNotification(
        organization_id=organization_id,
        deal_id=deal_id,
        document_id=document_id,
        document_version=version,
        event_id=event_id,
        event_type=event_type,
        business_key=business_key,
        request_hash=request_hash,
        channel="email",
        recipient=recipient,
        state=state,
        reason=reason,
        payload=normalized,
    )
    ctx.session.add(row)
    await ctx.session.flush()
    logger.info("Invoice notification receipt %s recorded for document %s", row.business_key, document.number)
    return row


async def on_invoice_expiring_notification(payload: dict, ctx) -> InvoiceNotification | None:
    return await _record(payload, ctx, EXPIRING)


async def on_invoice_cancelled_notification(payload: dict, ctx) -> InvoiceNotification | None:
    return await _record(payload, ctx, CANCELLED)


def summary(row: InvoiceNotification) -> dict:
    return {
        "id": row.id,
        "organization_id": row.organization_id,
        "deal_id": row.deal_id,
        "document_id": row.document_id,
        "document_version": row.document_version,
        "event_id": row.event_id,
        "event_type": row.event_type,
        "business_key": row.business_key,
        "channel": row.channel,
        "recipient": row.recipient,
        "state": row.state,
        "reason": row.reason,
        "payload": row.payload,
        "created_at": row.created_at.isoformat() + "Z" if row.created_at else None,
    }
