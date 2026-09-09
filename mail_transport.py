"""SMTP acceptance, never an assertion of inbox delivery. No secrets in results."""

from __future__ import annotations

import re
import smtplib
import ssl
from dataclasses import dataclass, field

MAX_MESSAGE_BYTES = 20 * 1024 * 1024  # includes MIME/base64 overhead
MAX_RECIPIENTS = 10


def address(value: str) -> str:
    """Accept a single plain mailbox, no display names or header syntax."""
    if not isinstance(value, str) or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError("Некорректный email")
    value = value.strip()
    if len(value) > 254 or value.count("@") != 1:
        raise ValueError("Некорректный email")
    local, domain = value.rsplit("@", 1)
    if (
        not re.fullmatch(
            r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+(?:\.[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+)*", local
        )
        or len(local) > 64
    ):
        raise ValueError("Некорректный email")
    try:
        domain = domain.encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise ValueError("Некорректный email") from exc
    if len(f"{local}@{domain}") > 254:
        raise ValueError("Некорректный email")
    labels = domain.split(".")
    if len(labels) < 2 or any(
        not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", p) for p in labels
    ):
        raise ValueError("Некорректный email")
    return f"{local}@{domain}"


def recipients(to: list[str], cc: list[str]) -> tuple[list[str], list[str]]:
    if not to or len(to) + len(cc) > MAX_RECIPIENTS:
        raise ValueError("Укажите от 1 до 10 получателей, включая хотя бы одного в To")
    seen: set[str] = set()
    output: list[list[str]] = [[], []]
    for group, values in zip(output, (to, cc), strict=True):
        for value in values:
            normalized = address(value)
            if normalized.casefold() not in seen:
                group.append(normalized)
                seen.add(normalized.casefold())
    return output[0], output[1]


def configured_sender(settings) -> str:
    if not settings.smtp_host:
        raise ValueError("Корпоративный SMTP не настроен")
    sender = address(settings.smtp_from)
    if sender.endswith("@aios.local"):
        raise ValueError("Корпоративный отправитель не настроен")
    if not settings.smtp_tls and settings.smtp_host not in {"127.0.0.1", "::1", "localhost"}:
        raise ValueError("Для корпоративного SMTP требуется TLS")
    return sender


@dataclass(frozen=True)
class SMTPResult:
    status: str  # accepted | retry_wait | failed | uncertain
    reason: str
    code: int | None = None
    recipients: dict[str, int] = field(default_factory=dict)


def _refused(code: int, reason: str, results=None) -> SMTPResult:
    return SMTPResult("retry_wait" if 400 <= code < 500 else "failed", reason, code, results or {})


def submit(settings, sender: str, targets: list[str], mime: bytes) -> SMTPResult:
    """One envelope, fixed MIME bytes; abort before DATA if any recipient refuses.

    A crash during this call is recovered conservatively as uncertain by the
    durable worker. Only explicit SMTP refusal is safe to retry after DATA.
    Never include raw server replies, exception strings or AUTH in the audit.
    """
    if len(mime) > MAX_MESSAGE_BYTES:
        return SMTPResult("failed", "message_too_large")
    smtp = None
    data_started = False
    rcpts: dict[str, int] = {}
    try:
        if configured_sender(settings) != sender:
            return SMTPResult("failed", "sender_configuration_changed")
        address(sender)
        for target in targets:
            address(target)
        if not targets:
            return SMTPResult("failed", "invalid_recipient")
        if settings.smtp_port == 465:
            smtp = smtplib.SMTP_SSL(
                settings.smtp_host,
                settings.smtp_port,
                timeout=15,
                context=ssl.create_default_context(),
            )
        else:
            smtp = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=15)
        code, _ = smtp.ehlo()
        if code != 250:
            return _refused(code, "greeting_rejected")
        if settings.smtp_tls and settings.smtp_port != 465:
            smtp.starttls(context=ssl.create_default_context())
            code, _ = smtp.ehlo()
            if code != 250:
                return _refused(code, "greeting_rejected")
        if settings.smtp_user:
            smtp.login(settings.smtp_user, settings.smtp_password)
        limit = smtp.esmtp_features.get("size", "")
        if limit.isdigit() and int(limit) > 0 and len(mime) > int(limit):
            return SMTPResult("failed", "server_size_limit")
        options = [f"size={len(mime)}"] if smtp.has_extn("size") else []
        code, _ = smtp.mail(sender, options)
        if code != 250:
            return _refused(code, "sender_rejected")
        for target in targets:
            code, _ = smtp.rcpt(target)
            rcpts[target] = code
        rejected = [c for c in rcpts.values() if c not in (250, 251)]
        if rejected:
            # Closing before DATA cancels all accepted RCPT commands too.
            permanent = next((c for c in rejected if not 400 <= c < 500), None)
            return _refused(permanent or rejected[0], "recipient_rejected", rcpts)
        data_started = True
        code, _ = smtp.data(mime)
        if code != 250:
            return _refused(code, "data_rejected", rcpts)
        return SMTPResult("accepted", "smtp_accepted", code, rcpts)
    except smtplib.SMTPAuthenticationError as exc:
        return _refused(exc.smtp_code, "authentication_rejected")
    except smtplib.SMTPResponseException as exc:
        return _refused(exc.smtp_code, "data_rejected" if data_started else "smtp_rejected", rcpts)
    except (ssl.SSLError, smtplib.SMTPNotSupportedError, ValueError):
        return SMTPResult(
            "uncertain" if data_started else "failed",
            "response_unknown" if data_started else "configuration_error",
            recipients=rcpts,
        )
    except (OSError, smtplib.SMTPServerDisconnected):
        return SMTPResult(
            "uncertain" if data_started else "retry_wait",
            "response_unknown" if data_started else "connection_error",
            recipients=rcpts,
        )
    except Exception:
        return SMTPResult(
            "uncertain" if data_started else "failed", "transport_error", recipients=rcpts
        )
    finally:
        # QUIT can fail after acceptance: it must never turn 250 into a retry.
        if smtp is not None:
            try:
                smtp.close()
            except Exception:
                pass
