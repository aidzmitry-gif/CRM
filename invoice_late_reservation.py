"""Internal later reservation command; caller owns commit/rollback, public UI follows."""
from datetime import date, datetime, timezone

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from modules.sales import documents
from modules.sales.deal_loss import assert_not_pending
from modules.sales.invoice_issuance import (
    MODE,
    InvoiceLateReservationReceipt,
    effective_reservation_digest,
    lock_context,
    receipt_anchor,
    service,
    verified_receipt,
)
from modules.sales.models import DealDocument
from modules.sales.schemas import DocumentOut, InvoiceAllocation


class LateReservationPreviewInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    organization_id: int = Field(gt=0, strict=True)
    expected_document_version: int = Field(gt=0, strict=True)
    expected_content_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class LateReservationInput(LateReservationPreviewInput):
    request_key: str = Field(min_length=8, max_length=64)
    allocations: list[InvoiceAllocation] = Field(min_length=1, max_length=1000)
    evidence: str = Field(min_length=1, max_length=1000)
    journal_complete: bool = Field(strict=True)


async def preview_later(session, core, user, access, deal_id, document_id, data: LateReservationPreviewInput):
    """Use immutable invoice lines, never the current mutable deal items."""
    try:
        await lock_context(session, core, user, access, deal_id, data.organization_id)
        doc = await session.scalar(select(DealDocument).where(DealDocument.id == document_id,
            DealDocument.deal_id == deal_id).with_for_update().execution_options(populate_existing=True))
        if doc is None:
            raise HTTPException(404, "Invoice not found")
        receipt = await verified_receipt(session, doc, data.organization_id)
        if (receipt is None or doc.reserve_mode != "on_order"
                or doc.version != data.expected_document_version or doc.content_sha256 != data.expected_content_sha256
                or doc.status not in {"issued", "paid"} or doc.reserve_status != "unreserved"
                or doc.superseded_by_id is not None or doc.valid_until is None or doc.valid_until < date.today()):
            raise HTTPException(409, "Invoice identity, validity or state requires review before reservation")
        await assert_not_pending(session, deal_id)
        lines = doc.snapshot_json["items"]
        availability = await service(core, "wms_reservations", "invoice_availability").invoice_availability(
            session, data.organization_id, sorted({line["sku_code"] for line in lines}))
        return {"organization_id": data.organization_id, "deal_id": deal_id, "document_id": document_id,
                "document_version": doc.version, "content_sha256": doc.content_sha256,
                "lines": lines, "availability": availability}
    finally:
        await session.rollback()


async def reserve_later(session, core, user, access, deal_id, document_id, data: LateReservationInput):
    _, actor = await lock_context(session, core, user, access, deal_id, data.organization_id)
    doc = await session.scalar(select(DealDocument).where(DealDocument.id == document_id,
        DealDocument.deal_id == deal_id).with_for_update().execution_options(populate_existing=True))
    if doc is None:
        raise HTTPException(404, "Invoice not found")
    receipt = await verified_receipt(session, doc, data.organization_id)
    if receipt is None or doc.reserve_mode != "on_order":
        raise HTTPException(409, "Later reservation requires an issued on-order original")
    body = data.model_dump(mode="json")
    body["allocations"] = sorted(body["allocations"], key=lambda row: (row["line_no"], row["warehouse"]))
    request_hash = documents.digest({"deal_id": deal_id, "document_id": document_id, **body})
    previous = await session.scalar(select(InvoiceLateReservationReceipt).where(
        InvoiceLateReservationReceipt.request_key == data.request_key))
    if previous:
        if previous.document_id != document_id or previous.organization_id != data.organization_id or previous.request_hash != request_hash:
            raise HTTPException(409, "Later reservation key identifies another command")
        await effective_reservation_digest(session, doc, receipt)
        return {**previous.response, "replayed": True}
    if await session.get(InvoiceLateReservationReceipt, document_id):
        raise HTTPException(409, "Invoice already reserved; replay its original reservation command")
    await assert_not_pending(session, deal_id)
    if (doc.version != data.expected_document_version or doc.content_sha256 != data.expected_content_sha256
            or doc.status not in {"issued", "paid"} or doc.reserve_status != "unreserved"
            or doc.superseded_by_id is not None or doc.valid_until is None or doc.valid_until < date.today()):
        raise HTTPException(409, "Invoice identity, validity or state requires review before reservation")
    gateway = service(core, "wms_reservations", "reserve_invoice")
    source = {"organization_id": data.organization_id, "document_id": doc.id, "deal_id": deal_id,
        "version": doc.version, "content_sha256": doc.content_sha256, "reserve_status": "reserved",
        "lines": [{k: line[k] for k in ("line_no", "sku_code", "qty")} for line in doc.snapshot_json["items"]]}
    reserved = await gateway.reserve_invoice(session, data.organization_id, source,
        {k: body[k] for k in ("allocations", "evidence", "journal_complete")}, actor)
    doc.reserve_status = "reserved"
    doc.reserved_at = datetime.now(timezone.utc).replace(tzinfo=None)
    # The receipt INSERT guard reads the document state in this same transaction.
    # Persist that state first; neither flush commits the reservation.
    await session.flush()
    response = {"document": DocumentOut.model_validate(doc).model_dump(mode="json"),
                "reservation_digest": reserved["digest"], "replayed": False}
    session.add(InvoiceLateReservationReceipt(document_id=doc.id, organization_id=data.organization_id,
        document_version=doc.version, content_sha256=doc.content_sha256, issuance_snapshot_digest=receipt.snapshot_digest,
        request_key=data.request_key, request_hash=request_hash, reservation_digest=reserved["digest"],
        response=response, actor=actor))
    await session.flush()
    core.event_bus.emit(session, "sales.stock.reserved", {"schema_version": 1, "issuance_mode": MODE,
        "document_id": doc.id, "deal_id": deal_id, "organization_id": data.organization_id,
        "document_version": doc.version, "content_sha256": doc.content_sha256,
        "reservation_digest": reserved["digest"], "issuance_receipt": receipt_anchor(receipt),
        "items": reserved["snapshot"]["allocations"], "valid_until": doc.valid_until.isoformat(),
        "issued_at": doc.issued_at.isoformat(), "by": actor, "entity_ref": f"deal:{deal_id}"})
    return response
