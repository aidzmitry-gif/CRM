"""Read the immutable-documents contract; never rebuild from current deal prices."""

from fastapi import HTTPException
from sqlalchemy import select

from modules.sales.accounting_ownership import DealOwnership
from modules.sales.invoice_issuance import is_erp_invoice, verified_receipt
from modules.sales.mail_pdf import pdf
from modules.sales.mail_queue import Attachment, digest
from modules.sales.models import DealDocument


async def email_document_available(session, core, user, document) -> bool:
    """Only a verified ERP receipt and current book member may use an ERP original."""
    if document.kind not in {"invoice", "contract"}:
        return False
    if document.kind == "invoice" and await is_erp_invoice(session, document):
        if document.status not in {"issued", "paid"}:
            return False
        owner = await session.get(DealOwnership, document.deal_id)
        if owner is None:
            return False
        try:
            await core.services.accounting.source_member(session, owner.organization_id, user)
            # A cancellation may have committed while the book lock was awaited.
            await session.refresh(document)
            if document.kind != "invoice" or document.status not in {"issued", "paid"}:
                return False
            return await verified_receipt(session, document, owner.organization_id) is not None
        except HTTPException as exc:
            if exc.status_code in {403, 404}:
                return False
            raise
        except (ValueError, KeyError, TypeError, ArithmeticError, AttributeError):
            return False
    return bool(document.status in {"posted", "paid"} and document.issued_at and document.original_html)


async def attachments_for(session, core, user, deal_id: int, ids: list[int]) -> list[Attachment]:
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
        erp_invoice = document.kind == "invoice" and await is_erp_invoice(session, document)
        if document.kind not in {"invoice", "contract"} or (
            erp_invoice and not await email_document_available(session, core, user, document)
        ) or (not erp_invoice and document.status not in {"posted", "paid"}):
            raise HTTPException(
                409, "Отправлять можно только выпущенные счета и согласованные договоры"
            )
        original = getattr(document, "original_html", None)
        expected = getattr(document, "content_sha256", None)
        if not original or not expected or not getattr(document, "issued_at", None):
            raise HTTPException(409, "У документа нет сохранённого выпущенного оригинала")
        if digest(original.encode("utf-8")) != expected:
            raise HTTPException(409, "Контрольная сумма оригинала не совпадает")
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
