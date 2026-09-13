"""Bounded MIME extraction and deal-scoped views; sender headers are untrusted."""

from __future__ import annotations

import re
from datetime import timezone
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import undefer

from config.access import is_super
from core.domain.models import AuditLog
from core.runtime.core import Core
from core.runtime.deps import get_core, get_session
from core.services.auth import CurrentUser, require_permission
from modules.sales.access import DealAccess, get_deal_access, visible_deal_or_404
from modules.sales.mail_attachments import safe_filename, validate_content
from modules.sales.mail_models import IncomingEmail, OutgoingEmail
from modules.sales.mail_profiles import actor_identity
from modules.sales.mail_queue import ReplyContext, digest
from modules.sales.mail_transport import address

router = APIRouter()
MAX_PARTS = 128
MAX_HEADERS = 64 * 1024
MAX_BODY_CHARS = 100000
MAX_REFERENCES = 50
_MESSAGE_ID = re.compile(r"<[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+>")
_HEADER_NAMES = (
    "from", "to", "cc", "reply-to", "subject", "date", "message-id", "in-reply-to", "references"
)


def _text(value, limit: int) -> str:
    text = str(value or "")[:limit].encode("utf-8", "replace").decode("utf-8")
    return "".join(c for c in text if ord(c) >= 32 and ord(c) != 127 or c in "\n\t")


def parse_mime(raw: bytes) -> EmailMessage:
    # Reject excessive headers before the stdlib parser allocates header objects.
    ends = [p for marker in (b"\r\n\r\n", b"\n\n") if (p := raw.find(marker)) >= 0]
    if not ends or min(ends) > MAX_HEADERS:
        raise ValueError("invalid_header_block")
    count = 0

    def factory(**kwargs):
        nonlocal count
        count += 1
        if count > MAX_PARTS:
            raise ValueError("too_many_mime_parts")
        return EmailMessage(**kwargs)

    message = BytesParser(_class=factory, policy=policy.default).parsebytes(raw)
    for part in message.walk():
        if part.defects or sum(len(k) + len(v) for k, v in part.raw_items()) > MAX_HEADERS:
            raise ValueError("malformed_mime")
    return message


def _addresses(message, name: str, *, maximum: int = 50) -> list[str]:
    fields = message.get_all(name, [])
    if len(fields) > 1:
        raise ValueError("duplicate_address_header")
    if not fields:
        return []
    field = fields[0]
    if field.defects or len(field.addresses) > maximum:
        raise ValueError("ambiguous_address_header")
    return [address(item.addr_spec).casefold() for item in field.addresses]


def _ids(message, name: str, *, maximum: int = MAX_REFERENCES) -> list[str]:
    fields = message.get_all(name, [])
    if len(fields) > 1:
        raise ValueError("duplicate_reference_header")
    value = str(fields[0]) if fields else ""
    if len(value) > 8192:
        raise ValueError("reference_header_too_long")
    result = _MESSAGE_ID.findall(value)
    if len(result) > maximum or any(len(item) > 255 for item in result):
        raise ValueError("too_many_references")
    if _MESSAGE_ID.sub("", value).strip():
        raise ValueError("invalid_reference_header")
    return list(dict.fromkeys(result))


class _PlainHTML(HTMLParser):
    """Extract display text only; never load resources or interpret active content."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = 0
        self.remaining = MAX_BODY_CHARS

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.hidden += 1
        elif tag in {"br", "p", "div", "li", "tr"}:
            self.handle_data("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style"}:
            self.hidden = max(0, self.hidden - 1)
        elif tag in {"p", "div", "li", "tr"}:
            self.handle_data("\n")

    def handle_data(self, data):
        if not self.hidden and self.remaining:
            value = data[:self.remaining]
            self.parts.append(value)
            self.remaining -= len(value)


def extract_message(raw: bytes) -> tuple[dict, str | None]:
    """Keep the receipt even when parsing fails; damaged MIME needs human review."""
    result = {
        "sender": None, "to": [], "cc": [], "subject": "", "body_text": "", "headers": {},
        "message_date": None, "message_id": None, "in_reply_to": [], "references": [],
        "attachments": [],
    }
    try:
        message = parse_mime(raw)
        result["headers"] = {
            name: [_text(value, 8192) for value in message.get_all(name, [])[:2]]
            for name in _HEADER_NAMES if message.get_all(name)
        }
        if sum(len(v) for values in result["headers"].values() for v in values) > MAX_HEADERS:
            raise ValueError("too_many_headers")
        result["subject"] = _text(message.get("Subject"), 250)
        body = message.get_body(preferencelist=("plain", "html"))
        if body is not None:
            content = body.get_content()
            if body.get_content_type() == "text/html":
                parser = _PlainHTML()
                try:
                    parser.feed(content)
                    parser.close()
                except AssertionError as exc:
                    raise ValueError("malformed_html") from exc
                content = "".join(parser.parts)
            result["body_text"] = _text(content, MAX_BODY_CHARS)
        if message.get("Date"):
            try:
                date = parsedate_to_datetime(str(message["Date"]))
                if date.tzinfo is not None:
                    date = date.astimezone(timezone.utc)
                result["message_date"] = date.replace(tzinfo=None)
            except (ValueError, TypeError, OverflowError):
                pass  # Date is display-only and never determines identity or ordering.
        blocked_descendants = set()
        for part_index, part in enumerate(message.walk()):
            if id(part) in blocked_descendants:
                continue
            filename = part.get_filename()
            content_type = part.get_content_type()
            if part.is_multipart():
                if content_type.startswith("multipart/") and not filename and part.get_content_disposition() != "attachment":
                    continue
                # A forwarded message is one unsupported attachment, not a source
                # of independently trusted inner files. Do not hash reserialized MIME.
                blocked_descendants.update(id(child) for child in part.walk() if child is not part)
                index = len(result["attachments"])
                try:
                    name = safe_filename(filename or "", content_type)
                except (HTTPException, ValueError):
                    name = _text(filename, 250) or f"attachment-{index}.bin"
                result["attachments"].append({
                    "index": index, "part_index": part_index, "filename": name,
                    "content_type": _text(content_type, 128), "size": None, "sha256": None,
                    "downloadable": False, "blocked_reason": "unsafe_or_unsupported_attachment",
                })
                continue
            if not filename and part.get_content_disposition() != "attachment" and content_type in {
                "text/plain", "text/html"
            }:
                continue
            content = part.get_payload(decode=True) or b""
            index = len(result["attachments"])
            meta = {
                "index": index, "part_index": part_index, "filename": f"attachment-{index}.bin",
                "content_type": _text(content_type, 128), "size": len(content),
                "sha256": digest(content), "downloadable": False, "blocked_reason": None,
            }
            try:
                meta["filename"] = safe_filename(filename or "", content_type)
                validate_content(content, content_type, uploaded=True)
                if part.defects:
                    raise ValueError("malformed_attachment")
                meta["downloadable"] = True
            except (HTTPException, ValueError):
                meta["blocked_reason"] = "unsafe_or_unsupported_attachment"
            result["attachments"].append(meta)
        # Decoding a body or attachment can add defects (e.g. invalid base64).
        if any(part.defects for part in message.walk()):
            raise ValueError("malformed_mime")
        try:
            senders = _addresses(message, "from", maximum=1)
            if len(senders) != 1:
                raise ValueError("missing_sender")
            result["sender"] = senders[0]
            result["to"] = _addresses(message, "to")
            result["cc"] = _addresses(message, "cc")
        except ValueError:
            return result, "ambiguous_sender"
        try:
            ids = _ids(message, "message-id", maximum=1)
            result["message_id"] = ids[0] if ids else None
            result["in_reply_to"] = _ids(message, "in-reply-to")
            result["references"] = _ids(message, "references")
        except ValueError:
            return result, "invalid_references"
        return result, None
    except (ValueError, TypeError, LookupError, RecursionError, OverflowError):
        for attachment in result["attachments"]:
            attachment["downloadable"] = False
            attachment["blocked_reason"] = "malformed_mime"
        return result, "malformed_mime"


async def route_message(session, extracted: dict, problem: str | None) -> tuple[int | None, str]:
    if problem:
        return None, problem
    references = list(dict.fromkeys(extracted["in_reply_to"] + extracted["references"]))
    if not references:
        return None, "unknown_chain"
    outgoing = (await session.scalars(
        select(OutgoingEmail).where(OutgoingEmail.message_id.in_(references))
    )).all()
    if not outgoing:
        return None, "unknown_chain"
    if len({email.deal_id for email in outgoing}) != 1:
        return None, "conflicting_references"
    # A mere prepared draft has not left ERP and cannot establish an email thread.
    if any(email.accepted_at is None for email in outgoing):
        return None, "unconfirmed_chain"
    sender = extracted["sender"]
    try:
        sender_matches = sender and all(
            sender in {address(a).casefold() for a in email.to + email.cc} for email in outgoing
        )
    except ValueError:
        sender_matches = False
    if not sender_matches:
        return None, "sender_mismatch"
    return outgoing[0].deal_id, "technical_reference_match"


def incoming_summary(email: IncomingEmail, deal=None, *, detail=False) -> dict:
    result = {
        "receipt_id": email.id, "direction": "incoming", "deal_id": email.deal_id,
        "owner_id": deal.owner_id if deal else None, "owner": deal.owner if deal else None,
        "sender": email.sender, "to": email.to, "cc": email.cc, "subject": email.subject,
        "received_at": email.received_at.isoformat() + "Z",
        "message_date": email.message_date.isoformat() + "Z" if email.message_date else None,
        "message_id": email.message_id, "routing_status": email.routing_status,
        "routing_reason": email.routing_reason,
        "attachments": [{k: v for k, v in a.items() if k != "part_index"} for a in email.attachments],
    }
    if detail:
        result.update(body_text=email.body_text, headers=email.headers, raw_sha256=email.raw_sha256)
    return result


async def incoming_or_404(session, receipt_id: str, *, deal_id=None, detail=False, raw=False):
    stmt = select(IncomingEmail).where(
        IncomingEmail.id == receipt_id, IncomingEmail.deal_id == deal_id
    ).execution_options(populate_existing=True)
    if detail:
        stmt = stmt.options(undefer(IncomingEmail.body_text), undefer(IncomingEmail.headers))
    if raw:
        stmt = stmt.options(undefer(IncomingEmail.raw))
    email = await session.scalar(stmt)
    if email is None:
        raise HTTPException(404, "Письмо не найдено")
    return email


async def reply_context(session, deal_id: int, receipt_id: str | None, to: list[str]):
    if receipt_id is None:
        return None
    email = await incoming_or_404(session, receipt_id, deal_id=deal_id)
    if not email.sender or email.sender not in {address(a).casefold() for a in to}:
        raise HTTPException(422, "Явно укажите отправителя входящего письма в поле Кому")
    references = tuple(dict.fromkeys(email.references + email.in_reply_to))
    return ReplyContext(email.id, email.raw_sha256, email.message_id, references)


def download_attachment(email: IncomingEmail, index: int) -> Response:
    if not 0 <= index < len(email.attachments):
        raise HTTPException(404, "Вложение не найдено")
    meta = email.attachments[index]
    if not meta["downloadable"]:
        raise HTTPException(422, "Вложение заблокировано: тип или содержимое небезопасны")
    if digest(email.raw) != email.raw_sha256:
        raise HTTPException(409, "Контрольная сумма оригинала не совпадает")
    try:
        parts = list(parse_mime(email.raw).walk())
        part = parts[meta["part_index"]]
        content = part.get_payload(decode=True) or b""
        if part.defects or digest(content) != meta["sha256"]:
            raise ValueError("attachment_hash_mismatch")
        filename = safe_filename(meta["filename"], meta["content_type"])
        validate_content(content, meta["content_type"], uploaded=True)
    except (ValueError, TypeError, LookupError, RecursionError, OverflowError) as exc:
        raise HTTPException(409, "Состав вложений оригинала не совпадает") from exc
    return Response(content, media_type=meta["content_type"], headers={
        "Content-Disposition": f"attachment; filename=attachment; filename*=UTF-8''{quote(filename)}",
        "X-Content-Type-Options": "nosniff", "Cache-Control": "no-store",
    })


async def _list_incoming(session, *, deal=None, limit=50, offset=0):
    rows = (await session.scalars(
        select(IncomingEmail).where(IncomingEmail.deal_id == (deal.id if deal else None))
        .order_by(IncomingEmail.received_at.desc(), IncomingEmail.id.desc())
        .offset(offset).limit(limit + 1)
    )).all()
    return {
        "items": [incoming_summary(email, deal) for email in rows[:limit]],
        "next_offset": offset + limit if len(rows) > limit else None,
    }


@router.get("/deals/{deal_id}/incoming-emails")
async def list_incoming_emails(
    deal_id: int, limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0, le=1000000),
    session: AsyncSession = Depends(get_session),
    _: CurrentUser = Depends(require_permission("sales.deal.read")),
    access: DealAccess = Depends(get_deal_access),
):
    deal = await visible_deal_or_404(session, deal_id, access)
    return await _list_incoming(session, deal=deal, limit=limit, offset=offset)


@router.get("/deals/{deal_id}/incoming-emails/{receipt_id}")
async def get_incoming_email(
    deal_id: int, receipt_id: str, session: AsyncSession = Depends(get_session),
    _: CurrentUser = Depends(require_permission("sales.deal.read")),
    access: DealAccess = Depends(get_deal_access),
):
    deal = await visible_deal_or_404(session, deal_id, access)
    email = await incoming_or_404(session, receipt_id, deal_id=deal_id, detail=True)
    return incoming_summary(email, deal, detail=True)


@router.get("/deals/{deal_id}/incoming-emails/{receipt_id}/attachments/{index}")
async def incoming_attachment(
    deal_id: int, receipt_id: str, index: int, session: AsyncSession = Depends(get_session),
    _: CurrentUser = Depends(require_permission("sales.deal.read")),
    access: DealAccess = Depends(get_deal_access),
):
    await visible_deal_or_404(session, deal_id, access)
    email = await incoming_or_404(session, receipt_id, deal_id=deal_id, raw=True)
    return download_attachment(email, index)


def require_inbox_user(
    user: CurrentUser = Depends(require_permission("sales.deal.approve")),
) -> CurrentUser:
    if "sales_head" not in user.roles and not is_super(user.roles):
        raise HTTPException(403, "Разбор почты доступен только руководителю продаж")
    return user


@router.get("/mail/inbox")
async def incoming_inbox(
    limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0, le=1000000),
    session: AsyncSession = Depends(get_session), _: CurrentUser = Depends(require_inbox_user),
    access: DealAccess = Depends(get_deal_access),
):
    return await _list_incoming(session, limit=limit, offset=offset)


@router.get("/mail/inbox/{receipt_id}")
async def inbox_email(
    receipt_id: str, session: AsyncSession = Depends(get_session),
    _: CurrentUser = Depends(require_inbox_user), access: DealAccess = Depends(get_deal_access),
):
    email = await incoming_or_404(session, receipt_id, detail=True)
    return incoming_summary(email, detail=True)


@router.get("/mail/inbox/{receipt_id}/attachments/{index}")
async def inbox_attachment(
    receipt_id: str, index: int, session: AsyncSession = Depends(get_session),
    _: CurrentUser = Depends(require_inbox_user), access: DealAccess = Depends(get_deal_access),
):
    email = await incoming_or_404(session, receipt_id, raw=True)
    return download_attachment(email, index)


class AssignEmail(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    deal_id: int = Field(gt=0)


@router.post("/mail/inbox/{receipt_id}/assign")
async def assign_incoming_email(
    receipt_id: str, payload: AssignEmail, core: Core = Depends(get_core),
    session: AsyncSession = Depends(get_session), user: CurrentUser = Depends(require_inbox_user),
    access: DealAccess = Depends(get_deal_access),
):
    deal = await visible_deal_or_404(session, payload.deal_id, access)
    actor = actor_identity(user, core.services.config)
    row = await session.execute(
        update(IncomingEmail).where(IncomingEmail.id == receipt_id, IncomingEmail.deal_id.is_(None))
        .values(deal_id=deal.id, routing_status="assigned", routing_reason="manual_assignment")
    )
    if row.rowcount:
        session.add(AuditLog(
            actor=actor, action="sales.email.incoming_assigned", entity_ref=f"deal:{deal.id}",
            detail={"receipt_id": receipt_id, "previous_deal_id": None, "deal_id": deal.id},
        ))
    else:
        existing = await session.scalar(select(IncomingEmail).where(IncomingEmail.id == receipt_id))
        if existing is None:
            raise HTTPException(404, "Письмо не найдено")
        if existing.deal_id != deal.id:
            raise HTTPException(409, "Письмо уже назначено: обновите очередь")
    await session.commit()
    email = await incoming_or_404(session, receipt_id, deal_id=deal.id)
    return incoming_summary(email, deal)
