"""One transaction's cancellation evidence; confirmation and execution are separate."""
import json
from dataclasses import dataclass, replace
from datetime import date, datetime
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import (
    JSON,
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
from core.services.logistics import snapshot_hash
from core.services.wms import EvidenceReference, VerifiedNoShipment, no_shipment_review_digest
from modules.sales.access import get_deal_access_for_user, scope_deals
from modules.sales.accounting_ownership import DealOwnership, context, immutable
from modules.sales.invoice_money_basis import evaluate_invoice_money_basis
from modules.sales.invoice_reconciliation import coverage, require_current, review_blockers
from modules.sales.models import Deal, DealDocument


class CancellationIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    expected_version: int = Field(gt=0, strict=True)
    expected_content_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class ExternalFulfillmentSource(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    system: str = Field(min_length=1, max_length=200)
    reference: str = Field(min_length=1, max_length=1000)
    history_from: date
    history_through: date
    confirmed_no_fulfillment: bool = Field(strict=True)


class FulfillmentReviewInput(CancellationIdentity):
    request_key: UUID
    expected_basis_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    money_reconciliation_id: int = Field(gt=0, strict=True)
    evidence: str = Field(min_length=1, max_length=2000)
    external_sources: list[ExternalFulfillmentSource] = Field(min_length=1, max_length=100)
    all_fulfillment_sources_identified: bool = Field(strict=True)


class SalesFulfillmentReview(Base):
    __tablename__ = "invoice_fulfillment_review"
    __table_args__ = (UniqueConstraint("organization_id", "request_key"), {"schema": "sales"})
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    organization_id: Mapped[int] = mapped_column(Integer, index=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("sales.deal_document.id"), index=True)
    money_reconciliation_id: Mapped[int] = mapped_column(ForeignKey("sales.invoice_money_reconciliation.id"))
    request_key: Mapped[str] = mapped_column(String(36))
    request_hash: Mapped[str] = mapped_column(String(64))
    request: Mapped[dict] = mapped_column(JSON)
    snapshot: Mapped[dict] = mapped_column(JSON)
    digest: Mapped[str] = mapped_column(String(64))
    actor: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


event.listen(SalesFulfillmentReview, "before_update", immutable)
event.listen(SalesFulfillmentReview, "before_delete", immutable)


router = APIRouter(tags=["Аннулирование счетов"])


def service(services, name, method):
    result = getattr(services, name, None)
    if not callable(getattr(result, method, None)):
        raise HTTPException(503, f"Cancellation evidence service unavailable: {name}")
    return result


async def collect_cancellation_basis(session, services, org_id, document_id, identity, user):
    """Caller holds chief authority/org lock; no business writes until collection ends.

    First lock all Sales/Office sources; then money, WMS and accounting evidence;
    finally enter Logistics gate. No source callbacks after the final collection.
    The returned prepared context is internal to the caller's transaction.
    """
    logistics = service(services, "logistics", "prepare_invoice_fulfillment_snapshot")
    accounting = service(services, "accounting", "invoice_fulfillment_snapshot")
    wms = service(services, "wms_reservations", "invoice_fulfillment_snapshot")
    source = service(services, "sales_source", "invoice_shipping_source")
    exact = {"organization_id": org_id, "document_id": document_id,
             **CancellationIdentity.model_validate(identity.model_dump(include={
                 "expected_version", "expected_content_sha256"})).model_dump()}
    prepared = await logistics.prepare_invoice_fulfillment_snapshot(session, exact_invoice=exact, user=user)
    access = await get_deal_access_for_user(session, user)
    doc = await session.scalar(scope_deals(select(DealDocument).join(Deal, Deal.id == DealDocument.deal_id)
        .where(DealDocument.id == document_id), access).execution_options(populate_existing=True))
    if doc is None:
        raise HTTPException(404, "Invoice not found")
    if doc.status == "cancelled" or doc.superseded_by_id is not None:
        raise HTTPException(409, "Invoice is already terminal")
    facts = await source.invoice_shipping_source(session, **exact, operation="historical_claim")
    money = await evaluate_invoice_money_basis(session, org_id, user, doc, accounting)
    start, through = coverage(doc, money)
    stock = await wms.invoice_fulfillment_snapshot(session, org_id, facts)
    ledger = await accounting.invoice_fulfillment_snapshot(session, org_id, user, document_id)
    shipping = await logistics.collect_prepared_invoice_fulfillment_snapshot(session, prepared)
    observed = stock["snapshot"]
    sections = {
        "wms_issue": {"identity": stock["identity"], "observed_state": stock["observed_state"],
            "acts": observed["acts"], "movements": observed["movements"],
            "physical_basis": observed["physical_basis"]},
        "wms_pick": {"identity": stock["identity"], **{key: observed[key] for key in
            ("reservation", "versions", "event_state", "picks", "remaining")}},
        "logistics_shipment": shipping,
        "accounting_issue": ledger,
        "legacy_fulfillment": {"coverage_complete": False,
            "required_history_from": start.isoformat(), "required_history_through": through.isoformat()},
    }
    blockers = review_blockers(money, through)
    if not money.money_conditions_met:
        blockers.append("money_not_fully_refunded_or_unknown")
    if stock["observed_state"] != "no_shipment":
        blockers.append("physical_shipment_requires_return_workflow")
    body = {"schema_version": 1, "exact_invoice": exact,
        "money": {"digest": money.digest, "state": money.money_state,
            "received": str(money.received), "refunded": str(money.refunded),
            "facts": json.loads(money.facts_json)},
        "reservation_digest": stock["identity"]["reservation_digest"],
        "remaining_digest": snapshot_hash(observed["remaining"]),
        "sections": {name: {"facts": value, "sha256": snapshot_hash(value)}
                     for name, value in sections.items()},
        "observed_blockers": sorted(set(blockers)),
        "required_reviews": ["current_complete_money_history", "fulfillment_classification",
                             "named_external_fulfillment_history", "unexecuted_logistics_withdrawal"],
        "cancellation_authorized": False}
    issues = []
    for scope, code, message, check in (
        ("accounting_issue", "accounting_fulfillment_classification_required",
         "Бухгалтерские основания требуют сверки перед отменой.",
         lambda: validate_accounting_classification(body)),
        ("logistics_shipment", "unexecuted_logistics_withdrawal_required",
         "План отгрузки требует отдельного подтверждения остановки перевозки или торгов.",
         lambda: service(services, "logistics", "assert_unexecuted_invoice").assert_unexecuted_invoice(shipping)),
    ):
        try:
            check()
        except HTTPException as exc:
            if exc.status_code != 409:
                raise
            issues.append({"scope": scope, "code": code, "message": message, "detail": exc.detail})
            blockers.append(code)
    body["observed_blockers"] = sorted(set(blockers))
    body["eligibility_issues"] = issues
    return {**body, "basis_digest": snapshot_hash(body)}, {
        "prepared": prepared, "doc": doc, "money": money, "source": facts}


def validate_review_coverage(basis, data):
    if not data.all_fulfillment_sources_identified or not all(s.confirmed_no_fulfillment for s in data.external_sources):
        raise HTTPException(422, "Confirm the named complete external fulfillment history")
    bounds = basis["sections"]["legacy_fulfillment"]["facts"]
    start = date.fromisoformat(bounds["required_history_from"])
    through = date.fromisoformat(bounds["required_history_through"])
    names = [(s.system, s.reference) for s in data.external_sources]
    if len(set(names)) != len(names) or any(s.history_from > start or s.history_through < through
        or s.history_through > date.today() or s.history_from > s.history_through for s in data.external_sources):
        raise HTTPException(409, "External fulfillment history coverage requires a new review")


def validate_accounting_classification(basis):
    ledger = basis["sections"]["accounting_issue"]["facts"]["facts"]
    bank_ids = {r["entry_id"] for r in basis["money"]["facts"]["revalidated_banks"] if "invalid_status" not in r}
    unknown = [entry["id"] for entry in ledger["entries"] if entry["id"] not in bank_ids]
    if unknown or ledger["missing_correction_ids"]:
        raise HTTPException(409, {"code": "accounting_fulfillment_classification_required", "entry_ids": unknown,
            "missing_correction_ids": ledger["missing_correction_ids"]})
    if any(row["entry_id"] is None or row.get("error") for row in ledger["inbox"]) or any(
            row["entry_id"] is None for row in ledger["source_controls"]):
        raise HTTPException(409, "Unprocessed accounting sources require reconciliation")


def review_digest(row):
    return snapshot_hash({"id": row.id, "organization_id": row.organization_id,
        "document_id": row.document_id, "money_reconciliation_id": row.money_reconciliation_id,
        "request_key": row.request_key, "request_hash": row.request_hash, "request": row.request,
        "snapshot": row.snapshot, "actor": row.actor})


def review_output(row):
    if row.digest != review_digest(row) or row.request_hash != snapshot_hash(row.request):
        raise HTTPException(409, "Stored fulfillment review is corrupt")
    return {"review_id": row.id, "review_digest": row.digest, "organization_id": row.organization_id,
        "request_key": row.request_key, "request_hash": row.request_hash,
        "document_version": row.request["expected_version"], "content_sha256": row.request["expected_content_sha256"],
        "document_id": row.document_id, "basis_digest": row.snapshot["basis_digest"],
        "money_reconciliation_id": row.money_reconciliation_id, "actor": row.actor,
        "created_at": row.created_at, "cancellation_authorized": False}


@router.post("/organizations/{org_id}/invoices/{document_id}/fulfillment-review", status_code=201)
async def confirm_review(org_id: int, document_id: int, data: FulfillmentReviewInput, ctx=Depends(context),
                         core=Depends(get_core), user=Depends(get_current_user)):
    session, actor = ctx
    access = await get_deal_access_for_user(session, user)
    visible = await session.scalar(scope_deals(select(DealDocument.id).join(Deal).join(
        DealOwnership, DealOwnership.deal_id == Deal.id).where(DealDocument.id == document_id,
        DealOwnership.organization_id == org_id), access))
    if visible is None:
        raise HTTPException(404, "Invoice not found")
    request = data.model_dump(mode="json")
    existing = await session.scalar(select(SalesFulfillmentReview).where(
        SalesFulfillmentReview.organization_id == org_id, SalesFulfillmentReview.request_key == str(data.request_key))
        .execution_options(populate_existing=True))
    if existing:
        if existing.document_id != document_id or existing.request != request:
            raise HTTPException(409, "Fulfillment review key conflict")
        return review_output(existing)
    basis, current = await collect_cancellation_basis(session, core.services, org_id, document_id, data, user)
    if basis["basis_digest"] != data.expected_basis_digest or basis["observed_blockers"]:
        raise HTTPException(409, "Current invoice facts do not support a no-shipment review")
    await require_current(session, org_id, current["doc"], current["money"], data.money_reconciliation_id)
    validate_review_coverage(basis, data)
    validate_accounting_classification(basis)
    service(core.services, "logistics", "assert_unexecuted_invoice").assert_unexecuted_invoice(
        basis["sections"]["logistics_shipment"]["facts"])
    row = SalesFulfillmentReview(id=str(uuid4()), organization_id=org_id, document_id=document_id,
        money_reconciliation_id=data.money_reconciliation_id, request_key=str(data.request_key),
        request_hash=snapshot_hash(request), request=request, snapshot=basis, actor=actor)
    row.digest = review_digest(row)
    session.add(row)
    await session.flush()
    await session.refresh(row, attribute_names=["created_at"])
    return review_output(row)


@router.post("/organizations/{org_id}/invoices/{document_id}/cancellation/preview")
async def preview(org_id: int, document_id: int, data: CancellationIdentity, ctx=Depends(context),
                  core=Depends(get_core), user=Depends(get_current_user)):
    result, _ = await collect_cancellation_basis(ctx[0], core.services, org_id, document_id, data, user)
    return result


@dataclass(frozen=True)
class PreparedFulfillmentVerification:
    """Internal one-use proof scoped to the collecting root transaction.

    Caller holds all source locks and may only withdraw the reviewed Logistics
    plans before invoking WMS release. No money/source/physical edits in between.
    This object is never decoded from a user request or stored for later reuse.
    """
    session: Any
    transaction: Any
    source_json: str
    stored_review_digest: str
    review_date: date
    proof: VerifiedNoShipment
    _consumed: bool = False

    async def verify_no_shipment(self, session, organization_id, source, review_id, expected_review_digest):
        if (self._consumed or session is not self.session or session.in_nested_transaction()
                or session.get_transaction() is not self.transaction or not self.transaction.is_active
                or organization_id != self.proof.organization_id or review_id != self.proof.review_id
                or expected_review_digest != self.proof.review_digest
                or json.loads(self.source_json) != source):
            raise HTTPException(409, "Fulfillment verification requires its original transaction and source")
        object.__setattr__(self, "_consumed", True)
        row = await session.scalar(select(SalesFulfillmentReview).where(
            SalesFulfillmentReview.id == review_id,
            SalesFulfillmentReview.organization_id == organization_id,
            SalesFulfillmentReview.document_id == source["document_id"],
        ).execution_options(populate_existing=True))
        if (date.today() != self.review_date or row is None
                or review_output(row)["review_digest"] != self.stored_review_digest):
            raise HTTPException(409, "Confirmed fulfillment evidence changed")
        return self.proof


async def prepare_fulfillment_verifier(session, services, org_id, basis, current, review_id, expected_digest):
    """Dereference saved review against freshly collected facts before mutations.

    Called only by the cancellation coordinator after collect_cancellation_basis;
    no source service calls/locks after the already-held Logistics gate.
    """
    review_date = date.today()
    prepared = current["prepared"]
    transaction = session.get_transaction()
    if (transaction is None or not transaction.is_active or session.in_nested_transaction()
            or prepared.session is not session or prepared.transaction is not transaction
            or not prepared._consumed or session.new or session.dirty or session.deleted):
        raise HTTPException(409, "Fulfillment review requires a clean collected transaction")
    row = await session.scalar(select(SalesFulfillmentReview).where(
        SalesFulfillmentReview.id == review_id, SalesFulfillmentReview.organization_id == org_id,
        SalesFulfillmentReview.document_id == current["doc"].id,
    ).execution_options(populate_existing=True))
    if (row is None or review_output(row)["review_digest"] != expected_digest or row.snapshot != basis
            or basis["observed_blockers"]):
        raise HTTPException(409, "A current confirmed fulfillment review is required")
    data = FulfillmentReviewInput.model_validate(row.request)
    await require_current(session, org_id, current["doc"], current["money"], row.money_reconciliation_id)
    validate_review_coverage(basis, data)
    validate_accounting_classification(basis)
    service(services, "logistics", "assert_unexecuted_invoice").assert_unexecuted_invoice(
        basis["sections"]["logistics_shipment"]["facts"])
    refs = []
    for name, section in sorted(row.snapshot["sections"].items()):
        # External coverage is an explicit saved attestation, not empty local facts.
        digest = section["sha256"] if name != "legacy_fulfillment" else snapshot_hash({
            "observed": section, "sources": row.request["external_sources"],
            "all_sources_identified": row.request["all_fulfillment_sources_identified"],
            "evidence": row.request["evidence"]})
        refs.append(EvidenceReference(name, row.id + ":" + name, row.digest, digest))
    source = current["source"]
    proof = VerifiedNoShipment(org_id, source["document_id"], source["version"], source["content_sha256"],
        basis["reservation_digest"], row.id, "", row.actor, tuple(refs))
    proof = replace(proof, review_digest=no_shipment_review_digest(proof))
    if date.today() != review_date:
        raise HTTPException(409, "Fulfillment history requires review for the new day")
    return PreparedFulfillmentVerification(session, transaction,
        json.dumps(source, ensure_ascii=False, sort_keys=True, allow_nan=False), row.digest, review_date, proof)


class CancellationInput(CancellationIdentity):
    request_key: UUID
    fulfillment_review_id: UUID
    expected_review_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    evidence: str = Field(min_length=1, max_length=1000)
    acknowledge_invoice_invalidation: bool = Field(strict=True)


class InvoiceCancellationReceipt(Base):
    __tablename__ = "invoice_cancellation_receipt"
    __table_args__ = (UniqueConstraint("organization_id", "request_key"), {"schema": "sales"})
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    organization_id: Mapped[int] = mapped_column(Integer, index=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("sales.deal_document.id"), unique=True)
    document_version: Mapped[int] = mapped_column(Integer)
    content_sha256: Mapped[str] = mapped_column(String(64))
    request_key: Mapped[str] = mapped_column(String(36))
    request_hash: Mapped[str] = mapped_column(String(64))
    request: Mapped[dict] = mapped_column(JSON)
    fulfillment_review_id: Mapped[str] = mapped_column(ForeignKey("sales.invoice_fulfillment_review.id"))
    release_id: Mapped[int] = mapped_column(ForeignKey("wms.invoice_reservation_release.id"), unique=True)
    snapshot: Mapped[dict] = mapped_column(JSON)
    digest: Mapped[str] = mapped_column(String(64))
    actor: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


event.listen(InvoiceCancellationReceipt, "before_update", immutable)
event.listen(InvoiceCancellationReceipt, "before_delete", immutable)


def cancellation_digest(row):
    return snapshot_hash({"id": row.id, "organization_id": row.organization_id,
        "document_id": row.document_id, "document_version": row.document_version,
        "content_sha256": row.content_sha256, "request_key": row.request_key,
        "request_hash": row.request_hash, "request": row.request,
        "fulfillment_review_id": row.fulfillment_review_id, "release_id": row.release_id,
        "snapshot": row.snapshot, "actor": row.actor})


def cancellation_output(row):
    if row.digest != cancellation_digest(row) or row.request_hash != snapshot_hash(row.request):
        raise HTTPException(409, "Stored cancellation receipt is corrupt")
    return {"cancellation_id": row.id, "digest": row.digest,
        "request_key": row.request_key, "request_hash": row.request_hash,
        "document_version": row.document_version, "content_sha256": row.content_sha256,
        "organization_id": row.organization_id, "document_id": row.document_id,
        "status": "cancelled", "reserve_status": "released", "release_id": row.release_id,
        "fulfillment_review_id": row.fulfillment_review_id, "actor": row.actor,
        "created_at": row.created_at, "customer_notification": "not_sent"}


@router.post("/organizations/{org_id}/invoices/{document_id}/cancel", status_code=201)
async def cancel(org_id: int, document_id: int, data: CancellationInput, ctx=Depends(context),
                 core=Depends(get_core), user=Depends(get_current_user)):
    session, actor = ctx
    if not data.acknowledge_invoice_invalidation:
        raise HTTPException(422, "Acknowledge that the invoice will no longer be valid")
    access = await get_deal_access_for_user(session, user)
    visible = await session.scalar(scope_deals(select(DealDocument.id).join(Deal).join(
        DealOwnership, DealOwnership.deal_id == Deal.id).where(DealDocument.id == document_id,
        DealOwnership.organization_id == org_id), access))
    if visible is None:
        raise HTTPException(404, "Invoice not found")
    request = data.model_dump(mode="json")
    rows = (await session.scalars(select(InvoiceCancellationReceipt).where(
        (InvoiceCancellationReceipt.document_id == document_id) |
        ((InvoiceCancellationReceipt.organization_id == org_id) &
         (InvoiceCancellationReceipt.request_key == str(data.request_key))))
        .execution_options(populate_existing=True))).all()
    if rows:
        if len(rows) != 1 or rows[0].organization_id != org_id or rows[0].document_id != document_id or rows[0].request != request:
            raise HTTPException(409, "Cancellation key or invoice already used")
        return cancellation_output(rows[0])
    basis, current = await collect_cancellation_basis(session, core.services, org_id, document_id, data, user)
    verifier = await prepare_fulfillment_verifier(session, core.services, org_id, basis, current,
        str(data.fulfillment_review_id), data.expected_review_digest)
    cancellation_id = str(uuid4())
    shipping = basis["sections"]["logistics_shipment"]["facts"]
    withdrawal = await core.services.logistics.withdraw_unexecuted_invoice(session,
        prepared=current["prepared"], current_snapshot=shipping, expected_digest=shipping["sha256"],
        cancel_receipt_identity={"exact_invoice": basis["exact_invoice"],
            "cancellation_receipt_id": cancellation_id, "cancellation_request_sha256": snapshot_hash(request)})
    release = await core.services.wms_reservations.release_invoice(session, org_id, current["source"], {
        "source_key": cancellation_id, "expected_reservation_digest": basis["reservation_digest"],
        "expected_remaining_digest": basis["remaining_digest"],
        "fulfillment_review_id": verifier.proof.review_id,
        "fulfillment_review_digest": verifier.proof.review_digest, "evidence": data.evidence,
    }, actor, verifier)
    doc = current["doc"]
    row = InvoiceCancellationReceipt(id=cancellation_id, organization_id=org_id, document_id=document_id,
        document_version=doc.version, content_sha256=doc.content_sha256, request_key=str(data.request_key),
        request_hash=snapshot_hash(request), request=request,
        fulfillment_review_id=str(data.fulfillment_review_id), release_id=release["release_id"],
        snapshot={"before_status": doc.status, "before_reserve_status": doc.reserve_status,
            "basis_digest": basis["basis_digest"], "review_digest": data.expected_review_digest,
            "money": basis["money"], "withdrawal": withdrawal, "release": release}, actor=actor)
    row.digest = cancellation_digest(row)
    session.add(row)
    await session.flush()  # receipt exists before narrow database status exception
    doc.status, doc.reserve_status = "cancelled", "released"
    core.event_bus.emit(session, "sales.invoice.cancelled", {"organization_id": org_id,
        "document_id": document_id, "document_version": doc.version, "content_sha256": doc.content_sha256,
        "cancellation_id": row.id, "cancellation_digest": row.digest, "release_id": row.release_id,
        "customer_notification": "requires_authorized_send", "by": actor})
    await session.flush()
    await session.refresh(row, attribute_names=["created_at"])
    return cancellation_output(row)



def orm_cancellation_receipt(connection, doc):
    """Narrow ORM equivalent: durable receipt, never a mutable session flag."""
    from types import SimpleNamespace

    from modules.wms.invoice_reservations import InvoiceReservationRelease
    saved = connection.execute(select(InvoiceCancellationReceipt.__table__).where(
        InvoiceCancellationReceipt.document_id == doc.id,
        InvoiceCancellationReceipt.document_version == doc.version,
        InvoiceCancellationReceipt.content_sha256 == doc.content_sha256,
    )).mappings().one_or_none()
    if saved is None or doc.reserve_status != "released":
        return False
    row = SimpleNamespace(**saved)
    cancellation_output(row)
    release = connection.execute(select(InvoiceReservationRelease.__table__).where(
        InvoiceReservationRelease.id == row.release_id,
        InvoiceReservationRelease.organization_id == row.organization_id,
        InvoiceReservationRelease.document_id == doc.id,
        InvoiceReservationRelease.source_key == row.id,
    )).mappings().one_or_none()
    return release is not None and release["digest"] == row.snapshot["release"]["digest"]
