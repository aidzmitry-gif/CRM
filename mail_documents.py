"""Read the immutable-documents contract; never rebuild from current deal prices."""

from decimal import InvalidOperation

from fastapi import HTTPException
from sqlalchemy import select

from modules.sales import documents
from modules.sales.invoice_issuance import is_erp_invoice, verified_receipt
from modules.sales.mail_pdf import pdf
from modules.sales.mail_queue import Attachment, digest
from modules.sales.models import DealDocument


async def eligible_original(session, document: DealDocument) -> str:
    """Use the same immutable original contract for options and fresh preparation."""
    if document.kind not in {"invoice", "contract"} or document.status not in {
        "issued", "posted", "paid",
    }:
        raise HTTPException(409, "Отправлять можно только выпущенные счета и согласованные договоры")
    original = document.original_html
    expected = document.content_sha256
    if not original or not expected or not document.issued_at:
        raise HTTPException(409, "У документа нет сохранённого выпущенного оригинала")
    if digest(original.encode("utf-8")) != expected:
        raise HTTPException(409, "Контрольная сумма оригинала не совпадает")
    if document.kind == "invoice" and await is_erp_invoice(session, document):
        try:
            snapshot = document.snapshot_json
            if not isinstance(snapshot, dict):
                raise ValueError("Missing invoice snapshot")
            organization_id = snapshot.get("organization_id")
            if type(organization_id) is not int or organization_id <= 0:
                raise ValueError("Invalid invoice organization")
            original = documents.original(document, issued_only=True)
            if await verified_receipt(session, document, organization_id) is None:
                raise ValueError("Missing invoice issuance receipt")
        except (ValueError, KeyError, TypeError, InvalidOperation) as exc:
            raise HTTPException(409, "Контроль целостности выпущенного счёта не пройден") from exc
    elif document.status == "issued":
        raise HTTPException(409, "Отправлять можно только выпущенные счета и согласованные договоры")
    return original


async def attachments_for(session, deal_id: int, ids: list[int]) -> list[Attachment]:
    documents = (
        await session.scalars(
            select(DealDocument).where(DealDocument.deal_id == deal_id, DealDocument.id.in_(ids))
        )
    ).all()
    by_id = {document.id: document for document in documents}
    if len(by_id) != len(ids):
        raise HTTPException(404, "Документ не найден в этой сделке")
    attachments = []
    for document_id in ids:
        document = by_id[document_id]
        original = await eligible_original(session, document)
        expected = document.content_sha256
        version = document.version
        attachments.append(
            Attachment(
                document.id,
                version,
                document.number,
                expected,
                f"{document.kind}-{document.id}-v{version}.pdf",
                await pdf(original),
            )
        )
    return attachments
