"""Explicit client identity, chief-confirmed legacy bindings and scoped history.

Counterparty is the shared-kernel MDM model. Names are display-only: no matching,
alias following or automatic rebinding is performed here.
"""
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    event,
    func,
    select,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, mapped_column

from core.db.base import Base
from core.domain.models import Counterparty
from core.runtime.deps import get_core, get_session
from core.services.auth import require_permission
from modules.sales.access import get_deal_access, scope_deals
from modules.sales.accounting_ownership import DealOwnership
from modules.sales.models import Deal, DealDocument


class DealClientBinding(Base):
    __tablename__ = "deal_client_binding"
    __table_args__ = (CheckConstraint("organization_id > 0 AND counterparty_id > 0",
                                     name="deal_client_binding_positive"), {"schema": "sales"})
    deal_id: Mapped[int] = mapped_column(ForeignKey("sales.deal_ownership.deal_id"), primary_key=True)
    organization_id: Mapped[int] = mapped_column(Integer, index=True)
    counterparty_id: Mapped[int] = mapped_column(ForeignKey("counterparty.id"), index=True)
    snapshot: Mapped[dict] = mapped_column(JSON)
    evidence: Mapped[str] = mapped_column(String(1000))
    actor: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


def immutable_binding(*args):
    raise ValueError("Confirmed client binding cannot be changed or deleted")


event.listen(DealClientBinding, "before_update", immutable_binding)
event.listen(DealClientBinding, "before_delete", immutable_binding)


class ClientBindingInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    counterparty_id: int = Field(gt=0, strict=True)
    expected_snapshot: dict
    evidence: str = Field(min_length=1, max_length=1000)


router = APIRouter(tags=["Подтверждённый клиент сделки и реестр клиента"])


def gateway(core, operation):
    service = getattr(core.services, "accounting", None)
    if service is None or not callable(getattr(service, operation, None)):
        raise HTTPException(503, "Organization service is unavailable")
    return service


async def chief_context(
    request: Request, org_id: int = Path(gt=0), deal_id: int = Path(gt=0),
    session=Depends(get_session), core=Depends(get_core),
    user=Depends(require_permission("sales.deal.read")), access=Depends(get_deal_access),
):
    try:
        # Organization -> deal -> counterparty; same book lock order as A.
        actor = await gateway(core, "source_owner_authority").source_owner_authority(session, org_id, user)
        deal = await session.scalar(scope_deals(select(Deal).where(Deal.id == deal_id), access)
                                    .with_for_update().execution_options(populate_existing=True))
        owner = await session.get(DealOwnership, deal_id) if deal else None
        if owner is None or owner.organization_id != org_id:
            raise HTTPException(404, "Deal not found in this organization")
        yield session, actor, deal
        if request.method == "POST":
            await session.commit()
    except IntegrityError as exc:
        raise HTTPException(409, "Concurrent client binding; review the confirmed record") from exc
    finally:
        await session.rollback()


async def client_context(
    org_id: int = Path(gt=0), session=Depends(get_session), core=Depends(get_core),
    user=Depends(require_permission("sales.deal.read")), access=Depends(get_deal_access),
):
    try:
        await gateway(core, "source_member").source_member(session, org_id, user)
        yield session, access
    finally:
        await session.rollback()


def identity(cp):
    return {"id": cp.id, "unp": cp.unp, "name": cp.name, "revision": cp.revision,
            "is_active": cp.is_active, "merged_into_id": cp.merged_into_id}


async def exact_client(session, client_id, *, for_claim=False):
    query = select(Counterparty).where(Counterparty.id == client_id)
    if for_claim:
        query = query.with_for_update()
    cp = await session.scalar(query.execution_options(populate_existing=True))
    if cp is None:
        raise HTTPException(404, "Client not found")
    if for_claim and (not cp.is_active or cp.merged_into_id is not None):
        raise HTTPException(409, "Choose an active, unmerged client by exact ID")
    return cp


async def preview_snapshot(session, org_id, deal, client_id):
    if deal.counterparty_id is not None and deal.counterparty_id != client_id:
        raise HTTPException(409, "Selected CRM party differs from the client binding")
    cp = await exact_client(session, client_id, for_claim=True)
    rows = (await session.scalars(select(DealDocument).where(DealDocument.deal_id == deal.id)
                                 .order_by(DealDocument.id).with_for_update()
                                 .execution_options(populate_existing=True))).all()
    return {"organization_id": org_id,
            "deal": {"id": deal.id, "number": deal.number, "counterparty": deal.counterparty,
                     "owner_id": deal.owner_id},
            "client": identity(cp),
            "documents": [{"id": row.id, "version": row.version, "number": row.number,
                           "kind": row.kind, "amount": str(row.amount), "status": row.status,
                           "content_sha256": row.content_sha256,
                           "supersedes_id": row.supersedes_id,
                           "superseded_by_id": row.superseded_by_id} for row in rows]}


def binding_out(row):
    return {"deal_id": row.deal_id, "organization_id": row.organization_id,
            "counterparty_id": row.counterparty_id, "snapshot": row.snapshot,
            "evidence": row.evidence, "actor": row.actor, "created_at": row.created_at}


async def client_identity_for_deal(session, org_id, deal_id):
    row = await session.get(DealClientBinding, deal_id)
    if row is None:
        return {"status": "unresolved", "counterparty_id": None}
    if row.organization_id != org_id:
        raise HTTPException(409, "Client binding organization requires reconciliation")
    return {"status": "confirmed", "counterparty_id": row.counterparty_id,
            "snapshot": row.snapshot["client"]}


@router.get("/organizations/{org_id}/deals/{deal_id}/client-binding-preview")
async def preview_deal_client_binding(
    org_id: int, deal_id: int, counterparty_id: int = Query(gt=0),
    ctx=Depends(chief_context, scope="function"),
):
    session, _, deal = ctx
    existing = await session.get(DealClientBinding, deal_id)
    if existing is not None:
        if existing.counterparty_id != counterparty_id or existing.organization_id != org_id:
            raise HTTPException(409, "Deal client is already confirmed; rebinding requires a separate protocol")
        return {"assigned": True, "snapshot": existing.snapshot, "binding": binding_out(existing)}
    return {"assigned": False, "snapshot": await preview_snapshot(session, org_id, deal, counterparty_id),
            "binding": None}


@router.post("/organizations/{org_id}/deals/{deal_id}/client-binding", status_code=201)
async def claim_deal_client_binding(
    org_id: int, deal_id: int, data: ClientBindingInput,
    ctx=Depends(chief_context, scope="function"),
):
    session, actor, deal = ctx
    existing = await session.get(DealClientBinding, deal_id)
    if existing is not None:
        if (existing.organization_id != org_id or existing.counterparty_id != data.counterparty_id
                or existing.snapshot != data.expected_snapshot or existing.evidence != data.evidence):
            raise HTTPException(409, "Deal client already confirmed with different facts")
        return binding_out(existing)
    current = await preview_snapshot(session, org_id, deal, data.counterparty_id)
    if current != data.expected_snapshot:
        raise HTTPException(409, "Client, deal or documents changed; preview again")
    row = DealClientBinding(deal_id=deal_id, organization_id=org_id,
                            counterparty_id=data.counterparty_id, snapshot=current,
                            evidence=data.evidence, actor=actor)
    session.add(row)
    await session.flush()
    await session.refresh(row, attribute_names=["created_at"])
    return binding_out(row)


def client_query(org_id, client_id, access):
    return scope_deals(select(DealDocument, DealClientBinding).join(Deal, Deal.id == DealDocument.deal_id)
        .join(DealOwnership, DealOwnership.deal_id == Deal.id)
        .join(DealClientBinding, DealClientBinding.deal_id == Deal.id).where(
            DealOwnership.organization_id == org_id, DealClientBinding.organization_id == org_id,
            DealClientBinding.counterparty_id == client_id,
        ), access)


def item_out(row, binding, org_id, client_id):
    from modules.sales.document_register import serialize

    return {**serialize(row, org_id), "counterparty_id": client_id,
            "client_snapshot": binding.snapshot["client"],
            "original_url": f"/sales/organizations/{org_id}/deals/{row.deal_id}/documents/{row.id}/original",
            "client_original_url": f"/sales/organizations/{org_id}/counterparties/{client_id}/documents/{row.id}/original"}


@router.get("/organizations/{org_id}/counterparties/{counterparty_id}/document-register")
async def list_client_document_register(
    org_id: int, counterparty_id: int = Path(gt=0), after_id: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=100),
    kind: Literal["invoice", "contract", "order"] | None = None,
    ctx=Depends(client_context, scope="function"), core=Depends(get_core),
):
    from modules.sales.document_register import validate_links

    session, access = ctx
    cp = await exact_client(session, counterparty_id)
    query = client_query(org_id, counterparty_id, access).where(DealDocument.id > after_id)
    if kind:
        query = query.where(DealDocument.kind == kind)
    rows = (await session.execute(query.order_by(DealDocument.id).limit(limit + 1))).all()
    page = rows[:limit]
    for row, _ in page:
        await validate_links(session, row.deal_id, [row])
    from modules.sales.shipment_register import project

    shipments = await project(core, session, org_id, [row.id for row, _ in page if row.kind == "invoice"])
    return {"organization_id": org_id, "counterparty_id": counterparty_id,
            "client_current": identity(cp), "identity_policy": "exact_binding_no_merge_follow",
            "coverage": {"sales_documents": "confirmed_bindings_only",
                         "settlements": "separate_chief_register", "shipments": shipments["status"],
                         "tn_ttn": shipments["tn_ttn_status"]},
            "shipment_documents": shipments["items"],
            "items": [item_out(row, binding, org_id, counterparty_id) for row, binding in page],
            "next_after_id": page[-1][0].id if len(rows) > limit else None}


async def scoped_client_document(session, org_id, client_id, doc_id, access):
    from modules.sales.document_register import validate_links

    result = (await session.execute(client_query(org_id, client_id, access)
                                   .where(DealDocument.id == doc_id))).first()
    if result is None:
        raise HTTPException(404, "Document not found in this client and organization")
    row, binding = result
    await validate_links(session, row.deal_id, [row])
    return row, binding


@router.get("/organizations/{org_id}/counterparties/{counterparty_id}/document-register/{doc_id}")
async def get_client_document_register_item(
    org_id: int, counterparty_id: int = Path(gt=0), doc_id: int = Path(gt=0),
    ctx=Depends(client_context, scope="function"),
):
    row, binding = await scoped_client_document(ctx[0], org_id, counterparty_id, doc_id, ctx[1])
    return item_out(row, binding, org_id, counterparty_id)


@router.get("/organizations/{org_id}/counterparties/{counterparty_id}/documents/{doc_id}/original")
async def get_client_document_register_original(
    org_id: int, counterparty_id: int = Path(gt=0), doc_id: int = Path(gt=0),
    ctx=Depends(client_context, scope="function"),
):
    from modules.sales.document_register import get_deal_document_register_original

    row, _ = await scoped_client_document(ctx[0], org_id, counterparty_id, doc_id, ctx[1])
    # Reuse A's exact deal/org original integrity checks without a redirect or
    # any second HTTP request. Client membership is checked on every read.
    return await get_deal_document_register_original(org_id, row.deal_id, doc_id, ctx[0])
