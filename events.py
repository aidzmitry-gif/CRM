"""Обработчики событий модуля Sales."""
from __future__ import annotations

import logging

from sqlalchemy import select

logger = logging.getLogger("aios.sales")


async def on_deal_created(payload: dict) -> None:
    """Реакция на создание сделки. Пока только логирует (демонстрация шины событий)."""
    logger.info(
        "Sales: создана сделка %s — %s", payload.get("number"), payload.get("title")
    )


async def on_campaign_launched(payload: dict, ctx) -> None:
    """Кампания запущена → привлечённые лиды попадают в приём лидов CRM (marketing → sales).

    Маркетинг питает воронку через её вход: создаёт записи лидов (front-of-funnel),
    которые менеджер/AI затем квалифицирует, распределяет и превращает в сделки —
    а не создаёт сделки напрямую. Так замыкается цикл «кампания → лиды → воронка».
    """
    if ctx is None:
        return
    from modules.sales.leads import LEAD_SOURCES
    from modules.sales.models import Lead

    count = min(int(payload.get("leads", 0) or 0), 10)
    name = payload.get("name", "Кампания")
    channel = payload.get("channel", "site")
    source = channel if channel in LEAD_SOURCES else "site"
    for _ in range(count):
        ctx.session.add(
            Lead(
                source=source,
                message=f"Заявка из кампании «{name}» (канал {channel})",
                status="new",
            )
        )
    logger.info("Sales: из кампании «%s» принято лидов: %d", name, count)


async def on_payment_paid(payload: dict, ctx) -> None:
    """Платёж проведён → документ-счёт помечается оплаченным (finance → sales)."""
    if ctx is None:
        return
    from modules.sales.models import DealDocument

    ref = payload.get("ref")
    if not ref:
        return
    doc = (
        await ctx.session.execute(select(DealDocument).where(DealDocument.number == ref))
    ).scalars().first()
    if doc is not None:
        doc.status = "paid"
        logger.info("Sales: документ %s помечен оплаченным", ref)


async def on_shipment_delivered(payload: dict, ctx) -> None:
    """Отгрузка доставлена → сделка закрывается успешно (logistics → sales)."""
    if ctx is None:
        return
    deal_id = payload.get("deal_id")
    if not deal_id:
        return
    from modules.sales.models import Deal

    deal = await ctx.session.get(Deal, deal_id)
    if deal is not None:
        deal.stage = "won"
        logger.info("Sales: сделка %s закрыта успешно (доставлено)", deal_id)


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
