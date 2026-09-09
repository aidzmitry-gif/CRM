"""Durable email bytes and append-only attempt records; no credentials."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from core.db.base import Base


class OutgoingEmail(Base):
    __tablename__ = "outgoing_email"
    __table_args__ = (
        UniqueConstraint("created_by", "request_key", name="uq_outgoing_email_request"),
        Index("ix_outgoing_email_due", "status", "next_attempt_at"),
        {"schema": "sales"},
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    deal_id: Mapped[int] = mapped_column(Integer, ForeignKey("sales.deal.id"), index=True)
    request_key: Mapped[str] = mapped_column(String(64))
    request_hash: Mapped[str] = mapped_column(String(64))
    created_by: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime)
    confirmed_by: Mapped[str | None] = mapped_column(String(128))
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime)
    sender: Mapped[str] = mapped_column(String(254))
    to: Mapped[list] = mapped_column(JSON)
    cc: Mapped[list] = mapped_column(JSON)
    subject: Mapped[str] = mapped_column(String(250))
    body: Mapped[str] = mapped_column(Text)
    attachments: Mapped[list] = mapped_column(JSON)
    mime: Mapped[bytes] = mapped_column(LargeBinary, deferred=True)
    mime_sha256: Mapped[str] = mapped_column(String(64))
    message_id: Mapped[str] = mapped_column(String(255), unique=True)
    status: Mapped[str] = mapped_column(String(24), default="prepared")
    attempt_count: Mapped[int] = mapped_column(default=0)
    round_attempts: Mapped[int] = mapped_column(default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime)
    claim_token: Mapped[str | None] = mapped_column(String(36))
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime)
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_reason: Mapped[str | None] = mapped_column(String(64))
    reply_to_receipt_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("sales.incoming_email.id")
    )


class IncomingEmail(Base):
    """Immutable transport receipt and extracted, untrusted content."""

    __tablename__ = "incoming_email"
    __table_args__ = (
        UniqueConstraint("mailbox", "uidvalidity", "uid", name="uq_incoming_email_identity"),
        CheckConstraint("uidvalidity > 0 AND uid > 0", name="incoming_email_positive_uid"),
        Index("ix_incoming_email_inbox", "deal_id", "received_at"),
        {"schema": "sales"},
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    mailbox: Mapped[str] = mapped_column(String(254))
    uidvalidity: Mapped[int] = mapped_column(BigInteger)
    uid: Mapped[int] = mapped_column(BigInteger)
    raw: Mapped[bytes] = mapped_column(LargeBinary, deferred=True)
    raw_sha256: Mapped[str] = mapped_column(String(64))
    received_at: Mapped[datetime] = mapped_column(DateTime)
    message_date: Mapped[datetime | None] = mapped_column(DateTime)
    sender: Mapped[str | None] = mapped_column(String(254))
    to: Mapped[list] = mapped_column(JSON)
    cc: Mapped[list] = mapped_column(JSON)
    subject: Mapped[str] = mapped_column(String(250))
    body_text: Mapped[str] = mapped_column(Text, deferred=True)
    headers: Mapped[dict] = mapped_column(JSON, deferred=True)
    message_id: Mapped[str | None] = mapped_column(String(255))
    in_reply_to: Mapped[list] = mapped_column(JSON)
    references: Mapped[list] = mapped_column(JSON)
    attachments: Mapped[list] = mapped_column(JSON)
    deal_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("sales.deal.id"))
    routing_status: Mapped[str] = mapped_column(String(24))
    routing_reason: Mapped[str] = mapped_column(String(64))


class EmailAttempt(Base):
    __tablename__ = "email_attempt"
    __table_args__ = (
        UniqueConstraint("email_id", "number", name="uq_email_attempt_number"),
        {"schema": "sales"},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    email_id: Mapped[str] = mapped_column(ForeignKey("sales.outgoing_email.id"), index=True)
    number: Mapped[int] = mapped_column()
    started_at: Mapped[datetime] = mapped_column(DateTime)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    status: Mapped[str] = mapped_column(String(24))
    reason: Mapped[str | None] = mapped_column(String(64))
    smtp_code: Mapped[int | None] = mapped_column()
    recipients: Mapped[dict] = mapped_column(JSON, default=dict)
