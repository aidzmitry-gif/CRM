"""Server-side deal visibility for CRM employees.

The setting is deliberately independent of a Keycloak role: a role grants a
capability, while ``deal_visibility`` decides whether that capability applies
to the whole CRM department or only the employee's own deals.
"""
from __future__ import annotations

from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config.access import is_super
from config.settings import get_settings
from core.domain.models import User
from core.runtime.deps import get_session
from core.services.auth import CurrentUser, get_current_user
from modules.sales.models import Deal

# ``Deal.owner_id`` stores ``hr.employee.id`` (via this soft link), never the
# primary key of app_user.  Only a current CRM employee may become a confirmed
# numeric owner; a legacy text owner remains display-only.
CRM_OWNER_ROLES = frozenset({"sales_head", "sales", "sales_manager", "sales_cli"})


@dataclass(frozen=True)
class DealAccess:
    """Resolved, request-scoped visibility. ``employee_id`` is required for own."""

    visibility: str
    employee_id: int | None = None

    @property
    def own_only(self) -> bool:
        return self.visibility == "own"


async def get_deal_access(
    request: Request,
    session: AsyncSession = Depends(get_session),
    user: CurrentUser = Depends(get_current_user),
) -> DealAccess:
    """Resolve a user's persistent scope without trusting client filters.

    A signed OIDC subject is the authoritative identity: never fall back to a
    renameable username for it. Header/test authentication without a subject
    may use username. Unlinked legacy directors remain supported only through
    the explicit super-role bypass above.
    """
    # Operational liveness is intentionally identity-independent. It exposes
    # no CRM data and must stay usable before the database is reachable.
    if request.url.path == "/sales/ping":
        return DealAccess("all")
    if is_super(user.roles):
        return DealAccess("all")

    identity_filter = (
        User.keycloak_user_id == user.keycloak_user_id
        if user.keycloak_user_id
        else User.username == user.username
    )
    app_user = (await session.execute(select(User).where(identity_filter))).scalar_one_or_none()
    if app_user is None:
        if not user.keycloak_user_id and get_settings().auth_mode == "dev":
            return DealAccess("all")
        raise HTTPException(status_code=403, detail="Учётная запись сотрудника не связана с CRM")
    if app_user.status != "active":
        raise HTTPException(status_code=403, detail="Учётная запись сотрудника не активна")

    visibility = getattr(app_user, "deal_visibility", "all")
    if visibility == "all":
        return DealAccess("all", app_user.employee_id)
    if visibility != "own" or app_user.employee_id is None:
        raise HTTPException(status_code=403, detail="Не настроен безопасный доступ к сделкам")
    return DealAccess("own", app_user.employee_id)


def scope_deals(stmt, access: DealAccess):
    """Append the server-side owner predicate for a list/aggregate query."""
    return stmt.where(Deal.owner_id == access.employee_id) if access.own_only else stmt


async def visible_deal_or_404(
    session: AsyncSession, deal_id: int, access: DealAccess
) -> Deal:
    stmt = select(Deal).where(Deal.id == deal_id)
    deal = (await session.execute(scope_deals(stmt, access))).scalar_one_or_none()
    if deal is None:
        # 404 deliberately does not reveal that another department member owns it.
        raise HTTPException(status_code=404, detail="Сделка не найдена")
    return deal


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
    assigned = (
        await session.execute(
            select(User).where(
                User.employee_id == assigned_id,
                User.status == "active",
                User.department == "Продажи",
                User.role.in_(CRM_OWNER_ROLES),
            )
        )
    ).scalar_one_or_none()
    if assigned is None:
        raise HTTPException(status_code=422, detail="Владелец сделки не является активным сотрудником CRM")
    return assigned.employee_id, assigned.full_name


# An own-scope request reaches only endpoints that explicitly call
# ``visible_deal_or_404`` or apply ``scope_deals``. New Sales endpoints are denied
# automatically until their author adds an object guard and opts them in here.
_OWN_ROUTE_NAMES = frozenset(
    {
        "board",
        "pipeline_analytics",
        "pipeline_stage_metrics",
        "list_deals",
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
        "list_tasks",
        "create_task",
        "list_deal_items",
        "repeat_last_order",
        "add_deal_item",
        "update_deal_item",
        "delete_deal_item",
        "list_contacts",
        "add_contact",
        "list_chats",
        "list_documents",
        "create_document",
        "list_messages",
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
