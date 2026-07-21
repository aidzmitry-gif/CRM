"""Окно входящего звонка (SALES-50) — журнал звонков, резолв продавца, SSE-push.

Подписчик доменных событий телефонии (``telephony.call.incoming/answered/ended/
transfer`` от коннектора ``integrations``): склеивает события одного вызова по
``call_id`` в одну запись ``CallLog``, резолвит ответственного продавца по номеру
(A.2) и пушит карточку этому продавцу через in-process SSE-реестр.

Резолв owner — серверная логика sales (знает сделки/контрагентов); коннектор
телефонии про owner не знает (правило границ, §2.4).
"""
from __future__ import annotations

import asyncio
import logging
import re
from collections import defaultdict
from datetime import datetime, timezone

from sqlalchemy import select

from core.domain.models import Contact, Counterparty
from modules.sales.models import CallLog, Deal
from modules.sales.stages import TERMINAL_STAGES

logger = logging.getLogger("aios.sales.calls")


def _utcnow() -> datetime:
    # наивный UTC — единообразно для SQLite и PostgreSQL (как в routes.py)
    return datetime.now(timezone.utc).replace(tzinfo=None)


# --- SSE-реестр подписок «owner → очереди карточек» (in-process) ------------------
# ponytail: in-process реестр (один воркер). При масштабировании — Redis pub/sub
# (заложено в ТЗ A.5), сейчас не реализуем.
_subscribers: dict[str, set[asyncio.Queue]] = defaultdict(set)


def subscribe(owner: str) -> asyncio.Queue:
    """Подписать продавца на поток карточек звонков; вернуть его очередь."""
    queue: asyncio.Queue = asyncio.Queue(maxsize=100)
    _subscribers[owner].add(queue)
    return queue


def unsubscribe(owner: str, queue: asyncio.Queue) -> None:
    subs = _subscribers.get(owner)
    if subs is not None:
        subs.discard(queue)
        if not subs:
            _subscribers.pop(owner, None)


def has_subscriber(owner: str) -> bool:
    return bool(_subscribers.get(owner))


def _card(call: CallLog) -> dict:
    """Карточка звонка для SSE (плоский dict, без ORM-объекта)."""
    return {
        "id": call.id,
        "call_id": call.call_id,
        "direction": call.direction,
        "phone": call.phone_e164,
        "did": call.did,
        "agent_ext": call.agent_ext,
        "owner": call.owner,
        "counterparty_id": call.counterparty_id,
        "contact_id": call.contact_id,
        "deal_id": call.deal_id,
        "status": call.status,
        "duration_sec": call.duration_sec,
        "recording_url": call.recording_url,
    }


def _push_card(call: CallLog) -> bool:
    """Положить карточку звонка в очереди подписок резолвленного продавца."""
    if not call.owner:
        return False  # новый номер / дежурный пул без подписки — карточка не пушится
    card = _card(call)
    delivered = False
    for queue in list(_subscribers.get(call.owner, ())):
        try:
            queue.put_nowait(card)
            delivered = True
        except asyncio.QueueFull:
            logger.warning("calls: очередь подписки переполнена (owner=%s)", call.owner)
    return delivered


# --- Резолв продавца по номеру (A.2) ----------------------------------------------
def _digits_tail(phone: str | None, length: int = 9) -> str:
    """Значащий хвост номера для матчинга (последние ``length`` цифр)."""
    return re.sub(r"\D", "", phone or "")[-length:]


async def _deal_context(session, cp_name: str) -> tuple[str, int | None]:
    """Owner и сделка по контрагенту.

    Открытая (не терминальная) сделка → ``(owner, deal_id)`` — звонок вешаем на неё.
    Только закрытые → ``(owner, None)``: история клиента остаётся через
    ``counterparty_id``, в сделку не пишем.
    """
    active = (
        await session.execute(
            select(Deal)
            .where(Deal.counterparty == cp_name, Deal.owner != "", Deal.stage.notin_(TERMINAL_STAGES))
            .order_by(Deal.created_at.desc())
        )
    ).scalars().first()
    if active is not None:
        return active.owner, active.id
    closed = (
        await session.execute(
            select(Deal).where(Deal.counterparty == cp_name, Deal.owner != "").order_by(Deal.created_at.desc())
        )
    ).scalars().first()
    return (closed.owner if closed is not None else ""), None


async def resolve_owner(session, phone_e164: str | None) -> dict:
    """Резолв продавца/сделки по номеру (A.2): контакт → контрагент → сделки.

    Возвращает ``{owner, owner_id, counterparty_id, contact_id, deal_id}``.
    ``deal_id`` — только открытая сделка в воронке; иначе ``None`` (звонок всё равно
    в истории клиента через ``counterparty_id``). Матч номера — по хвосту 9 цифр.
    ponytail: при росте базы — нормализованная колонка + индекс вместо LIKE-скана.

    Неизвестный номер → пустой owner / без deal (дежурный пул); лид создаёт модуль
    leads по ``sales.call.logged``.
    """
    result: dict = {
        "owner": "",
        "owner_id": None,
        "counterparty_id": None,
        "contact_id": None,
        "deal_id": None,
    }
    tail = _digits_tail(phone_e164)
    if not tail:
        return result

    contact = (
        await session.execute(
            select(Contact)
            .where(Contact.phone.isnot(None), Contact.phone.like(f"%{tail}"))
            .order_by(Contact.is_primary.desc(), Contact.id)
        )
    ).scalars().first()
    if contact is not None:
        result["contact_id"] = contact.id
        result["counterparty_id"] = contact.counterparty_id
        if contact.counterparty_id is not None:
            cp = await session.get(Counterparty, contact.counterparty_id)
            if cp is not None:
                owner, deal_id = await _deal_context(session, cp.name)
                result["deal_id"] = deal_id
                if owner:
                    result["owner"] = owner

    return result


# --- Апсерт записи звонка по событию ----------------------------------------------
async def record_event(session, payload: dict, event_type: str) -> tuple[CallLog | None, bool]:
    """Применить событие телефонии к ``CallLog`` (апсерт по ``call_id``).

    Возвращает ``(call, created)``. ``created`` — была ли запись создана этим событием
    (используется, чтобы резолвить owner и эмитить ``sales.call.logged`` один раз).
    """
    call_id = (payload.get("call_id") or "").strip()
    if not call_id:
        return None, False

    call = (
        await session.execute(select(CallLog).where(CallLog.call_id == call_id))
    ).scalars().first()
    created = False
    if call is None:
        call = CallLog(
            call_id=call_id,
            direction=payload.get("direction") or "in",
            phone_e164=payload.get("phone_e164"),
            did=payload.get("did"),
            agent_ext=payload.get("agent_ext"),
            status="ringing",
            started_at=_utcnow(),
        )
        owner = await resolve_owner(session, call.phone_e164)
        call.owner = owner["owner"]
        call.owner_id = owner["owner_id"]
        call.counterparty_id = owner["counterparty_id"]
        call.contact_id = owner["contact_id"]
        call.deal_id = owner["deal_id"]  # только открытая сделка; иначе None
        session.add(call)
        await session.flush()
        created = True

    # дозаполнить поля, приходящие на более поздних этапах (дозвон/ответ несут code/did)
    if payload.get("agent_ext") and not call.agent_ext:
        call.agent_ext = payload["agent_ext"]
    if payload.get("did") and not call.did:
        call.did = payload["did"]

    if event_type == "telephony.call.answered":
        # не понижаем терминальный статус, если answer пришёл/переставлен после hangup
        if call.ended_at is None:
            call.status = "answered"
        call.answered_at = call.answered_at or _utcnow()
    elif event_type == "telephony.call.ended":
        provider_status = payload.get("status")
        if payload.get("event") == "misscall" or provider_status == "no_answer":
            call.status = "missed"
        elif provider_status == "busy":
            call.status = "busy"
        elif provider_status == "failed":
            call.status = "failed"
        else:
            call.status = "ended"
        call.ended_at = _utcnow()
        if payload.get("duration_sec") is not None:
            call.duration_sec = payload["duration_sec"]
        if payload.get("hold_sec") is not None:
            call.hold_sec = payload["hold_sec"]
        if payload.get("recording_url"):
            call.recording_url = payload["recording_url"]
    elif event_type == "telephony.call.transfer" and payload.get("to_ext"):
        note = f"Перевод на {payload['to_ext']}"
        call.comment = f"{call.comment}; {note}" if call.comment else note

    return call, created


# --- Обработчики событий шины (ctx = сессия relay + сервисы) -----------------------
def _emit_logged(ctx, call: CallLog) -> None:
    """Эмитнуть ``sales.call.logged`` (→ audit, KPI-активность) для новой записи звонка.

    Любой первый по ``call_id`` звонок логируется ровно один раз — на каком бы этапе он
    ни «появился»: incoming, либо сразу misscall/answered/transfer, если предыдущие
    события потеряны/переставлены (webhook публичен). Иначе пропущенные и внепорядковые
    звонки не попали бы в аудит/KPI, хотя запись в журнале есть."""
    ctx.services.event_bus.emit(
        ctx.session,
        "sales.call.logged",
        {
            "call_id": call.call_id,
            "direction": call.direction,
            "owner": call.owner,
            "agent_ext": call.agent_ext,  # кто поднял трубку — репо лидов заводит лид на него
            "phone": call.phone_e164,
            "deal_id": call.deal_id,
            "actor": "telephony",
            "entity_ref": f"call:{call.call_id}",
        },
    )
    logger.info("calls: звонок %s → %s (owner=%s)", call.call_id, call.status, call.owner or "—")


async def on_incoming_call(payload: dict, ctx) -> None:
    """Входящий/исходящий старт звонка → запись в журнал + push карточки продавцу."""
    if ctx is None:
        return
    call, created = await record_event(ctx.session, payload, "telephony.call.incoming")
    if call is None:
        return
    if created:
        _emit_logged(ctx, call)
    _push_card(call)


async def on_call_answered(payload: dict, ctx) -> None:
    if ctx is None:
        return
    call, created = await record_event(ctx.session, payload, "telephony.call.answered")
    if call is None:
        return
    if created:
        _emit_logged(ctx, call)
    _push_card(call)


async def on_call_ended(payload: dict, ctx) -> None:
    """Завершение разговора → обновить статус/длительность/запись + событие ``sales.call.ended``."""
    if ctx is None:
        return
    call, created = await record_event(ctx.session, payload, "telephony.call.ended")
    if call is None:
        return
    if created:  # пропущенный/внепорядковый: запись появилась сразу с hangup/misscall
        _emit_logged(ctx, call)
    ctx.services.event_bus.emit(
        ctx.session,
        "sales.call.ended",
        {
            "call_id": call.call_id,
            "status": call.status,
            "duration_sec": call.duration_sec,
            "owner": call.owner,
            "deal_id": call.deal_id,
            "actor": "telephony",
            "entity_ref": f"call:{call.call_id}",
        },
    )
    _push_card(call)


async def on_call_transfer(payload: dict, ctx) -> None:
    if ctx is None:
        return
    call, created = await record_event(ctx.session, payload, "telephony.call.transfer")
    if call is None:
        return
    if created:
        _emit_logged(ctx, call)
    _push_card(call)


# Карта обработчиков по типу события — для синхронного приёма через эндпоинт-fallback
# (``POST /sales/telephony/incoming``) и для подписки в module.register().
EVENT_HANDLERS = {
    "telephony.call.incoming": on_incoming_call,
    "telephony.call.answered": on_call_answered,
    "telephony.call.ended": on_call_ended,
    "telephony.call.transfer": on_call_transfer,
}
