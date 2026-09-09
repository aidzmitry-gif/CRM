"""Dedicated token-authenticated transport receipt; no SMTP or business commands."""

from __future__ import annotations

import base64
import binascii
import hmac
import json
import os
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from core.domain.models import AuditLog
from core.runtime.deps import get_session
from modules.sales.mail_models import IncomingEmail
from modules.sales.mail_queue import digest, now
from modules.sales.mail_routing import extract_message, route_message
from modules.sales.mail_transport import _read_private_password
from ops.sales_mail.config import MAILBOX, MAX_BYTES
from ops.sales_mail.relay import receipt_id

# An empty registration prefix keeps this dedicated machine endpoint out of
# Sales UI RBAC. Its own mandatory token check applies in dev and OIDC modes.
router = APIRouter()
MAX_BASE64_BYTES = 4 * ((MAX_BYTES + 2) // 3)
MAX_REQUEST_BYTES = MAX_BASE64_BYTES + 4096


class InboundPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    mailbox: Literal["order@microchips.by"]
    uidvalidity: int = Field(gt=0, le=4294967295)
    uid: int = Field(gt=0, le=4294967295)
    raw_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    raw_base64: str = Field(min_length=1, max_length=MAX_BASE64_BYTES)


def _unique_json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_payload_field")
        result[key] = value
    return result


def require_inbound_token(request: Request) -> None:
    path = os.getenv("AIOS_SALES_MAIL_INBOUND_TOKEN_FILE", "").strip()
    try:
        # Reuse the existing bounded, regular/private-file credential reader.
        token = _read_private_password(path) if path else ""
        if not token or any(not 33 <= ord(c) <= 126 for c in token):
            raise ValueError("invalid_token_file")
    except ValueError as exc:
        raise HTTPException(503, "Приём почты не настроен") from exc
    supplied = request.headers.getlist("X-Sales-Mail-Token")
    if len(supplied) != 1 or not hmac.compare_digest(supplied[0].encode("utf-8"), token.encode("ascii")):
        raise HTTPException(401, "Неверный токен приёма почты")


async def read_payload(request: Request) -> tuple[InboundPayload, bytes]:
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
        raise HTTPException(415, "Ожидается application/json")
    if request.headers.get("content-encoding", "identity").lower() != "identity":
        raise HTTPException(415, "Кодирование тела не поддерживается")
    length = request.headers.get("content-length")
    if length is not None:
        try:
            size = int(length)
            if size < 0:
                raise ValueError("negative_length")
        except ValueError as exc:
            raise HTTPException(400, "Некорректная длина тела") from exc
        if size > MAX_REQUEST_BYTES:
            raise HTTPException(413, "Размер оригинала превышает 32 МиБ")
    data = bytearray()
    async for chunk in request.stream():
        if len(data) + len(chunk) > MAX_REQUEST_BYTES:
            raise HTTPException(413, "Размер оригинала превышает 32 МиБ")
        data.extend(chunk)
    try:
        payload = InboundPayload.model_validate(json.loads(data, object_pairs_hook=_unique_json_object))
        raw = base64.b64decode(payload.raw_base64.encode("ascii"), validate=True)
        if base64.b64encode(raw).decode("ascii") != payload.raw_base64:
            raise ValueError("noncanonical_base64")
    except (ValidationError, ValueError, UnicodeError, binascii.Error, RecursionError) as exc:
        # Do not echo MIME, tokens or Pydantic input values in error responses.
        raise HTTPException(422, "Некорректная квитанция приёма почты") from exc
    if not raw or len(raw) > MAX_BYTES:
        raise HTTPException(413, "Недопустимый размер оригинала")
    if digest(raw) != payload.raw_sha256:
        raise HTTPException(422, "Контрольная сумма оригинала не совпадает")
    return payload, raw


async def accept_message(session, payload: InboundPayload, raw: bytes) -> tuple[IncomingEmail, bool]:
    rid = receipt_id(MAILBOX, payload.uidvalidity, payload.uid)
    existing = await session.scalar(select(IncomingEmail).where(IncomingEmail.id == rid))
    if existing is not None:
        if existing.raw_sha256 != payload.raw_sha256:
            raise HTTPException(409, "Для этой квитанции уже сохранён другой оригинал")
        return existing, False
    extracted, problem = extract_message(raw)
    deal_id, reason = await route_message(session, extracted, problem)
    email = IncomingEmail(
        id=rid, mailbox=MAILBOX, uidvalidity=payload.uidvalidity, uid=payload.uid,
        raw=raw, raw_sha256=payload.raw_sha256, received_at=now(), deal_id=deal_id,
        routing_status="matched" if deal_id is not None else "unresolved", routing_reason=reason,
        **extracted,
    )
    try:
        async with session.begin_nested():
            session.add(email)
            await session.flush()
    except IntegrityError:
        # The unique identity serializes competing deliveries. A savepoint
        # leaves the caller's transaction usable for the winning receipt read.
        existing = await session.scalar(select(IncomingEmail).where(IncomingEmail.id == rid))
        if existing is None or existing.raw_sha256 != payload.raw_sha256:
            raise HTTPException(409, "Конфликт квитанции приёма почты") from None
        return existing, False
    session.add(AuditLog(
        actor="sales-mail-intake", action="sales.email.incoming_received",
        entity_ref=f"deal:{deal_id}" if deal_id is not None else rid,
        detail={"receipt_id": rid, "raw_sha256": payload.raw_sha256, "routing_reason": reason},
    ))
    return email, True


@router.post("/integrations/sales-mail/v1", dependencies=[Depends(require_inbound_token)])
async def receive_sales_mail(request: Request, session: AsyncSession = Depends(get_session)):
    payload, raw = await read_payload(request)
    email, created = await accept_message(session, payload, raw)
    receipt = {"receipt_id": email.id, "raw_sha256": email.raw_sha256}
    await session.commit()  # Acknowledgement must follow durable storage.
    return JSONResponse(receipt, status_code=201 if created else 200)
