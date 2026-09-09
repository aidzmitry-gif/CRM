"""Read the immutable-documents contract; never rebuild from current deal prices."""

from fastapi import HTTPException
from sqlalchemy import select

from modules.sales.mail_pdf import pdf
from modules.sales.mail_queue import Attachment, digest
from modules.sales.models import DealDocument


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
        original = getattr(document, "original_html", None)
        expected = getattr(document, "content_sha256", None)
        if document.kind not in {"invoice", "contract"} or document.status not in {
            "posted",
            "paid",
        }:
            raise HTTPException(
                409, "Отправлять можно только выпущенные счета и согласованные договоры"
            )
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
