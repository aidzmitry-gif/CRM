"""Обработчики событий модуля Sales."""
from __future__ import annotations

import logging

logger = logging.getLogger("aios.sales")


async def on_deal_created(payload: dict) -> None:
    """Реакция на создание сделки. Пока только логирует (демонстрация шины событий)."""
    logger.info(
        "Sales: создана сделка %s — %s", payload.get("number"), payload.get("title")
    )


async def on_incoming_message_ai(payload: dict, ctx) -> None:
    """AI-агент реагирует на входящее сообщение клиента (§2.5, Итерация 1).

    Обработчик событий с контекстом: при включённом AI генерирует черновик ответа
    через общий шлюз и публикует его событием ``ai.draft.suggested`` (→ audit).
    Так AI работает реактивно (на событие), а не только по кнопке. Выполняется
    в фоне relay; при выключенном AI или входящем не от клиента — ничего не делает.
    """
    if payload.get("direction") != "in" or ctx is None:
        return
    llm = getattr(ctx.services, "llm", None)
    if llm is None or not getattr(llm, "enabled", False):
        return
    deal_id = payload.get("deal_id")
    text = await llm.complete(
        f"Клиент написал по сделке (deal {deal_id}). Составь короткий черновик ответа.",
        system="Ты — менеджер по продажам. Краткий вежливый ответ на русском.",
        kind="draft",
    )
    ctx.services.event_bus.emit(
        ctx.session,
        "ai.draft.suggested",
        {"deal_id": deal_id, "text": text, "actor": "AI", "entity_ref": f"deal:{deal_id}"},
    )
    logger.info("Sales AI: предложен черновик ответа по сделке %s", deal_id)
