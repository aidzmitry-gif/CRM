"""Read-only view of durable invoice notification decisions."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.runtime.core import Core
from core.runtime.deps import get_core, get_session
from core.services.auth import CurrentUser, require_permission
from modules.sales.access import DealAccess, get_deal_access, visible_deal_or_404
from modules.sales.invoice_notifications import InvoiceNotification, summary

router = APIRouter(tags=["Уведомления по счетам"])


@router.get("/deals/{deal_id}/invoice-notifications")
async def list_invoice_notifications(
    deal_id: int,
    session: AsyncSession = Depends(get_session),
    _: object = Depends(require_permission("sales.deal.read")),
    access: DealAccess = Depends(get_deal_access),
):
    await visible_deal_or_404(session, deal_id, access)
    rows = (
        await session.scalars(
            select(InvoiceNotification)
            .where(InvoiceNotification.deal_id == deal_id)
            .order_by(InvoiceNotification.created_at.desc(), InvoiceNotification.id.desc())
            .limit(100)
        )
    ).all()
    return [summary(row) for row in rows]


@router.get("/deals/{deal_id}/invoice-notifications/{notification_id}")
async def get_invoice_notification(
    deal_id: int,
    notification_id: int,
    session: AsyncSession = Depends(get_session),
    _: object = Depends(require_permission("sales.deal.read")),
    access: DealAccess = Depends(get_deal_access),
):
    await visible_deal_or_404(session, deal_id, access)
    row = await session.scalar(
        select(InvoiceNotification).where(
            InvoiceNotification.id == notification_id,
            InvoiceNotification.deal_id == deal_id,
        )
    )
    if row is None:
        raise HTTPException(404, "Уведомление не найдено")
    return summary(row)


@router.post("/deals/{deal_id}/invoice-notifications/{notification_id}/prepare-email", status_code=201)
async def prepare_invoice_notification_email(
    deal_id: int,
    notification_id: int,
    core: Core = Depends(get_core),
    session: AsyncSession = Depends(get_session),
    user: CurrentUser = Depends(require_permission("sales.deal.write")),
    access: DealAccess = Depends(get_deal_access),
):
    """Freeze a customer notice; sending still requires the normal confirm action."""
    from modules.sales.documents import lock_deal
    from modules.sales.mail_models import OutgoingEmail
    from modules.sales.mail_pdf import pdf
    from modules.sales.mail_queue import Attachment, digest, prepare
    from modules.sales.mail_routes import summary as email_summary
    from modules.sales.mail_transport import configured_sender
    from modules.sales.models import DealDocument

    await visible_deal_or_404(session, deal_id, access)
    await lock_deal(session, deal_id)
    row = await session.scalar(
        select(InvoiceNotification).where(
            InvoiceNotification.id == notification_id,
            InvoiceNotification.deal_id == deal_id,
        )
    )
    if row is None:
        raise HTTPException(404, "Уведомление не найдено")
    if row.state != "pending" or not row.recipient:
        raise HTTPException(409, "Для уведомления сначала нужно исправить адрес клиента")
    document = await session.get(DealDocument, row.document_id)
    if (
        document is None
        or document.deal_id != deal_id
        or document.version != row.document_version
        or document.content_sha256 != row.payload.get("content_sha256")
        or not document.original_html
        or digest(document.original_html.encode("utf-8")) != row.payload.get("content_sha256")
    ):
        raise HTTPException(409, "Оригинал счёта изменён или требует сверки")
    if row.event_type == "sales.invoice.expiring" and document.status not in {"issued", "posted"}:
        raise HTTPException(409, "Сроковое уведомление устарело: счёт уже изменил статус")
    if row.event_type == "sales.invoice.cancelled" and document.status != "cancelled":
        raise HTTPException(409, "Уведомление об аннулировании требует статуса cancelled")
    try:
        sender = configured_sender(core.services.config)
    except ValueError as exc:
        raise HTTPException(503, str(exc)) from exc

    subject = (
        f"Срок действия счёта {document.number} истекает"
        if row.event_type == "sales.invoice.expiring"
        else f"Счёт {document.number} аннулирован"
    )
    if row.event_type == "sales.invoice.expiring":
        body = (
            f"Уведомляем: срок действия счёта {document.number} истекает "
            f"{row.payload.get('valid_until') or 'в установленную в счёте дату'}. "
            "После этой даты счёт перестанет действовать; для оформления поставки "
            "потребуется новый счёт."
        )
    else:
        body = (
            f"Уведомляем: счёт {document.number} аннулирован и больше не действует. "
            "Для оформления поставки потребуется новый счёт."
        )
    request_key = f"invoice-notification-{row.id}"
    existing = await session.scalar(
        select(OutgoingEmail).where(
            OutgoingEmail.deal_id == deal_id,
            OutgoingEmail.request_key == request_key,
        )
    )
    if existing is not None:
        if (
            existing.to != [row.recipient]
            or existing.subject != subject
            or not existing.attachments
            or existing.attachments[0].get("document_id") != document.id
            or existing.attachments[0].get("version") != document.version
            or existing.attachments[0].get("source_sha256") != document.content_sha256
        ):
            raise HTTPException(409, "Ключ уведомления уже использован с другим письмом")
        return email_summary(existing)

    attachment = Attachment(
        document.id,
        document.version,
        document.number,
        document.content_sha256,
        f"invoice-{document.id}-v{document.version}.pdf",
        await pdf(document.original_html),
    )
    email = await prepare(
        session,
        deal_id=deal_id,
        actor=user.keycloak_user_id or user.username,
        key=request_key,
        sender=sender,
        to=[row.recipient],
        cc=[],
        subject=subject,
        body=body,
        attachments=[attachment],
    )
    return email_summary(email)
