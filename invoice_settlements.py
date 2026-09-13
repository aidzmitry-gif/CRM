"""Accountant-confirmed allocations from posted bank entries to exact invoices.

This register is not a cancellation authorization: historical money and shipment
reconciliation must also be completed before enabling cancellation after refund.
"""
from datetime import datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    event,
    func,
    select,
)
from sqlalchemy.orm import Mapped, mapped_column

from core.db.base import Base
from core.runtime.deps import get_core
from core.services.auth import get_current_user
from modules.sales.access import DealAccess, get_deal_access, scope_deals
from modules.sales.accounting_ownership import DealOwnership, context, immutable
from modules.sales.models import Deal, DealDocument


class InvoiceSettlement(Base):
    __tablename__ = "invoice_settlement"
    __table_args__ = (UniqueConstraint("organization_id", "source_key"),
        CheckConstraint("amount > 0", name="invoice_settlement_positive"),
        CheckConstraint("(direction = 'receipt' AND refund_of IS NULL) OR (direction = 'refund' AND refund_of IS NOT NULL)", name="invoice_settlement_direction"),
        {"schema": "sales"})
    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(Integer, index=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("sales.deal_document.id"), index=True)
    source_key: Mapped[str] = mapped_column(String(160))
    bank_entry_id: Mapped[int] = mapped_column(Integer, index=True)
    direction: Mapped[str] = mapped_column(String(10))
    amount: Mapped[Decimal] = mapped_column(Numeric(20, 2))
    refund_of: Mapped[int | None] = mapped_column(ForeignKey("sales.invoice_settlement.id"))
    evidence: Mapped[str] = mapped_column(String(1000))
    snapshot: Mapped[dict] = mapped_column(JSON)
    actor: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


event.listen(InvoiceSettlement, "before_update", immutable)
event.listen(InvoiceSettlement, "before_delete", immutable)


class AllocationInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    source_key: str = Field(min_length=1, max_length=160)
    bank_entry_id: int = Field(gt=0, strict=True)
    amount: Decimal = Field(gt=0, max_digits=20, decimal_places=2, allow_inf_nan=False)
    refund_of: int | None = Field(default=None, gt=0, strict=True)
    evidence: str = Field(min_length=1, max_length=1000)

    @field_validator("amount", mode="before")
    @classmethod
    def exact_amount(cls, value):
        if isinstance(value, (float, bool)):
            raise ValueError("Use an exact decimal string")
        return value


router = APIRouter(tags=["Подтверждённые оплаты и возвраты счетов"])


async def settlement_ready(session, organization_id, doc):
    from modules.sales.invoice_issuance import verified_receipt

    if not (doc is not None and doc.kind == "invoice"
            and doc.status in {"issued", "posted", "paid", "cancelled"}
            and doc.content_sha256 and doc.issued_at and doc.original_html
            and isinstance(doc.snapshot_json, dict) and doc.snapshot_json.get("currency") == "BYN"):
        return False
    try:
        receipt = await verified_receipt(session, doc, organization_id)
    except (ValueError, KeyError, TypeError, ArithmeticError):
        return False
    return doc.status != "issued" or receipt is not None


@router.get("/organizations/{org_id}/invoices")
async def invoices(org_id: int, q: str = Query(default="", max_length=64),
                   after_id: int = Query(default=0, ge=0), limit: int = Query(default=50, ge=1, le=100),
                   ctx=Depends(context), access: DealAccess = Depends(get_deal_access)):
    session, _ = ctx
    query = scope_deals(select(DealDocument).join(Deal, Deal.id == DealDocument.deal_id)
        .join(DealOwnership, DealOwnership.deal_id == Deal.id).where(
            DealOwnership.organization_id == org_id, DealDocument.kind == "invoice",
            DealDocument.id > after_id,
        ), access)
    if q.strip():
        query = query.where(DealDocument.number.contains(q.strip(), autoescape=True))
    rows = (await session.scalars(query.order_by(DealDocument.id).limit(limit + 1))).all()
    items = []
    for doc in rows[:limit]:
        ready = await settlement_ready(session, org_id, doc)
        snapshot = doc.snapshot_json if isinstance(doc.snapshot_json, dict) else {}
        buyer, deal = snapshot.get("buyer"), snapshot.get("deal")
        name = buyer.get("name") if isinstance(buyer, dict) else None
        if not isinstance(name, str) or not name:
            name = deal.get("counterparty") if isinstance(deal, dict) else None
        items.append({"id": doc.id, "number": doc.number, "version": doc.version,
            "deal_id": doc.deal_id, "amount": str(doc.amount), "status": doc.status,
            "currency": snapshot.get("currency") if isinstance(snapshot.get("currency"), str) else None,
            "counterparty": name if isinstance(name, str) else None,
            "issued_at": doc.issued_at, "valid_until": doc.valid_until,
            "superseded_by_id": doc.superseded_by_id, "available_for_settlement": ready,
            "unavailable_reason": None if ready else "Нужен подтверждённый выпущенный оригинал счёта в BYN"})
    return {"items": items, "next_after_id": rows[limit - 1].id if len(rows) > limit else None}


def receipt_query(document_id):
    # Even a partial receipt blocks cancellation until full historical
    # reconciliation is implemented; a status flag is not the money register.
    return select(InvoiceSettlement.id).where(
        InvoiceSettlement.document_id == document_id, InvoiceSettlement.direction == "receipt",
    ).limit(1)


async def invoice(session, org_id, document_id, access):
    deal_id = await session.scalar(select(DealDocument.deal_id).where(DealDocument.id == document_id))
    deal = await session.scalar(scope_deals(select(Deal).where(Deal.id == deal_id), access).with_for_update())
    owner = await session.get(DealOwnership, deal_id) if deal is not None else None
    if owner is None or owner.organization_id != org_id:
        raise HTTPException(404, "Invoice not found in this organization")
    doc = await session.scalar(select(DealDocument).where(DealDocument.id == document_id).with_for_update().execution_options(populate_existing=True))
    if not await settlement_ready(session, org_id, doc):
        raise HTTPException(409, "An issued immutable invoice is required")
    return doc


@router.post("/organizations/{org_id}/invoices/{document_id}/settlements", status_code=201)
async def allocate(org_id: int, document_id: int, data: AllocationInput, ctx=Depends(context),
                   access: DealAccess = Depends(get_deal_access), core=Depends(get_core), user=Depends(get_current_user)):
    session, actor = ctx  # chief authority holds organization lock before deal/bank reads
    doc = await invoice(session, org_id, document_id, access)
    existing = await session.scalar(select(InvoiceSettlement).where(
        InvoiceSettlement.organization_id == org_id, InvoiceSettlement.source_key == data.source_key,
    ))
    if existing is not None:
        if (existing.document_id != document_id or existing.bank_entry_id != data.bank_entry_id
                or existing.amount != data.amount or existing.refund_of != data.refund_of or existing.evidence != data.evidence):
            raise HTTPException(409, "Settlement key already used with different facts")
        return {"id": existing.id, "direction": existing.direction, "amount": str(existing.amount)}
    fact = await core.services.accounting.bank_settlement(session, org_id, user, data.bank_entry_id)
    if fact["settlement_dimensions"].get("settlement_document") != f"sales:document:{document_id}":
        raise HTTPException(409, "Bank settlement must identify this exact invoice")
    used = await session.scalar(select(func.coalesce(func.sum(InvoiceSettlement.amount), 0)).where(
        InvoiceSettlement.organization_id == org_id, InvoiceSettlement.bank_entry_id == data.bank_entry_id,
    ))
    if used + data.amount > Decimal(fact["amount"]):
        raise HTTPException(409, "Bank amount is already allocated")
    if fact["direction"] == "receipt":
        if data.refund_of is not None:
            raise HTTPException(422, "Receipt cannot reference a refunded receipt")
    else:
        receipt = await session.get(InvoiceSettlement, data.refund_of) if data.refund_of else None
        if receipt is None or receipt.organization_id != org_id or receipt.document_id != document_id or receipt.direction != "receipt":
            raise HTTPException(409, "Refund must reference a confirmed receipt of this invoice")
        await core.services.accounting.bank_settlement(session, org_id, user, receipt.bank_entry_id)
        if fact["operation_date"] < receipt.snapshot["bank"]["operation_date"]:
            raise HTTPException(409, "Refund predates its receipt")
        refunded = await session.scalar(select(func.coalesce(func.sum(InvoiceSettlement.amount), 0)).where(InvoiceSettlement.refund_of == receipt.id))
        if refunded + data.amount > receipt.amount:
            raise HTTPException(409, "Refund exceeds the original receipt")
    row = InvoiceSettlement(organization_id=org_id, document_id=document_id,
        **data.model_dump(), direction=fact["direction"], actor=actor,
        snapshot={"bank": fact, "invoice": {"version": doc.version, "content_sha256": doc.content_sha256, "number": doc.number, "amount": str(doc.amount), "currency": "BYN"}})
    session.add(row)
    await session.flush()
    return {"id": row.id, "direction": row.direction, "amount": str(row.amount)}


@router.get("/organizations/{org_id}/invoices/{document_id}/settlements")
async def register(org_id: int, document_id: int, ctx=Depends(context), access: DealAccess = Depends(get_deal_access),
                   core=Depends(get_core), user=Depends(get_current_user)):
    session, _ = ctx
    await invoice(session, org_id, document_id, access)
    rows = (await session.scalars(select(InvoiceSettlement).where(
        InvoiceSettlement.organization_id == org_id, InvoiceSettlement.document_id == document_id,
    ).order_by(InvoiceSettlement.id))).all()
    receipt = refund = Decimal("0")
    for row in rows:
        await core.services.accounting.bank_settlement(session, org_id, user, row.bank_entry_id)
        if row.direction == "receipt":
            receipt += row.amount
        else:
            refund += row.amount
    return {"received": str(receipt), "refunded": str(refund), "net_received": str(receipt - refund),
            "cancellation_authorized": False, "reconciliation_required": True,
            "items": [{"id": r.id, "direction": r.direction, "amount": str(r.amount),
                       "bank_entry_id": r.bank_entry_id, "refund_of": r.refund_of,
                       "evidence": r.evidence, "snapshot": r.snapshot} for r in rows]}


@router.get("/organizations/{org_id}/invoices/{document_id}/money-basis")
async def money_basis(org_id: int, document_id: int, ctx=Depends(context),
                      access: DealAccess = Depends(get_deal_access), core=Depends(get_core),
                      user=Depends(get_current_user)):
    from modules.sales.invoice_money_basis import evaluate_invoice_money_basis

    session, _ = ctx
    doc = await invoice(session, org_id, document_id, access)
    gateway = core.services.accounting
    if not callable(getattr(gateway, "invoice_bank_basis", None)):
        raise HTTPException(503, "Invoice bank evidence service is unavailable")
    basis = await evaluate_invoice_money_basis(session, org_id, user, doc, gateway)
    return {"organization_id": org_id, "document_id": document_id, "digest": basis.digest,
            "money_state": basis.money_state, "received": format(basis.received, ".2f"),
            "refunded": format(basis.refunded, ".2f"), "blockers": basis.blockers,
            "external_requirements": basis.external_requirements,
            "per_receipt_remaining": [{"receipt_id": row.receipt_id,
                "received": format(row.received, ".2f"), "refunded": format(row.refunded, ".2f"),
                "remaining": format(row.remaining, ".2f")} for row in basis.per_receipt_remaining]}
