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
from modules.sales.calls import (
    on_call_answered,
    on_call_ended,
    on_call_transfer,
    on_incoming_call,
)
from modules.sales.events import (
    on_deal_created,
    on_incoming_message_ai,
    on_lead_converted,
    on_payment_paid,
    on_shipment_delivered,
)
from modules.sales.permissions import PERMISSIONS, ROLES
from modules.sales.reserve import tick_invoice_reserve
from modules.sales.workflows import DealApprovalWorkflow

logger = logging.getLogger("aios.sales")


class SalesModule(ModuleContract):
    name = "sales"
    version = "0.1.0"
    api_prefix = "/sales"

    def register(self, core: Core) -> None:
        core.include_router(routes.router, prefix=self.api_prefix)
        core.subscribe("sales.deal.created", on_deal_created)
        # AI-агент модуля как обработчик событий (Итерация 1, §2.5)
        core.subscribe("sales.message.sent", on_incoming_message_ai)
        # обратные межмодульные связи, замыкающие циклы (§2.5)
        core.subscribe("leads.lead.converted", on_lead_converted)  # лид → сделка (репо лидов)
        core.subscribe("finance.payment.paid", on_payment_paid)  # оплата → документ оплачен
        core.subscribe("logistics.shipment.delivered", on_shipment_delivered)  # доставка → won
        # телефония (SALES-50): события коннектора → журнал звонков + push карточки продавцу
        core.subscribe("telephony.call.incoming", on_incoming_call)
        core.subscribe("telephony.call.answered", on_call_answered)
        core.subscribe("telephony.call.ended", on_call_ended)
        core.subscribe("telephony.call.transfer", on_call_transfer)
        core.register_workflow(DealApprovalWorkflow.name, DealApprovalWorkflow)
        core.declare_permissions(PERMISSIONS)
        for role in ROLES:
            core.declare_role(role)
        for command in telegram.COMMANDS:
            core.register_telegram(command)
        core.register_widget(Widget("sales_pipeline", "Воронка продаж", source="sales.deals"))
        core.on_startup(self._on_startup)
        # SALES-51: периодический шаг — срок/напоминание/аннулирование резерва под счёт
        core.on_tick(tick_invoice_reserve)

    async def _on_startup(self) -> None:
        logger.info("Sales: модуль готов (каркас)")


def get_module() -> ModuleContract:
    """Фабрика модуля, вызываемая загрузчиком ядра."""
    return SalesModule()
