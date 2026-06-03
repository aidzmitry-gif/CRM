"""AI-подмодуль Sales (Итерация 1) — за feature-flag, через шлюз ядра.

Первый AI-агент: черновик ответа клиенту по истории переписки. Обращается к
общему LLM-шлюзу (``core.services.llm``), не к модели напрямую; вызов
сопровождается доменным событием (трассировка AI-действия в audit, §3.3).
Позже агент станет обработчиком событий и Temporal-activity внутри модуля —
без переписывания (§2.5).
"""
from __future__ import annotations

from modules.sales.models import Deal, Message


async def draft_reply(gateway, deal: Deal, messages: list[Message]) -> str:
    """Составить черновик ответа клиенту по последней входящей реплике."""
    last_in = next((m for m in reversed(messages) if m.direction == "in"), None)
    context = last_in.text if last_in else "клиент ожидает ответа по сделке"
    system = "Ты — менеджер по продажам. Составь вежливый краткий ответ клиенту на русском."
    prompt = (
        f"Сделка {deal.number}, контрагент {deal.counterparty}. "
        f"Последнее сообщение клиента: «{context}». Составь ответ менеджера."
    )
    return await gateway.complete(prompt, system=system)
