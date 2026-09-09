"""Transactional queue with frozen MIME and conservative restart recovery."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email import policy
from email.message import EmailMessage
from email.utils import format_datetime
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import undefer

from core.domain.models import AuditLog
from modules.sales.mail_models import EmailAttempt, OutgoingEmail
from modules.sales.mail_transport import MAX_MESSAGE_BYTES, SMTPResult, address, recipients, submit

logger = logging.getLogger("aios.sales.email")
RETRY_DELAYS = (30, 120, 600)  # initial attempt plus at most three automatic retries
LEASE_SECONDS = 600


def now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


@dataclass(frozen=True)
class Attachment:
    document_id: int
    version: int
    number: str
    source_sha256: str
    filename: str
    content: bytes

    def metadata(self) -> dict:
        return {
            "document_id": self.document_id,
            "version": self.version,
            "number": self.number,
            "source_sha256": self.source_sha256,
            "filename": self.filename,
            "content_type": "application/pdf",
            "size": len(self.content),
            "sha256": digest(self.content),
        }


def audit(session, email, action: str, actor: str = "", **detail) -> None:
    session.add(
        AuditLog(
            actor=actor,
            action=f"sales.email.{action}",
            entity_ref=f"deal:{email.deal_id}",
            detail={"email_id": email.id, "message_id": email.message_id, **detail},
        )
    )


async def prepare(
    session,
    *,
    deal_id: int,
    actor: str,
    key: str,
    sender: str,
    to: list[str],
    cc: list[str],
    subject: str,
    body: str,
    attachments: list[Attachment],
) -> OutgoingEmail:
    try:
        sender = address(sender)
        to, cc = recipients(to, cc)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if (
        not subject.strip()
        or len(subject) > 250
        or any(ord(c) < 32 or ord(c) == 127 for c in subject)
    ):
        raise HTTPException(422, "Некорректная тема письма")
    if len(body) > 10000 or not 1 <= len(attachments) <= 10:
        raise HTTPException(422, "Недопустимый размер письма или количество документов")
    if sum(len(a.content) for a in attachments) > 14 * 1024 * 1024:
        raise HTTPException(413, "Общий размер PDF превышает 14 МиБ")
    if any(
        not a.content.startswith(b"%PDF-") or any(c in a.filename for c in "\r\n/\\")
        for a in attachments
    ):
        raise HTTPException(422, "Некорректное PDF-вложение")
    metadata = [a.metadata() for a in attachments]
    # PDF engines may add volatile metadata: idempotency is based on immutable
    # source hashes, not the bytes of a redundant conversion.
    fingerprint = digest(
        json.dumps(
            {
                "deal": deal_id,
                "sender": sender,
                "to": to,
                "cc": cc,
                "subject": subject,
                "body": body,
                "documents": [
                    {k: m[k] for k in ("document_id", "version", "source_sha256")} for m in metadata
                ],
            },
            sort_keys=True,
            ensure_ascii=False,
        ).encode()
    )
    existing = await session.scalar(
        select(OutgoingEmail).where(
            OutgoingEmail.created_by == actor, OutgoingEmail.request_key == key
        )
    )
    if existing:
        if existing.request_hash != fingerprint:
            raise HTTPException(409, "Ключ запроса уже использован для другого письма")
        return existing
    email_id = str(uuid4())
    created = now()
    message_id = f"<{email_id}@{sender.rsplit('@', 1)[1]}>"
    msg = EmailMessage(policy=policy.SMTP)
    msg["From"], msg["To"] = sender, ", ".join(to)
    if cc:
        msg["Cc"] = ", ".join(cc)
    msg["Subject"], msg["Message-ID"] = subject, message_id
    msg["Date"] = format_datetime(created.replace(tzinfo=timezone.utc))
    msg.set_content(body)
    for attachment in attachments:
        msg.add_attachment(
            attachment.content, maintype="application", subtype="pdf", filename=attachment.filename
        )
    mime = msg.as_bytes()
    if len(mime) > MAX_MESSAGE_BYTES:
        raise HTTPException(413, "Письмо с вложениями превышает 20 МиБ после кодирования")
    email = OutgoingEmail(
        id=email_id,
        deal_id=deal_id,
        request_key=key,
        request_hash=fingerprint,
        created_by=actor,
        created_at=created,
        sender=sender,
        to=to,
        cc=cc,
        subject=subject,
        body=body,
        attachments=metadata,
        mime=mime,
        mime_sha256=digest(mime),
        message_id=message_id,
        status="prepared",
        attempt_count=0,
        round_attempts=0,
    )
    session.add(email)
    audit(session, email, "prepared", actor)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        existing = await session.scalar(
            select(OutgoingEmail).where(
                OutgoingEmail.created_by == actor, OutgoingEmail.request_key == key
            )
        )
        if existing is None or existing.request_hash != fingerprint:
            raise HTTPException(409, "Конфликт ключа подготовки письма") from None
        return existing
    return email


async def recover(session) -> int:
    """Expired in-flight attempts may already have been accepted: no blind retry."""
    cutoff = now() - timedelta(seconds=LEASE_SECONDS)
    rows = (
        await session.scalars(
            select(OutgoingEmail)
            .where(OutgoingEmail.status == "sending", OutgoingEmail.claimed_at < cutoff)
            .with_for_update(skip_locked=True)
        )
    ).all()
    for email in rows:
        email.status, email.last_reason = "uncertain", "worker_interrupted"
        await session.execute(
            update(EmailAttempt)
            .where(
                EmailAttempt.email_id == email.id,
                EmailAttempt.number == email.attempt_count,
                EmailAttempt.status == "sending",
            )
            .values(status="uncertain", reason="worker_interrupted", finished_at=now())
        )
        audit(session, email, "uncertain", reason="worker_interrupted")
    await session.commit()
    return len(rows)


async def claim(session) -> tuple[str, str] | None:
    due = now()
    stmt = select(OutgoingEmail).where(
        OutgoingEmail.status.in_(("queued", "retry_wait")),
        or_(OutgoingEmail.next_attempt_at.is_(None), OutgoingEmail.next_attempt_at <= due),
    )
    email = await session.scalar(
        stmt.order_by(OutgoingEmail.created_at, OutgoingEmail.id)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    if email is None:
        await session.rollback()
        return None
    email_id, token = email.id, str(uuid4())
    # CAS also protects SQLite tests and accidental multiple local workers.
    changed = await session.execute(
        update(OutgoingEmail)
        .where(
            OutgoingEmail.id == email_id,
            OutgoingEmail.status == email.status,
            OutgoingEmail.attempt_count == email.attempt_count,
        )
        .values(
            status="sending",
            claim_token=token,
            claimed_at=due,
            attempt_count=email.attempt_count + 1,
            round_attempts=email.round_attempts + 1,
        )
        .execution_options(synchronize_session=False)
    )
    if changed.rowcount != 1:
        await session.rollback()
        return None
    session.add(
        EmailAttempt(
            email_id=email_id,
            number=email.attempt_count + 1,
            started_at=due,
            status="sending",
            recipients={},
        )
    )
    await session.commit()  # durable BEFORE any network I/O
    return email_id, token


async def deliver(factory, settings, email_id: str, token: str, transport=submit) -> None:
    async with factory() as session:
        email = await session.scalar(
            select(OutgoingEmail)
            .options(undefer(OutgoingEmail.mime))
            .where(
                OutgoingEmail.id == email_id,
                OutgoingEmail.status == "sending",
                OutgoingEmail.claim_token == token,
            )
        )
        if email is None:
            return
        mime, sender, targets = email.mime, email.sender, [*email.to, *email.cc]
        intact = digest(mime) == email.mime_sha256
    result = (
        (await asyncio.to_thread(transport, settings, sender, targets, mime))
        if intact
        else SMTPResult("failed", "mime_integrity_error")
    )
    async with factory() as session:
        email = await session.scalar(
            select(OutgoingEmail)
            .where(
                OutgoingEmail.id == email_id,
                OutgoingEmail.claim_token == token,
                OutgoingEmail.status == "sending",
            )
            .with_for_update()
        )
        if email is None:
            return
        status = result.status
        if status == "retry_wait" and email.round_attempts > len(RETRY_DELAYS):
            status = "failed"
        email.status, email.last_reason = status, result.reason
        email.next_attempt_at = (
            now() + timedelta(seconds=RETRY_DELAYS[email.round_attempts - 1])
            if status == "retry_wait"
            else None
        )
        if status == "accepted":
            email.accepted_at = now()
            from modules.sales.models import Message

            session.add(
                Message(
                    deal_id=email.deal_id,
                    channel="email",
                    direction="out",
                    author=email.confirmed_by or email.created_by,
                    text=f"Почтовый сервер принял письмо «{email.subject}» → {', '.join(targets)}. ID: {email.id}. Доставка получателю не подтверждена.",
                )
            )
        await session.execute(
            update(EmailAttempt)
            .where(EmailAttempt.email_id == email.id, EmailAttempt.number == email.attempt_count)
            .values(
                status=status,
                reason=result.reason,
                smtp_code=result.code,
                recipients=result.recipients,
                finished_at=now(),
            )
        )
        audit(
            session,
            email,
            status,
            reason=result.reason,
            attempt=email.attempt_count,
            smtp_code=result.code,
        )
        await session.commit()


class EmailWorker:
    """Uses existing DB/config/lifecycle; independent from intake and event relay."""

    def __init__(self, services):
        self.services = services
        self.tasks: list[asyncio.Task] = []

    async def start(self) -> None:
        import os

        if os.getenv("AIOS_SALES_EMAIL_ENABLED") == "1":
            self.tasks = [asyncio.create_task(self.run()) for _ in range(2)]

    async def stop(self) -> None:
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)

    async def run(self) -> None:
        while True:
            try:
                factory = self.services.db.session_factory
                async with factory() as session:
                    await recover(session)
                async with factory() as session:
                    claimed = await claim(session)
                if claimed:
                    await deliver(factory, self.services.config, *claimed)
                    continue
            except asyncio.CancelledError:
                raise  # the durable lease becomes uncertain, never an automatic send
            except Exception as exc:
                logger.error("email worker iteration failed (%s)", type(exc).__name__)
            await asyncio.sleep(2)
