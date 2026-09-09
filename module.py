"""Модуль Sales (CRM) — реализация ModuleContract.

Заглушка-каркас: регистрирует API-роутер, обработчик события, пустой workflow,
права RBAC, Telegram-команды и виджет панели владельца — чтобы на живом примере
увидеть, как модуль подключается к ядру. Наполнение — части 7–11 дорожной карты.
"""
from __future__ import annotations

import logging

from core.runtime.contract import ModuleContract, Widget
from core.runtime.core import Core
from modules.sales import mail_routes, routes, telegram
from modules.sales.calls import (
    on_call_answered,
    on_call_ended,
    on_call_transfer,
    on_incoming_call,
)
from modules.sales.events import (
    on_deal_created,
    on_deal_won_handoff,
    on_incoming_message_ai,
    on_lead_converted,
    on_payment_paid,
    on_plan_approved,
    on_procurement_received,
    on_shipment_delivered,
)
from modules.sales.mail_queue import EmailWorker
from modules.sales.permissions import PERMISSIONS, ROLES
from modules.sales.reserve import tick_invoice_reserve
from modules.sales.touch_history import SalesTouchHistory
from modules.sales.workflows import DealApprovalWorkflow

logger = logging.getLogger("aios.sales")


class SalesModule(ModuleContract):
    name = "sales"
    version = "0.1.0"
    api_prefix = "/sales"

    def register(self, core: Core) -> None:
        core.include_router(routes.router, prefix=self.api_prefix)
        core.include_router(mail_routes.router, prefix=self.api_prefix)
        email_worker = EmailWorker(core.services)
        core.on_startup(email_worker.start)
        core.on_shutdown(email_worker.stop)
        # Лиды: фронт бьёт в /leads (был 404 — отдавались на /sales/leads). Монтируем тот же
        # роутер на /leads (фронт) и /sales/leads (back-compat). Полный вынос — Шаг 2 ТЗ.
        core.include_router(routes.leads_router, prefix="/leads")
        core.include_router(routes.leads_router, prefix="/sales/leads")
        core.subscribe("sales.deal.created", on_deal_created)
        # П10 ТЗ: won → handoff downstream (контракт для логистики/финансов/офиса).
        core.subscribe("sales.deal.won", on_deal_won_handoff)
        # AI-агент модуля как обработчик событий (Итерация 1, §2.5)
        core.subscribe("sales.message.sent", on_incoming_message_ai)
        # обратные межмодульные связи, замыкающие циклы (§2.5)
        core.subscribe("leads.lead.converted", on_lead_converted)  # лид → сделка (репо лидов)
        core.subscribe("finance.payment.paid", on_payment_paid)  # оплата → документ оплачен
        core.subscribe("logistics.shipment.delivered", on_shipment_delivered)  # доставка → won
        # S3-3: поставка пришла → сигнал продавцу на сделках с этим SKU (sales.supply.arrived)
        core.subscribe("procurement.received", on_procurement_received)
        # S3-5: согласованный план РОП → цель скорборда (KpiTarget), не из сида
        core.subscribe("sales.plan.approved", on_plan_approved)
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
        # M5: наполняем фасад истории касаний для 360°-карточки контрагента в ядре
        # (звонки/сообщения/сделки). Без этого core.services.touch_history=None → карточка
        # без истории (graceful). Реализация не лезет в схему ядра — только читает свою.
        core.services.touch_history = SalesTouchHistory()
        core.on_startup(self._on_startup)
        # SALES-51: периодический шаг — срок/напоминание/аннулирование резерва под счёт
        core.on_tick(tick_invoice_reserve)

    async def _on_startup(self) -> None:
        logger.info("Sales: модуль готов (каркас)")


def get_module() -> ModuleContract:
    """Фабрика модуля, вызываемая загрузчиком ядра."""
    return SalesModule()
