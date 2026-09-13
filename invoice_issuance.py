"""Local invoice issuance: one original, exact ERP reserve and outbox transaction.

No 1C/legacy stock or global seller/buyer lookup. The immutable receipt is the
authority for local-issued source facts, not a client-supplied snapshot marker.
"""

from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal

from fastapi import HTTPException
from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, event, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, mapped_column

from core.db.base import Base
from core.domain.models import Counterparty, CounterpartyBranch, Sku
from modules.sales import documents
from modules.sales.access import visible_deal_or_404
from modules.sales.accounting_ownership import DealOwnership
from modules.sales.client_document_register import DealClientBinding
from modules.sales.models import DealDocument, DealItem
from modules.sales.schemas import DocumentOut

MODE = "erp_issuance_v1"


class InvoiceIssuanceReceipt(Base):
    __tablename__ = "invoice_issuance_receipt"
    __table_args__ = {"schema": "sales"}
    document_id: Mapped[int] = mapped_column(
        ForeignKey("sales.deal_document.id"), primary_key=True, autoincrement=False
    )
    deal_id: Mapped[int] = mapped_column(Integer)
    organization_id: Mapped[int] = mapped_column(Integer)
    document_version: Mapped[int] = mapped_column(Integer)
    content_sha256: Mapped[str] = mapped_column(String(64))
    snapshot_digest: Mapped[str] = mapped_column(String(64))
    reservation_digest: Mapped[str | None] = mapped_column(String(64))
    request_key: Mapped[str] = mapped_column(String(64), unique=True)
    request_hash: Mapped[str] = mapped_column(String(64))
    response: Mapped[dict] = mapped_column(JSON)
    actor: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class InvoiceLateReservationReceipt(Base):
    """Separate proof of a later reserve; the issuance receipt remains unchanged."""
    __tablename__ = "invoice_late_reservation_receipt"
    __table_args__ = {"schema": "sales"}
    document_id: Mapped[int] = mapped_column(ForeignKey("sales.deal_document.id"), primary_key=True, autoincrement=False)
    organization_id: Mapped[int] = mapped_column(Integer)
    document_version: Mapped[int] = mapped_column(Integer)
    content_sha256: Mapped[str] = mapped_column(String(64))
    issuance_snapshot_digest: Mapped[str] = mapped_column(String(64))
    request_key: Mapped[str] = mapped_column(String(64), unique=True)
    request_hash: Mapped[str] = mapped_column(String(64))
    reservation_digest: Mapped[str] = mapped_column(String(64))
    response: Mapped[dict] = mapped_column(JSON)
    actor: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


def immutable_receipt(*args):
    raise ValueError("Invoice issuance receipt is immutable")


event.listen(InvoiceIssuanceReceipt, "before_update", immutable_receipt)
event.listen(InvoiceIssuanceReceipt, "before_delete", immutable_receipt)
event.listen(InvoiceLateReservationReceipt, "before_update", immutable_receipt)
event.listen(InvoiceLateReservationReceipt, "before_delete", immutable_receipt)


async def effective_reservation_digest(session, doc, receipt):
    if doc.reserve_mode != "on_order":
        return receipt.reservation_digest
    later = await session.get(InvoiceLateReservationReceipt, doc.id)
    if later is None:
        return None
    if (later.organization_id != receipt.organization_id or later.document_version != doc.version
            or later.content_sha256 != doc.content_sha256 or later.issuance_snapshot_digest != receipt.snapshot_digest):
        raise ValueError("Later reservation does not match the immutable invoice")
    return later.reservation_digest


def receipt_anchor(receipt):
    return {
        "document_id": receipt.document_id,
        "request_key": receipt.request_key,
        "request_hash": receipt.request_hash,
        "snapshot_digest": receipt.snapshot_digest,
    }


async def verified_receipt(session, doc, organization_id):
    if doc is None:
        raise ValueError("Issued document is missing")
    receipt = await session.get(InvoiceIssuanceReceipt, doc.id)
    marker = isinstance(doc.snapshot_json, dict) and doc.snapshot_json.get("issuance_mode") == MODE
    if receipt is None:
        if marker or doc.status == "issued":
            raise ValueError("Local issued invoice requires its persisted issuance receipt")
        return None
    if (
        receipt.organization_id != organization_id
        or doc.reserve_mode not in {"stock", "on_order"}
        or (doc.reserve_mode == "on_order" and receipt.reservation_digest is not None)
        or (doc.reserve_mode == "stock" and not receipt.reservation_digest)
        or receipt.deal_id != doc.deal_id
        or receipt.document_id != doc.id
        or receipt.document_version != doc.version
        or not marker
        or not doc.issued_at
        or doc.issued_by != receipt.actor
        or receipt.content_sha256 != doc.content_sha256
        or documents.digest(doc.original_html) != receipt.content_sha256
        or documents.digest(doc.snapshot_json) != receipt.snapshot_digest
        or doc.snapshot_json.get("document_id") != doc.id
        or doc.snapshot_json.get("organization_id") != organization_id
        or doc.snapshot_json.get("version") != doc.version
        or Decimal(doc.snapshot_json["amount"]) != doc.amount
    ):
        raise ValueError("Invoice issuance receipt does not match the immutable original")
    return receipt


async def is_erp_invoice(session, doc):
    # This is a conservative lifecycle guard, not an authorization decision.
    return (
        doc.status == "issued"
        or (isinstance(doc.snapshot_json, dict) and doc.snapshot_json.get("issuance_mode") == MODE)
        or await session.get(InvoiceIssuanceReceipt, doc.id) is not None
    )


def service(core, name, method):
    value = getattr(core.services, name, None)
    if not callable(getattr(value, method, None)):
        raise HTTPException(503, f"Required invoice service is unavailable: {name}.{method}")
    return value


async def lock_context(session, core, user, access, deal_id, org):
    actor = await service(core, "accounting", "source_member").source_member(session, org, user)
    await visible_deal_or_404(session, deal_id, access)
    await documents.lock_deal(session, deal_id)
    deal = await visible_deal_or_404(session, deal_id, access)
    await session.refresh(deal)
    owner = await session.get(DealOwnership, deal_id)
    if owner is None or owner.organization_id != org:
        raise HTTPException(409, "Confirm exact deal organization ownership before issuing")
    return deal, actor


async def candidate(session, core, user, deal, data, doc_id=None):
    doc = None
    if doc_id is not None:
        doc = await session.scalar(
            select(DealDocument)
            .where(DealDocument.id == doc_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if doc is None or doc.deal_id != deal.id or doc.kind != "invoice":
            raise HTTPException(404, "Invoice draft not found")
        if doc.supersedes_id is not None:
            raise HTTPException(
                409, "Invoice replacement requires exact reservation release; not implemented"
            )
        if doc.status != "draft" or doc.original_html:
            raise HTTPException(409, "Only an uncaptured invoice draft can be issued")
    else:
        if await session.scalar(
            select(DealDocument.id).where(
                DealDocument.deal_id == deal.id, DealDocument.kind == "invoice"
            )
        ):
            raise HTTPException(409, "Invoice already exists; replacement is a separate operation")
    binding = await session.get(DealClientBinding, deal.id)
    if binding is None or binding.organization_id != data.organization_id:
        raise HTTPException(409, "Confirm the exact client binding for this organization")
    buyer = await session.scalar(
        select(Counterparty)
        .where(Counterparty.id == binding.counterparty_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if buyer is None or not buyer.is_active or buyer.merged_into_id is not None:
        raise HTTPException(409, "Buyer must be an active, unmerged bound client")
    if deal.counterparty_id is not None and deal.counterparty_id != buyer.id:
        raise HTTPException(409, "Selected CRM party differs from the confirmed invoice buyer")
    branch = None
    if deal.branch_id is not None:
        branch = await session.scalar(select(CounterpartyBranch).where(
            CounterpartyBranch.id == deal.branch_id,
        ).with_for_update().execution_options(populate_existing=True))
        if branch is None or not branch.is_active or branch.legal_entity_id != buyer.id:
            raise HTTPException(409, "Selected branch does not belong to the active invoice buyer")
    seller = await service(core, "accounting", "invoice_seller").invoice_seller(
        session, data.organization_id, user, on=data.document_date, currency=data.currency
    )
    if (
        seller["organization_id"] != data.organization_id
        or seller["seller"]["currency"] != data.currency
    ):
        raise HTTPException(409, "Seller identity/currency does not match")
    buyer_facts = {
        "counterparty_id": buyer.id,
        "revision": buyer.revision,
        "name": buyer.legal_name or buyer.name,
        "display_name": buyer.display_name or buyer.name,
        "branch": ({"id": branch.id, "revision": branch.revision,
                    "legal_entity_id": branch.legal_entity_id, "name": branch.name,
                    "address": branch.address, "tax_mode": branch.tax_mode,
                    "portal_branch_code": branch.portal_branch_code} if branch else None),
        "unp": buyer.unp,
        "requisites": buyer.requisites or {},
    }
    buyer_facts["requisites_digest"] = documents.digest(buyer_facts)
    rows = (
        await session.execute(
            select(DealItem, Sku)
            .outerjoin(Sku, Sku.id == DealItem.sku_id)
            .where(DealItem.deal_id == deal.id)
            .order_by(DealItem.id)
            .with_for_update(of=DealItem)
            .execution_options(populate_existing=True)
        )
    ).all()
    pricing = {row.item_id: row for row in data.pricing}
    if not rows or {item.id for item, _ in rows} != set(pricing):
        raise HTTPException(
            422,
            "Explicit prices must exactly cover all goods lines; service-only issue is not implemented",
        )
    sku_ids = {item.sku_id for item, _ in rows}
    locked_skus = (
        await session.scalars(
            select(Sku)
            .where(Sku.id.in_(sku_ids))
            .order_by(Sku.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).all()
    skus = {sku.id: sku for sku in locked_skus}
    lines = []
    for line_no, (item, _) in enumerate(rows, 1):
        sku = skus.get(item.sku_id)
        if (
            sku is None
            or not sku.code
            or not sku.is_active
            or not item.qty.is_finite()
            or item.qty <= 0
            or item.qty >= Decimal("1000000000000")
            or item.qty != item.qty.quantize(Decimal(".01"))
        ):
            raise HTTPException(
                422, "Invoice goods require an active exact SKU and positive two-place quantity"
            )
        price = pricing[item.id]
        net = (item.qty * Decimal(price.unit_price_net)).quantize(
            Decimal(".01"), rounding=ROUND_HALF_UP
        )
        tax = (net * Decimal(price.vat_rate) / 100).quantize(Decimal(".01"), rounding=ROUND_HALF_UP)
        lines.append(
            {
                "line_no": line_no,
                "item_id": item.id,
                "sku_id": sku.id,
                "sku_code": sku.code,
                "name": sku.title,
                "unit": sku.unit,
                "qty": format(item.qty, ".2f"),
                "price": price.unit_price_net,
                "vat_rate": price.vat_rate,
                "net": format(net, ".2f"),
                "tax": format(tax, ".2f"),
                "total": format(net + tax, ".2f"),
                "currency": data.currency,
            }
        )
    amount = sum((Decimal(row["total"]) for row in lines), Decimal("0"))
    if amount >= Decimal("1000000000000"):
        raise HTTPException(422, "Invoice amount exceeds supported precision")
    basis = {
        "deal_id": deal.id,
        "deal_number": deal.number,
        "deal_title": deal.title,
        "document_id": doc_id,
        "document_version": doc.version if doc else 1,
        "organization_id": data.organization_id,
        "document_date": data.document_date.isoformat(),
        "valid_until": data.valid_until.isoformat(),
        "currency": data.currency,
        "seller_profile": {
            k: seller[k] for k in ("profile_id", "revision", "digest", "effective_from")
        },
        "seller": seller["seller"],
        "buyer": buyer_facts,
        "lines": lines,
        "pricing_evidence": data.pricing_evidence,
        "amount": format(amount, ".2f"),
        "payment_terms": doc.payment_terms if doc else None,
        "delivery_terms": doc.delivery_terms if doc else None,
    }
    # Preserve historical stock preview hashes; new on-order bases are distinct.
    if data.reserve_mode == "on_order":
        basis["reserve_mode"] = "on_order"
    return {**basis, "basis_digest": documents.digest(basis)}


async def preview(session, core, user, access, deal_id, data):
    try:
        deal, _ = await lock_context(session, core, user, access, deal_id, data.organization_id)
        prepared = await candidate(session, core, user, deal, data, data.document_id)
        availability = None
        if data.reserve_mode == "stock":
            availability = await service(core, "wms_reservations", "invoice_availability").invoice_availability(
                session, data.organization_id, sorted({row["sku_code"] for row in prepared["lines"]}))
        return {**prepared, "reserve_mode": data.reserve_mode, "availability": availability}
    finally:
        await session.rollback()


def canonical_request(deal_id, doc_id, data):
    request = data.model_dump(mode="json", exclude={"kind", "document_id"})
    # Existing immutable receipts were hashed without these new default fields.
    if request["reserve_mode"] == "stock":
        request.pop("reserve_mode")
        request.pop("unreserved_confirmed")
    request["pricing"] = sorted(request["pricing"], key=lambda row: row["item_id"])
    request["allocations"] = sorted(
        request["allocations"], key=lambda row: (row["line_no"], row["warehouse"])
    )
    return {"operation": "erp_invoice_issue", "deal_id": deal_id, "document_id": doc_id, **request}


async def issue(session, core, user, access, deal_id, data, doc_id=None):
    try:
        deal, actor = await lock_context(session, core, user, access, deal_id, data.organization_id)
        request_hash = documents.digest(canonical_request(deal_id, doc_id, data))
        prior = await session.scalar(
            select(InvoiceIssuanceReceipt).where(
                InvoiceIssuanceReceipt.request_key == data.request_key
            )
        )
        if prior is not None:
            if (
                prior.organization_id != data.organization_id
                or prior.deal_id != deal_id
                or prior.request_hash != request_hash
                or (doc_id is not None and prior.document_id != doc_id)
            ):
                raise HTTPException(409, "Issuance key already has a different request/scope")
            existing = await session.get(DealDocument, prior.document_id)
            try:
                await verified_receipt(session, existing, data.organization_id)
            except (ValueError, KeyError, TypeError) as exc:
                raise HTTPException(409, "Issued receipt requires reconciliation") from exc
            await session.commit()
            return {**prior.response, "replayed": True}
        if doc_id is not None and await session.get(InvoiceIssuanceReceipt, doc_id):
            raise HTTPException(409, "Invoice already issued; replay the original request key")
        from modules.sales.deal_loss import assert_not_pending
        await assert_not_pending(session, deal_id)
        prepared = await candidate(session, core, user, deal, data, doc_id)
        if (
            prepared["basis_digest"] != data.expected_basis_digest
            or prepared["document_version"] != data.expected_document_version
        ):
            raise HTTPException(409, "Invoice sources changed; obtain and confirm a new preview")
        if data.reserve_mode == "stock" and not data.journal_complete:
            raise HTTPException(422, "Confirm the complete physical journal explicitly")
        gateway = service(core, "wms_reservations", "reserve_invoice") if data.reserve_mode == "stock" else None
        if doc_id is None:
            doc = DealDocument(
                deal_id=deal_id,
                kind="invoice",
                number=f"ERP-INV-{deal.id}",
                version=prepared["document_version"],
                status="draft",
            )
            session.add(doc)
            await session.flush()
            doc.number = f"ERP-INV-{doc.id}"
        else:
            doc = await session.get(DealDocument, doc_id)
        documents.capture_erp_invoice(doc, prepared, actor)
        doc.status = "issued"
        doc.reserve_status = "reserved" if gateway else "unreserved"
        doc.reserved_at = datetime.now(timezone.utc).replace(tzinfo=None) if gateway else None
        await session.flush()
        source = {
            "document_id": doc.id,
            "deal_id": deal_id,
            "organization_id": data.organization_id,
            "version": doc.version,
            "content_sha256": doc.content_sha256,
            "reserve_status": "reserved",
            "lines": [
                {k: line[k] for k in ("line_no", "sku_code", "qty")} for line in prepared["lines"]
            ],
        }
        reserved = await gateway.reserve_invoice(
            session,
            data.organization_id,
            source,
            {
                "allocations": [a.model_dump() for a in data.allocations],
                "evidence": data.evidence,
                "journal_complete": data.journal_complete,
            },
            actor,
        ) if gateway else {"digest": None, "snapshot": {"allocations": []}}
        response = {
            "document": DocumentOut.model_validate(doc).model_dump(mode="json"),
            "status": "issued",
            "organization_id": data.organization_id,
            "document_version": doc.version,
            "content_sha256": doc.content_sha256,
            "reservation_digest": reserved["digest"],
            "replayed": False,
        }
        receipt = InvoiceIssuanceReceipt(
            document_id=doc.id,
            deal_id=deal_id,
            organization_id=data.organization_id,
            document_version=doc.version,
            content_sha256=doc.content_sha256,
            snapshot_digest=documents.digest(doc.snapshot_json),
            reservation_digest=reserved["digest"],
            request_key=data.request_key,
            request_hash=request_hash,
            response=response,
            actor=actor,
        )
        session.add(receipt)
        await session.flush()
        payload = {
            "schema_version": 1,
            "issuance_mode": MODE,
            "document_id": doc.id,
            "deal_id": deal_id,
            "organization_id": data.organization_id,
            "document_version": doc.version,
            "content_sha256": doc.content_sha256,
            "reservation_digest": reserved["digest"],
            "issuance_receipt": receipt_anchor(receipt),
            "items": reserved["snapshot"]["allocations"],
            "valid_until": doc.valid_until.isoformat(),
            "issued_at": doc.issued_at.isoformat(),
            "by": actor,
            "entity_ref": f"deal:{deal_id}",
        }
        if gateway:
            core.event_bus.emit(session, "sales.stock.reserved", payload)
        core.event_bus.emit(
            session,
            "sales.invoice.issued",
            {
                **payload,
                "reserve_mode": data.reserve_mode,
                "reserve_status": doc.reserve_status,
                "number": doc.number,
                "currency": data.currency,
                "amount": str(doc.amount),
                "buyer_id": prepared["buyer"]["counterparty_id"],
                "seller_profile_id": prepared["seller_profile"]["profile_id"],
            },
        )
        await session.commit()
        return response
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            409, "Concurrent invoice issuance; replay the original request"
        ) from exc
    except Exception:
        await session.rollback()
        raise
