"""Модуль Sales (CRM) — реализация ModuleContract.

Заглушка-каркас: регистрирует API-роутер, обработчик события, пустой workflow,
права RBAC, Telegram-команды и виджет панели владельца — чтобы на живом примере
увидеть, как модуль подключается к ядру. Наполнение — части 7–11 дорожной карты.
"""
from __future__ import annotations

import logging

from core.runtime.contract import ModuleContract, Widget
from core.runtime.core import Core
from modules.sales import routes, telegram
from modules.sales.events import on_deal_created
from modules.sales.permissions import PERMISSIONS, ROLES
from modules.sales.workflows import DealApprovalWorkflow

logger = logging.getLogger("aios.sales")


class SalesModule(ModuleContract):
    name = "sales"
    version = "0.1.0"
    api_prefix = "/sales"

    def register(self, core: Core) -> None:
        core.include_router(routes.router, prefix=self.api_prefix)
        core.subscribe("sales.deal.created", on_deal_created)
        core.register_workflow(DealApprovalWorkflow.name, DealApprovalWorkflow)
        core.declare_permissions(PERMISSIONS)
        for role in ROLES:
            core.declare_role(role)
        for command in telegram.COMMANDS:
            core.register_telegram(command)
        core.register_widget(Widget("sales_pipeline", "Воронка продаж", source="sales.deals"))
        core.on_startup(self._on_startup)

    async def _on_startup(self) -> None:
        logger.info("Sales: модуль готов (каркас)")


def get_module() -> ModuleContract:
    """Фабрика модуля, вызываемая загрузчиком ядра."""
    return SalesModule()
