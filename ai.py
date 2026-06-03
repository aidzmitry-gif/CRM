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
    return await gateway.complete(prompt, system=system, kind="draft")


async def summarize(gateway, deal: Deal, context: str) -> str:
    """Краткое резюме сделки и риски для РОПа/владельца."""
    system = "Ты — ассистент РОПа. Дай краткое резюме сделки и риски на русском."
    prompt = (
        f"Сделка {deal.number} ({deal.counterparty}), стадия {deal.stage}, "
        f"сумма {deal.amount}. {context} Сделай резюме и отметь риски."
    )
    return await gateway.complete(prompt, system=system, kind="summary")


async def next_step(gateway, deal: Deal, context: str) -> str:
    """Рекомендация следующего шага по сделке."""
    system = "Ты — ассистент менеджера по продажам. Предложи следующий шаг на русском."
    prompt = (
        f"Сделка {deal.number} ({deal.counterparty}), стадия {deal.stage}. "
        f"{context} Предложи конкретный следующий шаг."
    )
    return await gateway.complete(prompt, system=system, kind="next_step")
