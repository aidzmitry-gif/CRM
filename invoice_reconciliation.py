"""Chief attestations of complete money history, bound to observed invoice facts.

An attestation is evidence, not permission to cancel. The cancellation transaction
must call require_current and additionally prove fulfillment and release stock.
"""
import hashlib
import json
from datetime import date, datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import (
    JSON,
    Date,
    DateTime,
    ForeignKey,
    Integer,
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
from modules.sales.access import DealAccess, get_deal_access
from modules.sales.accounting_ownership import context, immutable
from modules.sales.invoice_money_basis import canonical, evaluate_invoice_money_basis
from modules.sales.invoice_settlements import invoice


class InvoiceMoneyReconciliation(Base):
    __tablename__ = "invoice_money_reconciliation"
    __table_args__ = (UniqueConstraint("organization_id", "source_key"), {"schema": "sales"})
    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(Integer, index=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("sales.deal_document.id"), index=True)
    source_key: Mapped[str] = mapped_column(String(160))
    basis_digest: Mapped[str] = mapped_column(String(64))
    history_from: Mapped[date] = mapped_column(Date)
    history_through: Mapped[date] = mapped_column(Date)
    request: Mapped[dict] = mapped_column(JSON)
    facts: Mapped[dict] = mapped_column(JSON)
    actor: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


event.listen(InvoiceMoneyReconciliation, "before_update", immutable)
event.listen(InvoiceMoneyReconciliation, "before_delete", immutable)


class ReconciliationInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    source_key: str = Field(min_length=1, max_length=160)
    expected_basis_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    history_from: date
    history_through: date
    evidence: str = Field(min_length=1, max_length=2000)
    source_references: list[str] = Field(min_length=1, max_length=100)
    all_money_sources_checked: bool = Field(strict=True)

    @field_validator("source_references")
    @classmethod
    def references(cls, values):
        result = [value.strip() for value in values]
        if any(not value or len(value) > 500 for value in result) or len(set(result)) != len(result):
            raise ValueError("Distinct nonempty documentary references are required")
        return result


router = APIRouter(tags=["Сверка денежной истории счёта"])


def coverage(doc, basis):
    facts = json.loads(basis.facts_json)
    dates = [doc.issued_at.date()]
    dates += [date.fromisoformat(bank["operation_date"]) for bank in facts["revalidated_banks"]
              if "operation_date" in bank]
    return min(dates), max(date.today(), *dates)


def output(row):
    return {"id": row.id, "document_id": row.document_id, "organization_id": row.organization_id,
            "source_key": row.source_key, "request_hash": hashlib.sha256(canonical(row.request).encode()).hexdigest(),
            "basis_digest": row.basis_digest, "history_from": row.history_from,
            "history_through": row.history_through, "actor": row.actor, "created_at": row.created_at,
            "evidence": row.request["evidence"], "source_references": row.request["source_references"]}


def review_blockers(basis, through):
    return [*basis.blockers, *(["future_money_history_requires_review"] if through > date.today() else [])]


async def require_current(session, organization_id, doc, basis, reconciliation_id):
    """Called by cancellation under the same org/deal/document transaction locks."""
    row = await session.scalar(select(InvoiceMoneyReconciliation).where(
        InvoiceMoneyReconciliation.id == reconciliation_id,
        InvoiceMoneyReconciliation.organization_id == organization_id,
        InvoiceMoneyReconciliation.document_id == doc.id,
    ).execution_options(populate_existing=True))
    start, through = coverage(doc, basis)
    if (row is None or row.basis_digest != basis.digest or row.facts != json.loads(basis.facts_json)
            or not basis.money_conditions_met or review_blockers(basis, through)
            or row.history_through > date.today()
            or row.history_from > start or row.history_through < through):
        raise HTTPException(409, "A current confirmed complete money reconciliation is required")
    return row


@router.get("/organizations/{org_id}/invoices/{document_id}/money-reconciliation")
async def preview(org_id: int, document_id: int, ctx=Depends(context),
                  access: DealAccess = Depends(get_deal_access), core=Depends(get_core), user=Depends(get_current_user)):
    session, _ = ctx
    doc = await invoice(session, org_id, document_id, access)
    basis = await evaluate_invoice_money_basis(session, org_id, user, doc, core.services.accounting)
    start, through = coverage(doc, basis)
    blockers = review_blockers(basis, through)
    rows = (await session.scalars(select(InvoiceMoneyReconciliation).where(
        InvoiceMoneyReconciliation.organization_id == org_id,
        InvoiceMoneyReconciliation.document_id == document_id,
    ).order_by(InvoiceMoneyReconciliation.id.desc()).limit(20))).all()
    return {"organization_id": org_id, "document_id": document_id, "review_date": date.today(),
            "basis_digest": basis.digest, "money_state": basis.money_state, "blockers": blockers,
            "required_history_from": start, "required_history_through": through,
            "can_confirm_money_history": basis.money_conditions_met and not blockers,
            "fulfillment_required": True,
            "records": [{**output(row), "current": row.basis_digest == basis.digest
                and row.history_from <= start and row.history_through >= through
                and row.history_through <= date.today()
                and basis.money_conditions_met and not blockers} for row in rows]}


@router.post("/organizations/{org_id}/invoices/{document_id}/money-reconciliation", status_code=201)
async def confirm(org_id: int, document_id: int, data: ReconciliationInput, ctx=Depends(context),
                  access: DealAccess = Depends(get_deal_access), core=Depends(get_core), user=Depends(get_current_user)):
    session, actor = ctx
    doc = await invoice(session, org_id, document_id, access)
    request = data.model_dump(mode="json")
    existing = await session.scalar(select(InvoiceMoneyReconciliation).where(
        InvoiceMoneyReconciliation.organization_id == org_id,
        InvoiceMoneyReconciliation.source_key == data.source_key,
    ))
    if existing is not None:
        if existing.document_id != document_id or canonical(existing.request) != canonical(request):
            raise HTTPException(409, "Reconciliation key already used with different facts")
        # Receipt of an earlier attestation, not a promise that it remains current.
        return output(existing)
    basis = await evaluate_invoice_money_basis(session, org_id, user, doc, core.services.accounting)
    start, through = coverage(doc, basis)
    if not data.all_money_sources_checked:
        raise HTTPException(422, "Confirm review of bank, cash and legacy money history")
    if (basis.digest != data.expected_basis_digest or not basis.money_conditions_met or basis.blockers
            or data.history_from > start or data.history_through < through
            or data.history_through > date.today()
            or data.history_from > data.history_through):
        raise HTTPException(409, "Money facts or history coverage require a new reconciliation")
    row = InvoiceMoneyReconciliation(organization_id=org_id, document_id=document_id,
        source_key=data.source_key, basis_digest=basis.digest, history_from=data.history_from,
        history_through=data.history_through, request=request, facts=json.loads(basis.facts_json), actor=actor)
    session.add(row)
    await session.flush()
    await session.refresh(row, attribute_names=["created_at"])
    return output(row)
