"""Server-side deal visibility for CRM employees.

The setting is deliberately independent of a Keycloak role: a role grants a
capability, while ``deal_visibility`` decides whether that capability applies
to the whole CRM department or only the employee's own deals.
"""
from __future__ import annotations

from fastapi import Depends, HTTPException, Request
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.domain.models import Counterparty
from core.services.crm_access import CrmAccess as DealAccess
from core.services.crm_access import get_crm_access as get_deal_access
from core.services.crm_access import (
    get_crm_access_for_user as get_deal_access_for_user,  # noqa: F401 - command API
)
from core.services.crm_owners import resolve_crm_owner
from modules.sales.models import CallLog, Deal, DealTask, DealTaskCrmLink


def scope_deals(stmt, access: DealAccess):
    """Append the server-side owner predicate for a list/aggregate query."""
    return stmt.where(Deal.owner_id == access.employee_id) if access.own_only else stmt


def unique_counterparty_names():
    """Keep the same fail-closed name resolution as deal contact endpoints."""
    return select(Counterparty.name).group_by(Counterparty.name).having(
        func.count(Counterparty.id) == 1,
    ).correlate(None)


def scope_counterparties(stmt, access: DealAccess):
    if not access.own_only:
        return stmt
    return stmt.where(
        Counterparty.name.in_(scope_deals(select(Deal.counterparty).where(Deal.crm_client_id.is_(None)), access)),
        Counterparty.name.in_(unique_counterparty_names()),
    )


def scope_calls(stmt, access: DealAccess):
    """Linked calls follow deal scope; unlinked calls require a confirmed owner."""
    if not access.own_only:
        return stmt
    own_deals = scope_deals(select(Deal.id), access)
    return stmt.where(or_(
        CallLog.deal_id.in_(own_deals),
        and_(CallLog.deal_id.is_(None), CallLog.owner_id == access.employee_id),
    ))


def scope_tasks(stmt, access: DealAccess):
    """Only own deals or truly unbound self-assigned tasks are visible."""
    if not access.own_only:
        return stmt
    own_deals = scope_deals(select(Deal.id), access)
    any_link = select(DealTaskCrmLink.task_id)
    own_link = select(DealTaskCrmLink.task_id).where(
        DealTaskCrmLink.deal_id.in_(own_deals)
    )
    return stmt.where(or_(
        DealTask.deal_id.in_(own_deals),
        DealTask.id.in_(own_link),
        and_(
            DealTask.deal_id.is_(None),
            DealTask.assignee_id == access.employee_id,
            ~DealTask.id.in_(any_link),
        ),
    ))


async def visible_call_or_404(session: AsyncSession, call_id: int, access: DealAccess) -> CallLog:
    call = (await session.execute(scope_calls(select(CallLog).where(CallLog.id == call_id), access))).scalar_one_or_none()
    if call is None:
        raise HTTPException(status_code=404, detail="Звонок не найден")
    return call


async def locked_call_for_write(
    session: AsyncSession, call_id: int, access: DealAccess, target_deal_id: int | None = None,
) -> CallLog:
    """Keep call assignment and both deal owners stable until the caller commits."""
    call = (await session.execute(
        select(CallLog).where(CallLog.id == call_id).with_for_update()
        .execution_options(populate_existing=True)
    )).scalar_one_or_none()
    if call is None:
        raise HTTPException(status_code=404, detail="Звонок не найден")
    deal_ids = {value for value in (call.deal_id, target_deal_id) if value is not None}
    if deal_ids:
        await session.execute(
            select(Deal).where(Deal.id.in_(deal_ids)).order_by(Deal.id).with_for_update()
            .execution_options(populate_existing=True)
        )
    if call.deal_id is not None:
        await visible_deal_or_404(session, call.deal_id, access)
    elif access.own_only and call.owner_id != access.employee_id:
        raise HTTPException(status_code=404, detail="Звонок не найден")
    if target_deal_id is not None:
        await visible_deal_or_404(session, target_deal_id, access)
    return call


async def visible_deal_or_404(
    session: AsyncSession, deal_id: int, access: DealAccess, *, lock: bool = False
) -> Deal:
    stmt = select(Deal).where(Deal.id == deal_id)
    if lock:
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    deal = (await session.execute(scope_deals(stmt, access))).scalar_one_or_none()
    if deal is None:
        # 404 deliberately does not reveal that another department member owns it.
        raise HTTPException(status_code=404, detail="Сделка не найдена")
    return deal


async def visible_deal_or_none(
    session: AsyncSession, deal_id: int, access: DealAccess
) -> Deal | None:
    """Return a visible deal without changing legacy empty-list contracts.

    Read-only collection endpoints historically return an empty list when the
    requested deal does not exist. Callers may use this helper for that
    compatibility case; an ``own`` caller still receives no foreign row.
    Mutating and direct-object endpoints must keep using ``visible_deal_or_404``.
    """

    stmt = select(Deal).where(Deal.id == deal_id)
    return (await session.execute(scope_deals(stmt, access))).scalar_one_or_none()


def require_owner_assignment(access: DealAccess, owner_id: int | None) -> int | None:
    """Prevent an own-scope employee from assigning a deal to another person."""
    if access.own_only:
        if owner_id not in (None, access.employee_id):
            raise HTTPException(status_code=403, detail="Нельзя назначить чужого владельца сделки")
        return access.employee_id
    return owner_id


async def resolve_owner_assignment(
    session: AsyncSession, access: DealAccess, owner_id: int | None
) -> tuple[int | None, str | None]:
    """Return a real active CRM employee identity for a new assignment.

    ``owner`` remains a legacy display field only.  It is derived from the
    authoritative local account and never used for visibility predicates.
    """
    assigned_id = require_owner_assignment(access, owner_id)
    if assigned_id is None:
        return None, None
    assigned = await resolve_crm_owner(session, employee_id=assigned_id)
    return assigned.employee_id, assigned.full_name


# An own-scope request reaches only endpoints that explicitly call
# ``visible_deal_or_404`` or apply ``scope_deals``. New Sales endpoints are denied
# automatically until their author adds an object guard and opts them in here.
_OWN_ROUTE_NAMES = frozenset(
    {
        "board",
        "shipping_association_preview", "shipping_association_confirm", "shipping_envelope_prepare",
        "pipeline_analytics",
        "pipeline_stage_metrics",
        "list_deals",
        "list_funnels",
        "get_deal",
        "update_deal",
        "create_deal",
        "list_skus",
        "price_info",
        "deal_margin",
        "deal_margin_reconcile",
        "pipeline_margin_forecast",
        "lose_deal",
        "win_deal",
        "deal_history",
        "deal_timeline_comments",
        "list_deal_timeline_comment_attachments",
        "download_deal_timeline_comment_attachment",
        "list_tasks",
        "list_all_tasks",
        "create_task",
        "update_task",
        "list_deal_items",
        "repeat_last_order",
        "add_deal_item",
        "update_deal_item",
        "delete_deal_item",
        "list_contacts",
        "list_crm_clients",
        "create_crm_client", "get_crm_client", "crm_client_owners",
        "get_counterparty_responsible",
        "list_crm_client_contacts", "add_crm_client_contact", "primary_crm_client_contact", "list_crm_client_deals", "list_crm_client_documents",
        "list_crm_contacts",
        "add_contact",
        "set_primary_contact",
        "list_chats",
        "list_documents",
        "create_document",
        "preview_invoice",
        "prepare_contract", "revise_document", "update_document_draft", "issue_document",
        "decide_document", "render_document", "document_snapshot", "render_package",
        "render_saved_package", "send_package", "preview_document",
        "list_messages",
        "list_calls", "get_call", "call_comment", "call_result", "call_link_deal",
        "calls_stream",
        "create_message",
        "mark_messages_read",
        "ai_draft_reply",
        "ai_assist",
    }
)


async def deny_unscoped_own_routes(
    request: Request, access: DealAccess = Depends(get_deal_access)
) -> None:
    if not access.own_only:
        return
    route = request.scope.get("route")
    if getattr(route, "name", "") not in _OWN_ROUTE_NAMES:
        raise HTTPException(status_code=403, detail="Этот раздел недоступен при личной видимости сделок")
