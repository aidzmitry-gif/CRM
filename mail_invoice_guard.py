"""Coordinate ERP invoice invalidation with frozen outgoing email."""
from fastapi import HTTPException
from sqlalchemy import select

from modules.sales.invoice_issuance import is_erp_invoice
from modules.sales.mail_documents import email_document_available
from modules.sales.mail_models import OutgoingEmail
from modules.sales.models import DealDocument


def document_references(attachments):
    """Do not infer absence of a document from an unreadable frozen list."""
    if not isinstance(attachments, list):
        raise HTTPException(409, "Не удалось проверить состав письма: сначала сверьте его состояние")
    refs = []
    for item in attachments:
        if not isinstance(item, dict) or "document_id" not in item:
            raise HTTPException(409, "Не удалось проверить вложение письма: сначала сверьте его состояние")
        document_id = item["document_id"]
        if document_id is None:
            if item.get("version") is not None or item.get("number") != "":
                raise HTTPException(409, "Не удалось проверить вложение письма: сначала сверьте его состояние")
            continue
        if type(document_id) is not int or document_id <= 0:
            raise HTTPException(409, "Не удалось проверить документ письма: сначала сверьте его состояние")
        refs.append((document_id, item))
    return refs


async def verify_erp_email_documents(session, core, user, email):
    """Lock the book before queueing, then recheck the frozen invoice identity."""
    for document_id, item in document_references(email.attachments):
        document = await session.get(DealDocument, document_id)
        if document is None or document.deal_id != email.deal_id:
            raise HTTPException(409, "Документ письма недоступен: подготовьте письмо заново")
        if document.kind != "invoice" or not await is_erp_invoice(session, document):
            continue
        if (not await email_document_available(session, core, user, document)
            or document.version != item.get("version")
            or document.content_sha256 != item.get("source_sha256")):
            raise HTTPException(409, "Счёт изменён или отменён: подготовьте письмо заново")


async def require_no_inflight_invoice_mail(session, deal_id, document_id):
    """The cancellation caller holds the organization lock before email locks."""
    rows = (await session.scalars(
        select(OutgoingEmail).where(
            OutgoingEmail.deal_id == deal_id,
            OutgoingEmail.status.in_(("queued", "retry_wait", "sending", "uncertain")),
        ).order_by(OutgoingEmail.id).with_for_update().execution_options(populate_existing=True)
    )).all()
    for email in rows:
        if any(doc_id == document_id for doc_id, _ in document_references(email.attachments)):
            raise HTTPException(409, "По счёту есть письмо в очереди или с неопределённой отправкой; сначала сверьте его состояние")