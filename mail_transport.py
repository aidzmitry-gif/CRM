"""SMTP acceptance, never an assertion of inbox delivery. No secrets in results."""

from __future__ import annotations

import os
import re
import smtplib
import ssl
import stat
from dataclasses import dataclass, field
from pathlib import Path

MAX_MESSAGE_BYTES = 20 * 1024 * 1024  # includes MIME/base64 overhead
MAX_RECIPIENTS = 10
_SALES_ENV = {
    "host": "AIOS_SALES_SMTP_HOST",
    "port": "AIOS_SALES_SMTP_PORT",
    "user": "AIOS_SALES_SMTP_USER",
    "sender": "AIOS_SALES_SMTP_FROM",
    "tls": "AIOS_SALES_SMTP_TLS",
    "password_file": "AIOS_SALES_SMTP_PASSWORD_FILE",
}


@dataclass(frozen=True)
class SalesSMTPConfig:
    host: str
    port: int
    user: str
    password: str
    sender: str
    tls: bool


def _read_private_password(path_value: str) -> str:
    path = Path(path_value).expanduser()
    try:
        info = path.lstat()
    except OSError as exc:
        raise ValueError("Файл пароля SMTP недоступен") from exc
    if not stat.S_ISREG(info.st_mode) or path.is_symlink() or info.st_size > 4096:
        raise ValueError("Файл пароля SMTP должен быть закрытым обычным файлом")
    if os.name != "nt" and stat.S_IMODE(info.st_mode) & 0o077:
        raise ValueError("Файл пароля SMTP должен быть доступен только владельцу")
    try:
        return path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError) as exc:
        raise ValueError("Файл пароля SMTP недоступен") from exc


def _parse_bool(value: str) -> bool:
    normalized = value.strip().casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError("Некорректный dedicated SMTP TLS-параметр")


def sales_smtp_config(settings) -> SalesSMTPConfig:
    """Resolve sales-only SMTP overrides without mutating global settings."""
    present = {key: os.environ[name] for key, name in _SALES_ENV.items() if name in os.environ}
    if not present:
        return SalesSMTPConfig(
            host=str(settings.smtp_host or ""),
            port=int(settings.smtp_port),
            user=str(settings.smtp_user or ""),
            password=str(settings.smtp_password or ""),
            sender=str(settings.smtp_from or ""),
            tls=bool(settings.smtp_tls),
        )
    if set(present) != set(_SALES_ENV):
        raise ValueError("Dedicated SMTP для продаж настроен не полностью")
    try:
        port = int(present["port"])
    except (TypeError, ValueError) as exc:
        raise ValueError("Некорректный dedicated SMTP порт") from exc
    if not 1 <= port <= 65535:
        raise ValueError("Некорректный dedicated SMTP порт")
    password_file = present["password_file"].strip()
    if not password_file:
        raise ValueError("Файл пароля SMTP не настроен")
    return SalesSMTPConfig(
        host=present["host"].strip(),
        port=port,
        user=present["user"].strip(),
        password=_read_private_password(password_file),
        sender=present["sender"].strip(),
        tls=_parse_bool(present["tls"]),
    )


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


def _configured_sender(config: SalesSMTPConfig) -> str:
    if not config.host:
        raise ValueError("Корпоративный SMTP не настроен")
    sender = address(config.sender)
    if sender.endswith("@aios.local"):
        raise ValueError("Корпоративный отправитель не настроен")
    if not config.tls and config.host not in {"127.0.0.1", "::1", "localhost"}:
        raise ValueError("Для корпоративного SMTP требуется TLS")
    return sender


def configured_sender(settings) -> str:
    return _configured_sender(sales_smtp_config(settings))


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
    try:
        config = sales_smtp_config(settings)
    except ValueError:
        return SMTPResult("failed", "configuration_error")
    smtp = None
    data_started = False
    rcpts: dict[str, int] = {}
    try:
        if _configured_sender(config) != sender:
            return SMTPResult("failed", "sender_configuration_changed")
        address(sender)
        for target in targets:
            address(target)
        if not targets:
            return SMTPResult("failed", "invalid_recipient")
        if config.port == 465:
            smtp = smtplib.SMTP_SSL(
                config.host,
                config.port,
                timeout=15,
                context=ssl.create_default_context(),
            )
        else:
            smtp = smtplib.SMTP(config.host, config.port, timeout=15)
        code, _ = smtp.ehlo()
        if code != 250:
            return _refused(code, "greeting_rejected")
        if config.tls and config.port != 465:
            smtp.starttls(context=ssl.create_default_context())
            code, _ = smtp.ehlo()
            if code != 250:
                return _refused(code, "greeting_rejected")
        if config.user:
            smtp.login(config.user, config.password)
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
