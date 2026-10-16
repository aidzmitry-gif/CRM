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

from fastapi import HTTPException
from sqlalchemy import select

from core.domain.models import Contact, Counterparty, SyncLink
from core.services.crm_owners import resolve_crm_owner
from modules.hr.models import Employee
from modules.integrations.telephony import normalize_e164
from modules.sales.models import CallLog, CrmClient, CrmClientContact, Deal
from modules.sales.stages import TERMINAL_STAGES

logger = logging.getLogger("aios.sales.calls")


def _utcnow() -> datetime:
    # наивный UTC — единообразно для SQLite и PostgreSQL (как в routes.py)
    return datetime.now(timezone.utc).replace(tzinfo=None)


# --- SSE-реестр подписок «owner → очереди карточек» (in-process) ------------------
# ponytail: in-process реестр (один воркер). При масштабировании — Redis pub/sub
# (заложено в ТЗ A.5), сейчас не реализуем.
_subscribers: dict[int, set[asyncio.Queue]] = defaultdict(set)


def subscribe(employee_id: int) -> asyncio.Queue:
    """Подписать сотрудника на поток карточек звонков; вернуть его очередь."""
    queue: asyncio.Queue = asyncio.Queue(maxsize=100)
    _subscribers[employee_id].add(queue)
    return queue


def unsubscribe(employee_id: int, queue: asyncio.Queue) -> None:
    subs = _subscribers.get(employee_id)
    if subs is not None:
        subs.discard(queue)
        if not subs:
            _subscribers.pop(employee_id, None)


def has_subscriber(employee_id: int) -> bool:
    return bool(_subscribers.get(employee_id))


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


def _publish_card(card: dict, employee_id: int | None) -> bool:
    """Publish a committed snapshot without retaining a mutable ORM object."""
    if employee_id is None:
        return False  # новый номер / дежурный пул без подписки — карточка не пушится
    delivered = False
    for queue in list(_subscribers.get(employee_id, ())):
        try:
            queue.put_nowait(card)
            delivered = True
        except asyncio.QueueFull:
            logger.warning("calls: очередь подписки переполнена (employee_id=%s)", employee_id)
    return delivered


def _push_card(call: CallLog, ctx=None) -> bool:
    """Relay publishes after commit; the direct fallback keeps immediate push."""
    card = _card(call)
    employee_id = call.owner_id
    if ctx is None:
        return _publish_card(card, employee_id)
    return bool(ctx.after_commit(lambda: _publish_card(card, employee_id)))


# --- Резолв ответственного по номеру ----------------------------------------------
async def resolve_owner(session, phone_e164: str | None) -> dict:
    """Resolve a caller through an exact CRM contact and stable CRM IDs.

    CRM contact/client IDs are authoritative. A legacy shared contact is used only
    when its full normalized number uniquely identifies the CRM client through its
    stable ``counterparty_id``. Ambiguous identities stay unassigned.
    """
    result: dict = {
        "owner": "",
        "owner_id": None,
        "counterparty_id": None,
        "contact_id": None,
        "deal_id": None,
    }
    number = normalize_e164(phone_e164)
    if not number:
        return result

    async def confirmed_owner(employee_id: int | None):
        if employee_id is None:
            return None
        try:
            return await resolve_crm_owner(session, employee_id=employee_id)
        except HTTPException:
            return None

    async def route_client(client: CrmClient, matched_contact_ids: set[int], legacy_contact: Contact | None = None) -> None:
        if client.counterparty_id is not None:
            party = await session.get(Counterparty, client.counterparty_id)
            if party is None or not party.is_active or party.merged_into_id is not None:
                return
        deals = (await session.execute(
            select(Deal).where(
                Deal.crm_client_id == client.id,
                Deal.stage.notin_(TERMINAL_STAGES),
            )
        )).scalars().all()
        if legacy_contact is not None:
            deals = [deal for deal in deals if deal.branch_id == legacy_contact.branch_id]
        contact_bound_deals = [deal for deal in deals if deal.crm_contact_id is not None]
        matching_contact_deals = [
            deal for deal in contact_bound_deals
            if deal.crm_contact_id in matched_contact_ids
        ]
        if matching_contact_deals:
            deals = matching_contact_deals
        elif contact_bound_deals:
            # Don't let an unbound deal mask an explicit link to another contact.
            deals = []

        deal_owner = None
        deal_id = None
        if len(deals) == 1:
            deal_owner = await confirmed_owner(deals[0].owner_id)
            if deal_owner is not None:
                deal_id = deals[0].id
        elif len(deals) > 1:
            owner_ids = {deal.owner_id for deal in deals}
            if len(owner_ids) == 1:
                deal_owner = await confirmed_owner(next(iter(owner_ids)))

        owner = deal_owner or await confirmed_owner(client.owner_id)
        if owner is not None:
            result["owner"] = owner.full_name
            result["owner_id"] = owner.employee_id
            result["deal_id"] = deal_id

    # ponytail: these exact normalized lookups scan contact rows; add an indexed
    # E.164 key when a measured lookup exceeds 50 ms or contact rows exceed 10,000.
    crm_contacts = (await session.execute(
        select(CrmClientContact).where(CrmClientContact.phone.isnot(None))
    )).scalars().all()
    matching_crm_contacts = [
        contact for contact in crm_contacts if normalize_e164(contact.phone) == number
    ]
    crm_client_ids = {contact.crm_client_id for contact in matching_crm_contacts}
    if len(crm_client_ids) > 1:
        return result
    if crm_client_ids:
        client = await session.get(CrmClient, next(iter(crm_client_ids)))
        if client is not None:
            result["counterparty_id"] = client.counterparty_id
            await route_client(client, {contact.id for contact in matching_crm_contacts})
        return result

    legacy_contacts = (await session.execute(
        select(Contact).where(Contact.phone.isnot(None))
    )).scalars().all()
    exact_legacy = [contact for contact in legacy_contacts if normalize_e164(contact.phone) == number]
    if len(exact_legacy) != 1:
        return result

    contact = exact_legacy[0]
    result["contact_id"] = contact.id
    result["counterparty_id"] = contact.counterparty_id
    if contact.counterparty_id is None:
        return result

    clients = (await session.execute(
        select(CrmClient).where(CrmClient.counterparty_id == contact.counterparty_id)
    )).scalars().all()
    if len(clients) == 1:
        await route_client(clients[0], set(), contact)

    return result


# --- Апсерт записи звонка по событию ----------------------------------------------
def _extension(value: object) -> str | None:
    """Validate legacy event values without guessing or changing the audit payload."""
    return value if isinstance(value, str) and re.fullmatch(r"[0-9]{1,8}", value) else None


async def resolve_agent(session, extension: str | None) -> tuple[str, int | None] | None:
    """An explicitly confirmed external employee identity; never infer from a number.

    Staff without a CRM account can be named in the call log. Numeric ownership
    (and thus own-scope access) still requires an eligible active CRM account.
    Duplicate links fail closed, including duplicates pointing at the same person.
    """
    if _extension(extension) is None:
        return None
    rows = (await session.execute(
        select(Employee).join(SyncLink, SyncLink.entity_id == Employee.id).where(
            SyncLink.entity_type == "employee", SyncLink.system == "telephony",
            SyncLink.external_ref == extension, SyncLink.origin == "telephony",
            SyncLink.direction == "in", SyncLink.state == "synced",
        )
    )).scalars().all()
    if len(rows) != 1 or rows[0].status != "active" or not rows[0].full_name.strip():
        return None
    employee = rows[0]
    try:
        owner = await resolve_crm_owner(session, employee_id=employee.id)
    except HTTPException:
        return employee.full_name, None
    return owner.full_name, owner.employee_id


async def record_event(session, payload: dict, event_type: str) -> tuple[CallLog | None, bool]:
    """Применить событие телефонии к ``CallLog`` (апсерт по ``call_id``).

    Возвращает ``(call, created)``. ``created`` — была ли запись создана этим событием
    (используется, чтобы резолвить owner и эмитить ``sales.call.logged`` один раз).
    """
    call_id = (payload.get("call_id") or "").strip()
    if not call_id:
        return None, False
    agent_ext = _extension(payload.get("agent_ext"))
    to_ext = _extension(payload.get("to_ext"))

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
            agent_ext=agent_ext,
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
    if agent_ext and not call.agent_ext:
        call.agent_ext = agent_ext
    agent = await resolve_agent(session, call.agent_ext)
    if agent is not None:
        call.owner, call.owner_id = agent
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
    elif event_type == "telephony.call.transfer" and to_ext:
        note = f"Перевод на {to_ext}"
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
            "agent_ext": call.agent_ext,  # validated extension; employee mapping is separate
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
    _push_card(call, ctx)


async def on_call_answered(payload: dict, ctx) -> None:
    if ctx is None:
        return
    call, created = await record_event(ctx.session, payload, "telephony.call.answered")
    if call is None:
        return
    if created:
        _emit_logged(ctx, call)
    _push_card(call, ctx)


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
    _push_card(call, ctx)


async def on_call_transfer(payload: dict, ctx) -> None:
    if ctx is None:
        return
    call, created = await record_event(ctx.session, payload, "telephony.call.transfer")
    if call is None:
        return
    if created:
        _emit_logged(ctx, call)
    _push_card(call, ctx)


# Карта обработчиков по типу события — для синхронного приёма через эндпоинт-fallback
# (``POST /sales/telephony/incoming``) и для подписки в module.register().
EVENT_HANDLERS = {
    "telephony.call.incoming": on_incoming_call,
    "telephony.call.answered": on_call_answered,
    "telephony.call.ended": on_call_ended,
    "telephony.call.transfer": on_call_transfer,
}
