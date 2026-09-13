"""Immutable order envelopes and explicit invoice associations; caller owns commit."""
from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, UniqueConstraint, event, func
from sqlalchemy.orm import Mapped, mapped_column

from core.db.base import Base


class ShippingEnvelope(Base):
    __tablename__ = "shipping_envelope"
    __table_args__ = (
        UniqueConstraint("order_document_id", "order_version", name="uq_sales_shipping_envelope_source"),
        {"schema": "sales"},
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    order_document_id: Mapped[int] = mapped_column(ForeignKey("sales.deal_document.id"))
    order_version: Mapped[int] = mapped_column(Integer)
    source_sha256: Mapped[str] = mapped_column(String(64))
    request_key: Mapped[str] = mapped_column(String(64), unique=True)
    payload: Mapped[dict] = mapped_column(JSON)
    payload_sha256: Mapped[str] = mapped_column(String(64))
    actor: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class OrderInvoiceAssociation(Base):
    __tablename__ = "order_invoice_association"
    __table_args__ = {"schema": "sales"}
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    envelope_id: Mapped[str] = mapped_column(ForeignKey("sales.shipping_envelope.id"), unique=True)
    organization_id: Mapped[int] = mapped_column(Integer, index=True)
    deal_id: Mapped[int] = mapped_column(ForeignKey("sales.deal.id"))
    exact_invoice: Mapped[dict] = mapped_column(JSON)
    execution_id: Mapped[str] = mapped_column(String(36))
    intent_digest: Mapped[str] = mapped_column(String(64))
    request_key: Mapped[str] = mapped_column(String(64), unique=True)
    confirmation_sha256: Mapped[str] = mapped_column(String(64))
    evidence_refs: Mapped[dict] = mapped_column(JSON)
    actor: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


def immutable(*args):
    raise ValueError("Shipping envelope and association history is immutable")


for _model in (ShippingEnvelope, OrderInvoiceAssociation):
    event.listen(_model, "before_update", immutable)
    event.listen(_model, "before_delete", immutable)
