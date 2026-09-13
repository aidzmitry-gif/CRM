"""Durable request-to-close saga. No invoice cancellation or external delivery.

Commands serialize organization -> deal -> ALL invoices (ascending ID). Terminal
receipts are immutable; request.state is only a projection of its resolution.
PostgreSQL trigger contract is in deal_loss_guards.sql (installed by integration).
"""
import hashlib
import json
from datetime import date, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import JSON, DateTime, ForeignKey, Index, String, event, func, inspect, select, text
from sqlalchemy.orm import Mapped, mapped_column

from core.db.base import Base
from core.runtime.deps import get_core, get_session
from core.services.auth import (
    EffectiveIdentityLookupError,
    has_permission,
    require_permission,
    resolve_effective_oidc_user,
)
from core.services.logistics import snapshot_hash
from modules.sales.access import get_deal_access, get_deal_access_for_user, visible_deal_or_404
from modules.sales.accounting_ownership import DealOwnership, immutable
from modules.sales.documents import lock_deal
from modules.sales.models import Deal, DealDocument, LossReason, Stage
from modules.sales.repository import record_stage
from modules.sales.stages import canonical_stages


class DealLossRequest(Base):
    __tablename__ = "deal_loss_request"
    __table_args__ = (
        Index("uq_deal_loss_pending", "deal_id", unique=True,
              postgresql_where=text("state = 'pending'"), sqlite_where=text("state = 'pending'")),
        {"schema": "sales"},
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("accounting.organization.id"))
    deal_id: Mapped[int] = mapped_column(ForeignKey("sales.deal.id"))
    command: Mapped[dict] = mapped_column(JSON)
    command_hash: Mapped[str] = mapped_column(String(64))
    snapshot: Mapped[dict] = mapped_column(JSON)
    digest: Mapped[str] = mapped_column(String(64))
    actor: Mapped[str] = mapped_column(String(200))
    state: Mapped[str] = mapped_column(String(16), default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class DealLossResolution(Base):
    __tablename__ = "deal_loss_resolution"
    __table_args__ = {"schema": "sales"}
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    request_id: Mapped[str] = mapped_column(ForeignKey("sales.deal_loss_request.id"), unique=True)
    action: Mapped[str] = mapped_column(String(16))
    command: Mapped[dict] = mapped_column(JSON)
    command_hash: Mapped[str] = mapped_column(String(64))
    snapshot: Mapped[dict] = mapped_column(JSON)
    digest: Mapped[str] = mapped_column(String(64))
    actor: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


event.listen(DealLossRequest, "before_delete", immutable)
event.listen(DealLossResolution, "before_update", immutable)
event.listen(DealLossResolution, "before_delete", immutable)


def request_digest(row):
    return snapshot_hash({k: getattr(row, k) for k in (
        "id", "organization_id", "deal_id", "command", "command_hash", "snapshot", "actor")})


def resolution_digest(row):
    return snapshot_hash({k: getattr(row, k) for k in (
        "id", "request_id", "action", "command", "command_hash", "snapshot", "actor")})


@event.listens_for(DealLossRequest, "before_update")
def guard_request(mapper, connection, row):
    changed = {a.key for a in inspect(row).attrs if a.history.has_changes()}
    resolution = connection.execute(select(DealLossResolution.__table__).where(
        DealLossResolution.request_id == row.id)).mappings().one_or_none()
    if (changed - {"state"} or inspect(row).attrs.state.history.deleted != ["pending"]
            or resolution is None or resolution["action"] != row.state):
        raise ValueError("Loss request command is immutable; state requires a resolution")


async def pending(session, deal_id):
    return await session.scalar(select(DealLossRequest).where(
        DealLossRequest.deal_id == deal_id, DealLossRequest.state == "pending")
        .execution_options(populate_existing=True))


async def assert_not_pending(session, deal_id):
    if await pending(session, deal_id):
        raise HTTPException(409, "Deal loss request is pending; resolve or withdraw it first")


async def kind_map(session, funnel):
    rows = (await session.execute(select(Stage.code, Stage.kind).where(
        Stage.funnel == funnel, Stage.is_active))).all()
    return dict(rows) if rows else {s["code"]: s["kind"] for s in canonical_stages() if s["funnel"] == funnel}


async def current_writer(session, core, user):
    try:
        user = await resolve_effective_oidc_user(user, session)
    except EffectiveIdentityLookupError as exc:
        raise HTTPException(503, "Current identity cannot be verified") from exc
    if user.local_status not in (None, "active") or not has_permission(core, user, "sales.deal.write"):
        raise HTTPException(403, "Current Sales write permission is required")
    return user, await get_deal_access_for_user(session, user)


async def lock_mutation(session, deal_id, access, core, user):
    """Generic stage writers take the same lock order without requiring book access."""
    await visible_deal_or_404(session, deal_id, access)
    owner = await session.get(DealOwnership, deal_id)
    if owner and core is not None:
        await core.services.accounting.lock_event_organization(session, owner.organization_id)
    await lock_deal(session, deal_id)
    user, access = await current_writer(session, core, user)
    deal = await visible_deal_or_404(session, deal_id, access)
    await session.refresh(deal)
    return deal, access, user


async def guard_patch(session, deal, data):
    target = (data.get("funnel") or deal.funnel, data.get("stage") or deal.stage)
    if target != (deal.funnel, deal.stage):
        await assert_not_pending(session, deal.id)
        if (await kind_map(session, target[0])).get(target[1]) == "lost":
            raise HTTPException(409, "Lost stage requires the deal loss coordinator")


async def guard_stage_definition(session, stage, changes):
    # Even deactivation/activation can switch a funnel between configured and
    # canonical semantics. Freeze semantic edits in populated funnels.
    semantic = {"kind", "funnel", "code", "is_active"}
    if any(k in changes and changes[k] != getattr(stage, k, None) for k in semantic):
        funnels = {stage.funnel, changes.get("funnel", stage.funnel)}
        if await session.scalar(select(Deal.id).where(Deal.funnel.in_(funnels)).limit(1)):
            raise HTTPException(409, "Populated funnel semantics require an explicit migration")


IDENTITY = ("id", "deal_id", "kind", "version", "content_sha256", "supersedes_id", "superseded_by_id")


def composition(docs):
    return [{k: getattr(d, k) for k in IDENTITY} for d in docs]


async def context(session, core, user, access, deal_id, org):
    actor = await core.services.accounting.source_member(session, org, user)
    await visible_deal_or_404(session, deal_id, access)
    await lock_deal(session, deal_id)
    user, access = await current_writer(session, core, user)
    actor = await core.services.accounting.source_member(session, org, user)
    deal = await visible_deal_or_404(session, deal_id, access)
    await session.refresh(deal)
    owner = await session.scalar(select(DealOwnership).where(DealOwnership.deal_id == deal_id)
        .execution_options(populate_existing=True))
    if owner is None or owner.organization_id != org:
        raise HTTPException(409, "Exact deal organization mapping is required")
    docs = (await session.scalars(select(DealDocument).where(
        DealDocument.deal_id == deal_id, DealDocument.kind == "invoice").order_by(DealDocument.id)
        .with_for_update().execution_options(populate_existing=True))).all()
    return deal, docs, actor, user


async def invoice_progress(session, core, user, org, doc):
    from modules.sales.invoice_cancellation import (
        InvoiceCancellationReceipt,
        SalesFulfillmentReview,
        cancellation_output,
        orm_cancellation_receipt,
        review_output,
    )
    from modules.sales.invoice_money_basis import canonical, evaluate_invoice_money_basis
    from modules.sales.invoice_reconciliation import InvoiceMoneyReconciliation

    result = {"document_id": doc.id, "status": doc.status, "reserve_status": doc.reserve_status,
              "ready": False, "blockers": [], "cancellation_receipt": None}
    try:
        money = await evaluate_invoice_money_basis(session, org, user, doc, core.services.accounting)
        result["money"] = {"state": money.money_state, "digest": money.digest,
                           "received": str(money.received), "refunded": str(money.refunded)}
        if not money.money_conditions_met:
            result["blockers"].append("funds_not_fully_refunded_or_history_unknown")
        if doc.status != "cancelled" or doc.reserve_status != "released":
            result["blockers"].append("chief_invoice_cancellation_required")
            return result
        receipt = await session.scalar(select(InvoiceCancellationReceipt).where(
            InvoiceCancellationReceipt.document_id == doc.id).execution_options(populate_existing=True))
        if receipt is None or receipt.organization_id != org:
            raise ValueError("Missing exact cancellation receipt")
        saved = cancellation_output(receipt)
        if not await session.run_sync(lambda s: orm_cancellation_receipt(s.connection(), doc)):
            raise ValueError("Missing linked reservation release")
        review = await session.get(SalesFulfillmentReview, receipt.fulfillment_review_id,
                                   populate_existing=True)
        if (review is None or review.organization_id != org or review.document_id != doc.id
                or review_output(review)["review_digest"] != receipt.snapshot["review_digest"]
                or review.request["expected_version"] != doc.version
                or review.request["expected_content_sha256"] != doc.content_sha256):
            raise ValueError("Missing exact chief fulfillment review")
        # The evaluator includes invoice_status and a cancellation-only reminder
        # in its digest. Normalize exactly that proven terminal transition; all
        # bank facts/allocations/amounts and other blockers must remain identical.
        facts = json.loads(money.facts_json)
        facts["invoice_status"] = receipt.snapshot["before_status"]
        facts["blockers"] = [b for b in facts["blockers"] if b != "cancelled_invoice_money_review_required"]
        if facts != receipt.snapshot["money"]["facts"]:
            raise ValueError("Money evidence changed after cancellation")
        frozen = canonical(facts)
        reconciliation = await session.get(InvoiceMoneyReconciliation, review.money_reconciliation_id,
                                           populate_existing=True)
        start = min([doc.issued_at.date(), *[date.fromisoformat(b["operation_date"])
                    for b in facts["revalidated_banks"] if "operation_date" in b]])
        # A durable terminal cancellation is not invalidated just by midnight.
        # Preserve the chief's coverage through cancellation; fresh bank facts
        # above must still match exactly, including zero-net later activity.
        if (reconciliation is None or reconciliation.organization_id != org
                or reconciliation.document_id != doc.id or reconciliation.facts != facts
                or reconciliation.basis_digest != hashlib.sha256(frozen.encode()).hexdigest()
                or reconciliation.request.get("all_money_sources_checked") is not True
                or reconciliation.history_from > start
                or reconciliation.history_through < receipt.created_at.date()
                or facts["blockers"]):
            raise ValueError("Missing complete chief money review at cancellation")
        # Source-owned historical replay verifies release lines/latest zero reserve
        # and canceled pick tasks. The persisted link above is mandatory BEFORE
        # this call, so there is no new release or cancellation authorization.
        source = await core.services.sales_source.invoice_reservation(session, doc.id)
        release = await core.services.wms_reservations.release_invoice(session, org, source, {
            "source_key": receipt.id, "expected_reservation_digest": review.snapshot["reservation_digest"],
            "expected_remaining_digest": review.snapshot["remaining_digest"],
            "fulfillment_review_id": review.id,
            "fulfillment_review_digest": receipt.snapshot["release"]["snapshot"]["fulfillment"]["digest"],
            "evidence": receipt.request["evidence"],
        }, receipt.actor, None)
        if release != receipt.snapshot["release"]:
            raise ValueError("Release receipt mismatch")
        result["cancellation_receipt"] = {k: saved[k] for k in (
            "cancellation_id", "digest", "document_version", "content_sha256", "release_id", "fulfillment_review_id")}
        result["ready"] = not result["blockers"]
    except HTTPException as exc:
        if exc.status_code != 409:
            raise
        result["blockers"].append(str(exc.detail))
    except (ValueError, KeyError, TypeError, ArithmeticError):
        result["blockers"].append("missing_or_inconsistent_invoice_history")
    return result


async def progress(session, core, user, row, deal, docs):
    if row.command_hash != snapshot_hash(row.command) or row.digest != request_digest(row):
        raise HTTPException(409, "Stored loss request is corrupt")
    current = composition(docs)
    if current != row.snapshot["invoices"]:
        raise HTTPException(409, "Frozen invoice composition changed; reconciliation required")
    invoices = [await invoice_progress(session, core, user, row.organization_id, d) for d in docs]
    return {"request_id": row.id, "request_digest": row.digest, "state": row.state,
        "organization_id": row.organization_id, "deal_id": row.deal_id,
        "invoices": invoices, "ready_to_finalize": row.state == "pending" and all(x["ready"] for x in invoices),
        "customer_notification": "not_sent"}


def resolution_output(row):
    if row.command_hash != snapshot_hash(row.command) or row.digest != resolution_digest(row):
        raise HTTPException(409, "Stored loss resolution is corrupt")
    return {"resolution_id": row.id, "request_id": row.request_id, "action": row.action,
            "command": row.command, "command_hash": row.command_hash, "digest": row.digest,
            "snapshot": row.snapshot, "actor": row.actor, "customer_notification": "not_sent"}


class ResolveInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    request_key: UUID
    expected_request_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    evidence: str = Field(min_length=1, max_length=2000)


async def resolve(session, core, user, row, deal, docs, actor, data, action):
    if row.digest != request_digest(row) or row.command_hash != snapshot_hash(row.command):
        raise HTTPException(409, "Stored loss request is corrupt")
    command = data.model_dump(mode="json")
    prior = await session.scalar(select(DealLossResolution).where(
        (DealLossResolution.id == str(data.request_key)) | (DealLossResolution.request_id == row.id)))
    if prior:
        if prior.request_id != row.id or prior.action != action or prior.command != command:
            raise HTTPException(409, "Loss resolution key or request already used")
        return resolution_output(prior)
    if row.digest != data.expected_request_digest or row.state != "pending":
        raise HTTPException(409, "Pending exact loss request required")
    if composition(docs) != row.snapshot["invoices"]:
        raise HTTPException(409, "Frozen invoice composition changed; reconciliation required")
    status = (await progress(session, core, user, row, deal, docs) if action == "finalized" else {
        "invoices": [{"document_id": d.id, "status": d.status, "reserve_status": d.reserve_status} for d in docs]})
    if action == "finalized" and not status["ready_to_finalize"]:
        raise HTTPException(409, detail=status)
    if (deal.funnel, deal.stage) != (row.snapshot["funnel"], row.snapshot["stage"]):
        raise HTTPException(409, "Deal stage changed during pending request")
    if action == "finalized" and (await kind_map(session, deal.funnel)).get(row.snapshot["lost_stage"]) != "lost":
        raise HTTPException(409, "Requested lost stage semantics changed")
    receipt = DealLossResolution(id=str(data.request_key), request_id=row.id, action=action,
        command=command, command_hash=snapshot_hash(command), snapshot={"request_digest": row.digest,
            "organization_id": row.organization_id, "deal_id": row.deal_id,
            "from_stage": deal.stage, "funnel": deal.funnel,
            "to_stage": row.snapshot["lost_stage"] if action == "finalized" else deal.stage,
            "invoices": status["invoices"], "composition": row.snapshot["invoices"]}, actor=actor)
    receipt.digest = resolution_digest(receipt)
    session.add(receipt)
    await session.flush()
    row.state = action
    await session.flush()
    try:
        if action == "finalized":
            deal.lost_reason_code = row.command["reason_code"]
            deal.lost_comment = row.command["comment"]
            deal.closed_date = date.today().strftime("%d.%m.%Y")
            session.info["deal_loss_transition"] = (deal.id, deal.funnel, deal.stage, row.snapshot["lost_stage"])
            record_stage(session, deal, row.snapshot["lost_stage"], by=actor)
        core.event_bus.emit(session, "sales.deal.loss_" + action, {
            "deal_id": deal.id, "organization_id": row.organization_id, "request_id": row.id,
            "resolution_id": receipt.id, "digest": receipt.digest, "by": actor})
        await session.flush()
    finally:
        session.info.pop("deal_loss_transition", None)
    return resolution_output(receipt)


def assert_principal(actor, expected_principal):
    if actor != expected_principal:
        raise HTTPException(409, "Current principal changed; review the command in this session")


async def request_loss(session, core, user, access, deal_id, data, expected_principal):
    deal, docs, actor, user = await context(session, core, user, access, deal_id, data.organization_id)
    assert_principal(actor, expected_principal)
    command = data.model_dump(mode="json")
    prior = await session.get(DealLossRequest, str(data.request_key), populate_existing=True)
    if prior:
        if prior.organization_id != data.organization_id or prior.deal_id != deal_id or prior.command != command:
            raise HTTPException(409, "Loss request key already used for a different command")
        if prior.digest != request_digest(prior) or prior.command_hash != snapshot_hash(command):
            raise HTTPException(409, "Stored loss request is corrupt")
        receipt = await session.scalar(select(DealLossResolution).where(DealLossResolution.request_id == prior.id))
        return {"request_id": prior.id, "request_digest": prior.digest, "state": prior.state,
                "organization_id": prior.organization_id, "deal_id": prior.deal_id,
                "command_hash": prior.command_hash, "actor": prior.actor,
                "command": prior.command, "snapshot": prior.snapshot, "replayed": True,
                "resolution": resolution_output(receipt) if receipt else None}
    await assert_not_pending(session, deal_id)
    kinds = await kind_map(session, deal.funnel)
    lost = next((code for code, kind in kinds.items() if kind == "lost"), None)
    if lost is None or kinds.get(deal.stage) == "lost":
        raise HTTPException(409, "An open deal and configured lost stage are required")
    codes = set((await session.scalars(select(LossReason.code).where(LossReason.active))).all())
    if codes and data.reason_code not in codes:
        raise HTTPException(422, "Unknown loss reason")
    snapshot = {"funnel": deal.funnel, "stage": deal.stage, "lost_stage": lost, "invoices": composition(docs)}
    if snapshot_hash(snapshot) != data.expected_composition_digest:
        raise HTTPException(409, "Deal or invoice composition changed; obtain a new preview")
    row = DealLossRequest(id=str(data.request_key), organization_id=data.organization_id, deal_id=deal_id,
        command=command, command_hash=snapshot_hash(command), snapshot=snapshot, actor=actor, state="pending")
    row.digest = request_digest(row)
    session.add(row)
    await session.flush()
    core.event_bus.emit(session, "sales.deal.loss_requested", {"deal_id": deal_id,
        "organization_id": data.organization_id, "request_id": row.id, "digest": row.digest, "by": actor})
    receipt = None
    if not docs and data.finalize_if_empty:
        receipt = await resolve(session, core, user, row, deal, docs, actor, ResolveInput(
            request_key=data.request_key, expected_request_digest=row.digest,
            evidence="Explicit finalize_if_empty request"), "finalized")
    return {"request_id": row.id, "request_digest": row.digest, "state": row.state,
            "organization_id": row.organization_id, "deal_id": row.deal_id,
            "command_hash": row.command_hash, "actor": row.actor,
            "command": row.command, "snapshot": row.snapshot, "replayed": False, "resolution": receipt}


router = APIRouter(tags=["Запрос отказа сделки"])


@router.get("/deals/{deal_id}/loss-context")
async def loss_context(deal_id: int, session=Depends(get_session), core=Depends(get_core),
                       user=Depends(require_permission("sales.deal.write")), access=Depends(get_deal_access)):
    try:
        await visible_deal_or_404(session, deal_id, access)
        owner = await session.get(DealOwnership, deal_id)
        org = owner.organization_id if owner else None
        if org is not None:
            await core.services.accounting.lock_event_organization(session, org)
        await lock_deal(session, deal_id)
        user, access = await current_writer(session, core, user)
        deal = await visible_deal_or_404(session, deal_id, access)
        await session.refresh(deal)
        owner = await session.get(DealOwnership, deal_id, populate_existing=True)
        if (owner.organization_id if owner else None) != org:
            raise HTTPException(409, "Deal organization changed; reload context")
        actor = user.keycloak_user_id or user.username
        organization = None
        if org is not None:
            actor = await core.services.accounting.source_member(session, org, user)
            organizations = await core.services.accounting.source_organizations(session, user)
            organization = next((item for item in organizations if item["id"] == org), None)
            if organization is None:
                raise HTTPException(403, "Current organization access is required")
        kinds = await kind_map(session, deal.funnel)
        active = await pending(session, deal_id) if org is not None else None
        latest = await session.scalar(select(DealLossRequest).where(
            DealLossRequest.deal_id == deal_id, DealLossRequest.organization_id == org)
            .order_by(DealLossRequest.created_at.desc(), DealLossRequest.id.desc()).limit(1)) if org else None
        result = {"deal_id": deal_id, "principal": actor, "organization_id": org,
                  "organization": organization, "mapping_required": org is None,
                  "funnel": deal.funnel, "stage": deal.stage,
                  "lost_stage": next((code for code, kind in kinds.items() if kind == "lost"), None),
                  "pending_request_id": active.id if active else None,
                  "latest_request_id": latest.id if latest else None,
                  "latest_request_state": latest.state if latest else None}
        await session.commit()
        return result
    except Exception:
        await session.rollback()
        raise


@router.get("/organizations/{org_id}/deals/{deal_id}/loss-preview")
async def preview(org_id: int, deal_id: int, session=Depends(get_session), core=Depends(get_core),
                  user=Depends(require_permission("sales.deal.write")), access=Depends(get_deal_access)):
    try:
        deal, docs, _, user = await context(session, core, user, access, deal_id, org_id)
        kinds = await kind_map(session, deal.funnel)
        snapshot = {"funnel": deal.funnel, "stage": deal.stage,
            "lost_stage": next((c for c, k in kinds.items() if k == "lost"), None), "invoices": composition(docs)}
        active = await pending(session, deal_id)
        result = {"organization_id": org_id, "deal_id": deal_id, "snapshot": snapshot,
            "composition_digest": snapshot_hash(snapshot), "pending_request_id": active.id if active else None,
            "invoices": [await invoice_progress(session, core, user, org_id, d) for d in docs]}
        await session.commit()
        return result
    except Exception:
        await session.rollback()
        raise


async def command_context(org_id, deal_id, request_id, session, core, user, access):
    deal, docs, actor, user = await context(session, core, user, access, deal_id, org_id)
    row = await session.get(DealLossRequest, str(request_id), populate_existing=True)
    if row is None or row.organization_id != org_id or row.deal_id != deal_id:
        raise HTTPException(404, "Loss request not found")
    return row, deal, docs, actor, user


@router.get("/organizations/{org_id}/deals/{deal_id}/loss-requests/{request_id}")
async def get_progress(org_id: int, deal_id: int, request_id: UUID, session=Depends(get_session),
                       core=Depends(get_core), user=Depends(require_permission("sales.deal.write")),
                       access=Depends(get_deal_access)):
    try:
        row, deal, docs, _, user = await command_context(org_id, deal_id, request_id, session, core, user, access)
        receipt = await session.scalar(select(DealLossResolution).where(DealLossResolution.request_id == row.id))
        if row.digest != request_digest(row) or row.command_hash != snapshot_hash(row.command):
            raise HTTPException(409, "Stored loss request is corrupt")
        if row.state == "pending":
            result = await progress(session, core, user, row, deal, docs)
        else:
            if receipt is None or receipt.action != row.state:
                raise HTTPException(409, "Missing terminal loss resolution")
            result = {"request_id": row.id, "request_digest": row.digest, "state": row.state,
                      "organization_id": org_id, "deal_id": deal_id, "ready_to_finalize": False,
                      "invoices": receipt.snapshot["invoices"], "customer_notification": "not_sent"}
        result["resolution"] = resolution_output(receipt) if receipt else None
        result.update(command=row.command, command_hash=row.command_hash, snapshot=row.snapshot, actor=row.actor)
        await session.commit()
        return result
    except Exception:
        await session.rollback()
        raise


@router.post("/organizations/{org_id}/deals/{deal_id}/loss-requests/{request_id}/{action}")
async def finish(org_id: int, deal_id: int, request_id: UUID, action: str, data: ResolveInput,
                 expected_principal: str = Header(..., alias="X-Expected-Principal", min_length=1, max_length=200),
                 session=Depends(get_session), core=Depends(get_core),
                 user=Depends(require_permission("sales.deal.write")), access=Depends(get_deal_access)):
    if action not in {"finalize", "withdraw"}:
        raise HTTPException(404, "Unknown loss command")
    try:
        row, deal, docs, actor, user = await command_context(org_id, deal_id, request_id, session, core, user, access)
        assert_principal(actor, expected_principal)
        result = await resolve(session, core, user, row, deal, docs, actor, data,
                               "finalized" if action == "finalize" else "withdrawn")
        await session.commit()
        return result
    except Exception:
        await session.rollback()
        raise


def composition_guard(mapper, connection, doc):
    state = inspect(doc)
    changed = {a.key for a in state.attrs if a.history.has_changes()}
    if state.persistent and not changed.intersection(IDENTITY):
        return
    ids = {doc.deal_id, *state.attrs.deal_id.history.deleted}
    kinds = {doc.kind, *state.attrs.kind.history.deleted}
    if "invoice" in kinds and connection.scalar(select(DealLossRequest.id).where(
            DealLossRequest.deal_id.in_(ids), DealLossRequest.state == "pending").limit(1)):
        raise ValueError("Pending loss request freezes every invoice identity")


event.listen(DealDocument, "before_insert", composition_guard)
event.listen(DealDocument, "before_update", composition_guard)


def connection_kinds(connection, funnel):
    rows = connection.execute(select(Stage.code, Stage.kind).where(
        Stage.funnel == funnel, Stage.is_active)).all()
    return dict(rows) if rows else {s["code"]: s["kind"] for s in canonical_stages() if s["funnel"] == funnel}


@event.listens_for(Deal, "before_update")
def deal_stage_guard(mapper, connection, deal):
    state = inspect(deal)
    if not any(state.attrs[k].history.has_changes() for k in ("stage", "funnel")):
        return
    if connection.scalar(select(DealLossRequest.id).where(
            DealLossRequest.deal_id == deal.id, DealLossRequest.state == "pending")):
        raise ValueError("Pending loss request blocks stage and funnel transitions")
    if connection_kinds(connection, deal.funnel).get(deal.stage) == "lost":
        rows = connection.execute(select(DealLossRequest.__table__).where(
            DealLossRequest.deal_id == deal.id, DealLossRequest.state == "finalized")).mappings().all()
        before = state.attrs.stage.history.deleted
        permit = state.session.info.get("deal_loss_transition")
        if permit != (deal.id, deal.funnel, before[0] if before else None, deal.stage) or not any(
                   r["snapshot"]["lost_stage"] == deal.stage and r["snapshot"]["funnel"] == deal.funnel
                   and before == [r["snapshot"]["stage"]] for r in rows):
            raise ValueError("Lost transition requires an exact durable loss resolution")


@event.listens_for(Deal, "before_insert")
def deal_insert_guard(mapper, connection, deal):
    if connection_kinds(connection, deal.funnel or "new_clients").get(deal.stage) == "lost":
        raise ValueError("A new deal cannot bypass the loss coordinator")


@event.listens_for(Stage, "before_update")
def stage_definition_guard(mapper, connection, stage):
    state = inspect(stage)
    if any(state.attrs[k].history.has_changes() for k in ("kind", "funnel", "code", "is_active")):
        funnels = {stage.funnel, *state.attrs.funnel.history.deleted}
        if connection.scalar(select(Deal.id).where(Deal.funnel.in_(funnels)).limit(1)):
            raise ValueError("Populated funnel semantics require an explicit migration")


@event.listens_for(Stage, "before_insert")
def stage_insert_guard(mapper, connection, stage):
    if stage.kind == "lost" and stage.is_active is not False and connection.scalar(
            select(Deal.id).where(Deal.funnel == (stage.funnel or "new_clients"), Deal.stage == stage.code).limit(1)):
        raise ValueError("Cannot reclassify a populated stage as lost")


@event.listens_for(DealDocument, "before_delete")
def deletion_guard(mapper, connection, doc):
    if doc.kind == "invoice" and connection.scalar(select(DealLossRequest.id).where(
            DealLossRequest.deal_id == doc.deal_id, DealLossRequest.state == "pending")):
        raise ValueError("Pending loss request freezes every invoice identity")
