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


async def on_lead_converted(payload: dict, ctx) -> None:
    """Лид сконвертирован (модуль лидов) → создать сделку (leads → sales).

    Точка интеграции с репозиторием лидов через шину (§2.4/§2.5): модуль лидов не
    импортирует sales — он публикует ``leads.lead.converted``, а sales создаёт
    ``Deal`` (стадия ``new``, ответственный и приоритет из payload) и отвечает
    ``sales.deal.created`` с ``lead_id``/``deal_id``, по которому лид получает
    обратную ссылку на сделку.

    Позиции КП из ``payload.items`` переносятся в ``DealItem`` + сумма сделки —
    иначе конвертация теряет коммерческое предложение (денежный путь L4).
    """
    if ctx is None:
        return
    from decimal import Decimal, InvalidOperation

    lead_id = payload.get("lead_id")
    if not lead_id:
        return
    from modules.sales.models import Deal, DealItem

    deal = Deal(
        number=f"CRM-LEAD-{lead_id}",
        title=payload.get("title") or "Лид",
        counterparty=payload.get("counterparty") or "Новый лид",
        owner=payload.get("owner", ""),
        stage="new",
        priority=payload.get("priority", "Средний"),
    )
    ctx.session.add(deal)
    await ctx.session.flush()

    total = Decimal("0")
    for raw in payload.get("items") or []:
        try:
            sku_id = int(raw["sku_id"])
            qty = Decimal(str(raw.get("qty") or 1))
            price = Decimal(str(raw.get("price") or 0))
            disc = Decimal(str(raw.get("discount_pct") or 0))
        except (KeyError, TypeError, ValueError, InvalidOperation):
            continue
        if qty <= 0:
            continue
        ctx.session.add(DealItem(deal_id=deal.id, sku_id=sku_id, qty=qty))
        # линия = price×qty×(1−скидка%); деньги Decimal, не float
        line = (price * qty * (Decimal("1") - disc / Decimal("100"))).quantize(Decimal("0.01"))
        total += line
    deal.amount = total

    ctx.services.event_bus.emit(
        ctx.session,
        "sales.deal.created",
        {
            "number": deal.number,
            "title": deal.title,
            "lead_id": lead_id,
            "deal_id": deal.id,
            "entity_ref": f"deal:{deal.id}",
        },
    )
    logger.info("Sales: из лида %s создана сделка %s (%d поз.)", lead_id, deal.number, len(payload.get("items") or []))


async def on_payment_paid(payload: dict, ctx) -> None:
    """Платёж проведён → документ-счёт помечается оплаченным (finance → sales).

    Сначала по ``ref`` (= номер счёта). Если не нашли — по ``deal_id`` (шов 0105 /
    finance.payment.*), чтобы оплата не терялась при расхождении номеров.
    """
    if ctx is None:
        return
    from modules.sales.models import DealDocument

    doc = None
    ref = payload.get("ref")
    if ref:
        doc = (
            await ctx.session.execute(select(DealDocument).where(DealDocument.number == ref))
        ).scalars().first()
    if doc is None and payload.get("deal_id") is not None:
        try:
            deal_id = int(payload["deal_id"])
        except (TypeError, ValueError):
            deal_id = None
        if deal_id is not None:
            doc = (
                await ctx.session.execute(
                    select(DealDocument)
                    .where(DealDocument.deal_id == deal_id, DealDocument.kind == "invoice")
                    .order_by(DealDocument.id.desc())
                )
            ).scalars().first()
    if doc is not None:
        doc.status = "paid"
        if doc.reserve_status == "reserved":
            doc.reserve_status = "consumed"  # SALES-51: оплачен → резерв израсходован
        logger.info("Sales: документ %s помечен оплаченным", doc.number)


async def on_shipment_delivered(payload: dict, ctx) -> None:
    """Отгрузка доставлена → сделка закрывается успешно (logistics → sales).

    Закрытие в ``won`` идёт через тот же ``record_stage``, что и ручное закрытие
    в карточке — чтобы стадия, ``stage_changed_at``, история и дата не разъехались
    (ТЗ §9, SALES-40/43)."""
    if ctx is None:
        return
    deal_id = payload.get("deal_id")
    if not deal_id:
        return
    from datetime import date

    from modules.sales.models import Deal
    from modules.sales.repository import record_stage

    deal = await ctx.session.get(Deal, deal_id)
    if deal is not None and deal.stage != "won":
        record_stage(ctx.session, deal, "won", by="logistics")
        deal.closed_date = deal.closed_date or date.today().strftime("%d.%m.%Y")
        logger.info("Sales: сделка %s закрыта успешно (доставлено)", deal_id)


async def on_deal_won_handoff(payload: dict, ctx) -> None:
    """Сделка выиграна → собрать контракт ``sales.deal.handoff`` для downstream (логистика/
    финансы/офис). Полезная нагрузка: сделка, контрагент, сумма, позиции, ответственный +
    gross_profit (если landed cost подключён). Никакого write в чужие схемы — только событие.

    Идемпотентно: для одной и той же сделки эмитим один handoff (проверяем по audit-журналу
    ``OutboxEvent``-логирование произойдёт раз; повторные won-события ничего не делают).
    """
    if ctx is None:
        return
    deal_id = payload.get("deal_id")
    number = payload.get("number")
    if not deal_id:
        # Старые won-события без deal_id — игнор (закрытие из логистики приходит с deal_id).
        return

    # Идемпотентность: если уже эмитили handoff по этой сделке — выходим.
    from core.domain.models import OutboxEvent

    already = (
        await ctx.session.execute(
            select(OutboxEvent).where(OutboxEvent.event_type == "sales.deal.handoff")
        )
    ).scalars().all()
    if any(ev.payload.get("deal_id") == deal_id for ev in already):
        return

    from modules.sales.models import Deal, DealItem

    deal = await ctx.session.get(Deal, deal_id)
    if deal is None:
        return
    items_rows = (
        await ctx.session.execute(select(DealItem).where(DealItem.deal_id == deal_id))
    ).scalars().all()

    # Резолв сводки по позициям (sku_code+qty) — без вытаскивания всего Sku, через codes.
    from core.domain.models import Sku

    sku_ids = [r.sku_id for r in items_rows]
    sku_map: dict[int, Sku] = {}
    if sku_ids:
        sku_map = {
            s.id: s for s in (
                await ctx.session.execute(select(Sku).where(Sku.id.in_(sku_ids)))
            ).scalars().all()
        }
    items = [
        {
            "sku_code": sku_map[r.sku_id].code if r.sku_id in sku_map else "",
            "title": sku_map[r.sku_id].title if r.sku_id in sku_map else "",
            "qty": float(r.qty),
        }
        for r in items_rows
    ]

    # gross_profit — best-effort через landed_cost фасад (None → handoff без маржи).
    gross_profit: float | None = None
    facade = getattr(ctx.services, "landed_cost", None)
    if facade is not None and items:
        # Бюджет: тянем landed по уникальным кодам, цену клиенту из PriceQuote.
        codes = sorted({it["sku_code"] for it in items if it["sku_code"]})
        landed = await facade.last_landed_cost_batch(ctx.session, codes)
        from modules.sales.models import PriceQuote

        quotes = (
            await ctx.session.execute(
                select(PriceQuote)
                .where(PriceQuote.counterparty == deal.counterparty, PriceQuote.sku_code.in_(codes))
                .order_by(PriceQuote.id)
            )
        ).scalars().all()
        last_price: dict[str, float] = {}
        for q in quotes:
            last_price[q.sku_code] = float(q.price)
        gp = 0.0
        any_priced = False
        for it in items:
            code = it["sku_code"]
            price = last_price.get(code)
            cost_row = landed.get(code)
            if price is not None and cost_row is not None:
                any_priced = True
                gp += (price - float(cost_row["unit_landed_cost_byn"])) * it["qty"]
        gross_profit = round(gp, 2) if any_priced else None

    ctx.services.event_bus.emit(
        ctx.session,
        "sales.deal.handoff",
        {
            "deal_id": deal_id,
            "number": number or deal.number,
            "counterparty": deal.counterparty,
            "amount": float(deal.amount),
            "owner": deal.owner,
            "funnel": deal.funnel,
            "items": items,
            "gross_profit": gross_profit,
            "actor": "sales",
            "entity_ref": f"deal:{deal_id}",
        },
    )
    logger.info("Sales: handoff по сделке #%s (контрагент=%s, позиций=%d)", deal_id, deal.counterparty, len(items))


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


async def on_procurement_received(payload: dict, ctx) -> None:
    """Поставка пришла (``procurement.received``) → сигнал продавцу на активных сделках с этим SKU.

    Находит активные (нетерминальные) сделки, у которых в позициях есть пришедший SKU, и
    эмитит ``sales.supply.arrived {deal_ids, sku_code, qty}`` (инфо, в audit). Стадию НЕ меняет.
    SKU берём из ``sku_code`` или ``item``; нет SKU / нет активных сделок — тихий ранний выход
    (S3-3, close-deferred: продюсер может появиться позже — подписка не падает на чужом payload).
    """
    if ctx is None:
        return
    sku_code = payload.get("sku_code") or payload.get("item")
    if not sku_code:
        return

    from core.domain.models import Sku
    from modules.sales.models import Deal, DealItem
    from modules.sales.stages import TERMINAL_STAGES

    sku = (
        await ctx.session.execute(select(Sku).where(Sku.code == sku_code))
    ).scalars().first()
    if sku is None:
        return
    deal_ids = (
        await ctx.session.execute(select(DealItem.deal_id).where(DealItem.sku_id == sku.id))
    ).scalars().all()
    if not deal_ids:
        return
    deals = (
        await ctx.session.execute(
            select(Deal).where(Deal.id.in_(set(deal_ids)), Deal.stage.notin_(TERMINAL_STAGES))
        )
    ).scalars().all()
    active_ids = [d.id for d in deals]
    if not active_ids:
        return
    ctx.services.event_bus.emit(
        ctx.session,
        "sales.supply.arrived",
        {
            "deal_ids": active_ids,
            "sku_code": sku_code,
            "qty": payload.get("qty"),
            "warehouse": payload.get("warehouse"),
            "actor": "sales",
            "entity_ref": payload.get("entity_ref") or f"sku:{sku_code}",
        },
    )
    logger.info(
        "Sales: поставка SKU %s → сигнал на %d активн. сделок", sku_code, len(active_ids)
    )


async def on_plan_approved(payload: dict, ctx) -> None:
    """Согласованный план РОП (``sales.plan.approved``) → мягкий upsert цели скорборда (S3-5).

    Для метрики, совпадающей с ключом скорборда (``KpiTarget.key == metric``), пишем
    ``target`` из плана; метрика вне скорборда — игнор (строк не плодим). Идемпотентно
    (повторная установка того же значения — no-op). Так ``/sales/kpis`` берёт цель из
    согласованных чисел, а не из сида.
    """
    if ctx is None:
        return
    metric = payload.get("metric")
    target = payload.get("target")
    if not metric or target is None:
        return

    from decimal import Decimal, InvalidOperation

    from modules.sales.models import KpiTarget
    from modules.sales.routes import PERIOD_MULT

    # Кривой target (нечисловая строка, nan/inf) НЕ должен валить relay → poison-pill всей шины
    # (relay не изолирует хендлеры try/except; падение не выставит processed_at → вечный реплей).
    try:
        value = Decimal(str(target))
    except (InvalidOperation, ValueError):
        return
    if not value.is_finite():
        return

    kpi = (
        await ctx.session.execute(select(KpiTarget).where(KpiTarget.key == metric))
    ).scalars().first()
    if kpi is None:
        return  # метрика вне скорборда — не создаём строк
    # KpiTarget.target — ДНЕВНОЙ seed: /kpis домножает на рабочие дни периода (PERIOD_MULT).
    # План — ИТОГ за свой период, поэтому нормализуем в дневной (иначе план месяца раздуется ×22).
    mult = PERIOD_MULT.get(payload.get("period_type", "day"), 1)
    kpi.target = (value / Decimal(mult)).quantize(Decimal("0.01"))
    logger.info(
        "Sales: KpiTarget '%s' ← план РОП %s/%s = %s",
        metric, payload.get("period_type"), payload.get("period_key"), target,
    )
