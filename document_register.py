"""Read-only, book-scoped document versions for one exact CRM deal.

Client identity and shipment coverage remain explicitly unresolved. This module
does not infer either from names, payment statuses or warehouse pick tasks.
"""
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from fastapi.responses import HTMLResponse
from sqlalchemy import select

from core.runtime.deps import get_core, get_session
from core.services.auth import require_permission
from modules.sales import documents as originals
from modules.sales.access import get_deal_access, visible_deal_or_404
from modules.sales.accounting_ownership import DealOwnership
from modules.sales.client_document_register import client_identity_for_deal
from modules.sales.models import DealDocument

router = APIRouter(tags=["Реестр документов сделки по юрлицу"])


@router.get("/document-register/organizations")
async def document_register_organizations(session=Depends(get_session), core=Depends(get_core),
                                          user=Depends(require_permission("sales.deal.read"))):
    gateway = getattr(core.services, "accounting", None)
    if gateway is None or not callable(getattr(gateway, "source_organizations", None)):
        raise HTTPException(503, "Organization service is unavailable")
    return await gateway.source_organizations(session, user)


async def read_context(
    org_id: int = Path(gt=0), deal_id: int = Path(gt=0),
    session=Depends(get_session), core=Depends(get_core),
    user=Depends(require_permission("sales.deal.read")), access=Depends(get_deal_access),
):
    gateway = getattr(core.services, "accounting", None)
    if gateway is None or not callable(getattr(gateway, "source_member", None)):
        raise HTTPException(503, "Organization service is unavailable")
    try:
        # source_member obtains the organization lock. Never lock/read a deal
        # for this operation before book membership and the organization lock.
        await gateway.source_member(session, org_id, user)
        await visible_deal_or_404(session, deal_id, access)
        owner = await session.get(DealOwnership, deal_id)
        if owner is None or owner.organization_id != org_id:
            raise HTTPException(404, "Deal not found in this organization")
        yield session
    finally:
        # Read-only request: release the book lock without committing any data.
        await session.rollback()


async def validate_links(session, deal_id, rows):
    linked = {value for row in rows for value in (row.supersedes_id, row.superseded_by_id)
              if value is not None}
    if not linked:
        return
    valid = set((await session.scalars(select(DealDocument.id).where(
        DealDocument.id.in_(linked), DealDocument.deal_id == deal_id,
    ))).all())
    if linked != valid:
        # Do not expose foreign identifiers, document numbers or existence.
        raise HTTPException(409, "Document version links require reconciliation")


def serialize(row, org_id):
    snapshot = row.snapshot_json or {}
    currency = snapshot.get("currency")
    return {
        "id": row.id, "deal_id": row.deal_id, "organization_id": org_id,
        "kind": row.kind, "number": row.number, "version": row.version,
        "status": row.status, "amount": str(row.amount),
        "currency": currency if isinstance(currency, str) and currency else None,
        "created_at": row.created_at, "issued_at": row.issued_at,
        "valid_until": row.valid_until, "reserve_status": row.reserve_status,
        "expiry_reminder_at": row.reminded_at,
        "onec_ref": row.onec_ref, "original_state": row.original_state,
        "content_sha256": row.content_sha256, "replacement_reason": row.replacement_reason,
        "supersedes_id": row.supersedes_id, "superseded_by_id": row.superseded_by_id,
        "original_available": row.kind in {"invoice", "contract"}
        and row.original_state in {"issued", "approval_copy"}
        and bool(row.snapshot_json and row.content_sha256),
        # Preview stays in the existing editing UI; never used as an original.
        "preview_available": row.kind in {"invoice", "contract"} and row.status == "draft",
    }


@router.get("/organizations/{org_id}/deals/{deal_id}/document-register")
async def list_deal_document_register(
    org_id: int, deal_id: int, after_id: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=100),
    kind: Literal["invoice", "contract", "order"] | None = None,
    session=Depends(read_context, scope="function"), core=Depends(get_core),
):
    query = select(DealDocument).where(
        DealDocument.deal_id == deal_id, DealDocument.id > after_id,
    )
    if kind is not None:
        query = query.where(DealDocument.kind == kind)
    rows = (await session.scalars(query.order_by(DealDocument.id).limit(limit + 1))).all()
    page = rows[:limit]
    await validate_links(session, deal_id, page)
    from modules.sales.shipment_register import project

    shipments = await project(core, session, org_id, [row.id for row in page if row.kind == "invoice"])
    return {
        "organization_id": org_id, "deal_id": deal_id,
        "client_identity": await client_identity_for_deal(session, org_id, deal_id),
        "coverage": {"sales_documents": "available",
                     "settlements": "separate_chief_register", "shipments": shipments["status"],
                     "tn_ttn": shipments["tn_ttn_status"]},
        "shipment_documents": shipments["items"],
        "items": [serialize(row, org_id) for row in page],
        "next_after_id": page[-1].id if len(rows) > limit else None,
    }


async def scoped_document(session, deal_id, doc_id):
    doc = await session.scalar(select(DealDocument).where(
        DealDocument.id == doc_id, DealDocument.deal_id == deal_id,
    ))
    if doc is None:
        raise HTTPException(404, "Document not found in this deal")
    await validate_links(session, deal_id, [doc])
    return doc


@router.get("/organizations/{org_id}/deals/{deal_id}/document-register/{doc_id}")
async def get_deal_document_register_item(
    org_id: int, deal_id: int, doc_id: int = Path(gt=0),
    session=Depends(read_context, scope="function"),
):
    """Exact scoped lookup for a linked version outside the current page/filter."""
    return serialize(await scoped_document(session, deal_id, doc_id), org_id)


@router.get("/organizations/{org_id}/deals/{deal_id}/documents/{doc_id}/original",
            response_class=HTMLResponse)
async def get_deal_document_register_original(
    org_id: int, deal_id: int, doc_id: int = Path(gt=0),
    session=Depends(read_context, scope="function"),
):
    doc = await scoped_document(session, deal_id, doc_id)
    return original_response(doc)


@router.get("/organizations/{org_id}/documents/{doc_id}/original", response_class=HTMLResponse)
async def get_book_document_original(
    org_id: int = Path(gt=0), doc_id: int = Path(gt=0),
    session=Depends(get_session), core=Depends(get_core),
    user=Depends(require_permission("sales.deal.read")), access=Depends(get_deal_access),
):
    """Resolve an exact ledger reference without guessing a deal or its owner."""
    gateway = getattr(core.services, "accounting", None)
    if gateway is None or not callable(getattr(gateway, "source_member", None)):
        raise HTTPException(503, "Organization service is unavailable")
    try:
        await gateway.source_member(session, org_id, user)
        doc = await session.scalar(select(DealDocument).join(
            DealOwnership, DealOwnership.deal_id == DealDocument.deal_id,
        ).where(DealDocument.id == doc_id, DealOwnership.organization_id == org_id))
        if doc is None:
            raise HTTPException(404, "Document not found in this organization")
        await visible_deal_or_404(session, doc.deal_id, access)
        await validate_links(session, doc.deal_id, [doc])
        return original_response(doc)
    finally:
        await session.rollback()


def original_response(doc):
    if doc.kind not in {"invoice", "contract"}:
        raise HTTPException(409, "Saved original is supported only for invoices and contracts")
    if doc.original_state == "draft":
        raise HTTPException(409, "Draft preview is not an original")
    try:
        html = originals.original(doc)
    except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
        raise HTTPException(409, "Original integrity requires reconciliation") from exc
    return HTMLResponse(html, headers={
        "ETag": f'"{doc.content_sha256}"', "Cache-Control": "private, no-store",
        "X-Document-State": doc.original_state,
    })
