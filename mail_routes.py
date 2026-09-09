"""Explicit preview -> confirm -> observed SMTP result, scoped to visible deals."""

from __future__ import annotations

import os
from email import policy
from email.parser import BytesParser

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import undefer

from core.runtime.core import Core
from core.runtime.deps import get_core, get_session
from core.services.auth import CurrentUser, require_permission
from modules.sales.access import DealAccess, get_deal_access, visible_deal_or_404
from modules.sales.mail_documents import attachments_for
from modules.sales.mail_models import EmailAttempt, OutgoingEmail
from modules.sales.mail_queue import audit, digest, now, prepare
from modules.sales.mail_transport import configured_sender, recipients
from modules.sales.models import DealDocument

router = APIRouter()


class PrepareEmail(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_key: str = Field(pattern=r"^[A-Za-z0-9_-]{8,64}$")
    document_ids: list[int] = Field(min_length=1, max_length=10)
    to: list[str] = Field(min_length=1, max_length=10)
    cc: list[str] = Field(default_factory=list, max_length=10)
    subject: str = Field(min_length=1, max_length=250)
    body: str = Field(default="Направляем документы по вашей сделке.", max_length=10000)

    @field_validator("document_ids")
    @classmethod
    def unique_documents(cls, value):
        if len(set(value)) != len(value) or any(i <= 0 for i in value):
            raise ValueError("Выберите разные документы по их ID")
        return value

    @field_validator("subject")
    @classmethod
    def safe_subject(cls, value):
        if not value.strip() or any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise ValueError("Некорректная тема письма")
        return value


class RetryEmail(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_attempt: int = Field(ge=1)
    acknowledge_possible_duplicate: bool = False


def sender_or_503(core):
    try:
        return configured_sender(core.services.config)
    except ValueError as exc:
        raise HTTPException(503, str(exc)) from exc


def enabled(core):
    if os.getenv("AIOS_SALES_EMAIL_ENABLED") != "1":
        raise HTTPException(503, "Исходящая отправка ещё не включена администратором")
    sender_or_503(core)


def summary(email):
    def timestamp(value):
        return value.isoformat() + "Z" if value else None

    return {
        "id": email.id,
        "deal_id": email.deal_id,
        "sender": email.sender,
        "to": email.to,
        "cc": email.cc,
        "subject": email.subject,
        "body": email.body,
        "attachments": email.attachments,
        "status": email.status,
        "created_at": timestamp(email.created_at),
        "confirmed_at": timestamp(email.confirmed_at),
        "accepted_at": timestamp(email.accepted_at),
        "next_attempt_at": timestamp(email.next_attempt_at),
        "attempt_count": email.attempt_count,
        "last_reason": email.last_reason,
        "message_id": email.message_id,
    }


async def visible_email(session, deal_id, email_id, access, *, include_mime=False):
    await visible_deal_or_404(session, deal_id, access)
    stmt = (
        select(OutgoingEmail)
        .where(OutgoingEmail.id == email_id, OutgoingEmail.deal_id == deal_id)
        .execution_options(populate_existing=True)
    )
    if include_mime:
        stmt = stmt.options(undefer(OutgoingEmail.mime))
    email = await session.scalar(stmt)
    if email is None:
        raise HTTPException(404, "Письмо не найдено")
    return email


@router.get("/deals/{deal_id}/emails/options")
async def email_options(
    deal_id: int,
    core: Core = Depends(get_core),
    session: AsyncSession = Depends(get_session),
    _: CurrentUser = Depends(require_permission("sales.deal.read")),
    access: DealAccess = Depends(get_deal_access),
):
    await visible_deal_or_404(session, deal_id, access)
    documents = (
        await session.scalars(
            select(DealDocument)
            .where(DealDocument.deal_id == deal_id, DealDocument.kind.in_(("invoice", "contract")))
            .order_by(DealDocument.id.desc())
        )
    ).all()
    try:
        sender, error = configured_sender(core.services.config), None
    except ValueError as exc:
        sender, error = None, str(exc)
    return {
        "sender": sender,
        "configuration_error": error,
        "enabled": os.getenv("AIOS_SALES_EMAIL_ENABLED") == "1",
        "documents": [
            {
                "id": d.id,
                "number": d.number,
                "kind": d.kind,
                "status": d.status,
                "version": getattr(d, "version", None),
                "superseded_by_id": getattr(d, "superseded_by_id", None),
                "available": bool(
                    getattr(d, "issued_at", None)
                    and getattr(d, "original_html", None)
                    and d.status in {"posted", "paid"}
                ),
            }
            for d in documents
        ],
    }


@router.post("/deals/{deal_id}/emails/prepare", status_code=201)
async def prepare_email(
    deal_id: int,
    payload: PrepareEmail,
    core: Core = Depends(get_core),
    session: AsyncSession = Depends(get_session),
    user: CurrentUser = Depends(require_permission("sales.deal.write")),
    access: DealAccess = Depends(get_deal_access),
):
    await visible_deal_or_404(session, deal_id, access)
    sender = sender_or_503(core)
    try:
        to, cc = recipients(payload.to, payload.cc)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    attachments = await attachments_for(session, deal_id, payload.document_ids)
    email = await prepare(
        session,
        deal_id=deal_id,
        actor=user.keycloak_user_id or user.username,
        key=payload.request_key,
        sender=sender,
        to=to,
        cc=cc,
        subject=payload.subject,
        body=payload.body,
        attachments=attachments,
    )
    return summary(email)


@router.get("/deals/{deal_id}/emails")
async def list_emails(
    deal_id: int,
    session: AsyncSession = Depends(get_session),
    _: CurrentUser = Depends(require_permission("sales.deal.read")),
    access: DealAccess = Depends(get_deal_access),
):
    await visible_deal_or_404(session, deal_id, access)
    emails = (
        await session.scalars(
            select(OutgoingEmail)
            .where(OutgoingEmail.deal_id == deal_id)
            .order_by(OutgoingEmail.created_at.desc(), OutgoingEmail.id.desc())
            .limit(100)
        )
    ).all()
    return [summary(email) for email in emails]


@router.get("/deals/{deal_id}/emails/{email_id}")
async def get_email(
    deal_id: int,
    email_id: str,
    session: AsyncSession = Depends(get_session),
    _: CurrentUser = Depends(require_permission("sales.deal.read")),
    access: DealAccess = Depends(get_deal_access),
):
    email = await visible_email(session, deal_id, email_id, access)
    attempts = (
        await session.scalars(
            select(EmailAttempt)
            .where(EmailAttempt.email_id == email.id)
            .order_by(EmailAttempt.number)
        )
    ).all()
    return {
        **summary(email),
        "attempts": [
            {
                "number": a.number,
                "status": a.status,
                "reason": a.reason,
                "smtp_code": a.smtp_code,
                "recipients": a.recipients,
                "started_at": a.started_at.isoformat() + "Z",
                "finished_at": a.finished_at.isoformat() + "Z" if a.finished_at else None,
            }
            for a in attempts
        ],
    }


@router.get("/deals/{deal_id}/emails/{email_id}/attachments/{index}")
async def email_attachment(
    deal_id: int,
    email_id: str,
    index: int,
    session: AsyncSession = Depends(get_session),
    _: CurrentUser = Depends(require_permission("sales.deal.read")),
    access: DealAccess = Depends(get_deal_access),
):
    email = await visible_email(session, deal_id, email_id, access, include_mime=True)
    if not 0 <= index < len(email.attachments):
        raise HTTPException(404, "Вложение не найдено")
    if digest(email.mime) != email.mime_sha256:
        raise HTTPException(409, "Контрольная сумма письма не совпадает")
    parts = list(BytesParser(policy=policy.default).parsebytes(email.mime).iter_attachments())
    content = parts[index].get_payload(decode=True)
    meta = email.attachments[index]
    if digest(content) != meta["sha256"]:
        raise HTTPException(409, "Контрольная сумма вложения не совпадает")
    return Response(
        content,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'inline; filename="{meta["filename"]}"',
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.post("/deals/{deal_id}/emails/{email_id}/send")
async def confirm_email(
    deal_id: int,
    email_id: str,
    core: Core = Depends(get_core),
    session: AsyncSession = Depends(get_session),
    user: CurrentUser = Depends(require_permission("sales.deal.write")),
    access: DealAccess = Depends(get_deal_access),
):
    email = await visible_email(session, deal_id, email_id, access)
    if email.status != "prepared":
        return summary(email)  # retrying the same confirmation never queues twice
    enabled(core)
    if sender_or_503(core) != email.sender:
        raise HTTPException(409, "Отправитель изменился: подготовьте новое письмо")
    result = await session.execute(
        update(OutgoingEmail)
        .where(OutgoingEmail.id == email_id, OutgoingEmail.status == "prepared")
        .values(
            status="queued",
            confirmed_at=now(),
            confirmed_by=user.keycloak_user_id or user.username,
            next_attempt_at=now(),
        )
    )
    if result.rowcount:
        audit(session, email, "queued", user.keycloak_user_id or user.username)
    await session.commit()
    await session.refresh(email)
    return summary(email)


@router.post("/deals/{deal_id}/emails/{email_id}/retry")
async def retry_email(
    deal_id: int,
    email_id: str,
    payload: RetryEmail,
    core: Core = Depends(get_core),
    session: AsyncSession = Depends(get_session),
    user: CurrentUser = Depends(require_permission("sales.deal.write")),
    access: DealAccess = Depends(get_deal_access),
):
    email = await visible_email(session, deal_id, email_id, access)
    if email.attempt_count != payload.expected_attempt:
        raise HTTPException(409, "Состояние изменилось: обновите историю попыток")
    if email.status not in {"failed", "uncertain"}:
        return summary(email)
    if email.status == "uncertain" and not payload.acknowledge_possible_duplicate:
        raise HTTPException(409, "Получатель мог уже получить письмо. Подтвердите риск дубля")
    enabled(core)
    result = await session.execute(
        update(OutgoingEmail)
        .where(
            OutgoingEmail.id == email_id,
            OutgoingEmail.status == email.status,
            OutgoingEmail.attempt_count == payload.expected_attempt,
        )
        .values(status="queued", round_attempts=0, next_attempt_at=now(), claim_token=None)
    )
    if result.rowcount:
        audit(
            session,
            email,
            "retry_requested",
            user.keycloak_user_id or user.username,
            possible_duplicate=payload.acknowledge_possible_duplicate,
        )
    await session.commit()
    await session.refresh(email)
    return summary(email)
