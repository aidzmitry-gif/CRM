"""Факт KPI из операционных таблиц (не только Activity).

Доска сделок читает GET /sales/kpis; для импортированных данных Bitrix/1С
звонки лежат в ``CallLog``, выручка — в ``finance.Payment``, сделки — в ``Deal``.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from modules.finance.models import Payment
from modules.sales.models import CallLog, Deal

# Ключи, для которых операционный факт важнее ручных Activity.
OPERATIONAL_KPI_KEYS = frozenset(
    {
        "ship_plan",
        "payments_vat",
        "calls_all",
        "calls_cold",
        "won_count",
        "won_sum",
        "new_deals_count",
        "avg_deal",
    }
)

# Метрики первичного ряда доски, которых может не быть в kpi_target (seed).
BOARD_EXTRA_TARGETS: dict[str, tuple[str, str, str, str, float]] = {
    # key: (title, unit, icon, tone, daily_target)
    "payments_vat": ("Оплаты с НДС", "money", "ruble", "green", 8_000_000.0),
    "gross_profit": ("Прибыль валовая", "money", "ruble", "green", 1_760_000.0),
    "new_deals_count": ("Новые сделки", "count", "doc", "blue", 4.0),
    "invoice_payment_conv": ("Конв. счёт→оплата", "count", "doc", "slate", 80.0),
    "avg_deal": ("Средний чек", "money", "ruble", "green", 500_000.0),
}

WON_STAGES = ("won", "rp_won")


def _bounds(start: date, end: date) -> tuple[datetime, datetime]:
    """[start_dt, end_excl) для datetime-полей."""
    start_dt = datetime.combine(start, datetime.min.time())
    end_excl = datetime.combine(end, datetime.min.time()) + timedelta(days=1)
    return start_dt, end_excl


async def compute_operational_kpi_facts(
    session: AsyncSession, start: date, end: date
) -> dict[str, float]:
    """Собрать факт по звонкам, выручке (1С) и сделкам за календарное окно."""
    start_dt, end_excl = _bounds(start, end)
    facts: dict[str, float] = {}

    revenue = (
        await session.execute(
            select(func.coalesce(func.sum(Payment.amount), 0)).where(
                Payment.kind == "receivable",
                Payment.paid_at.is_not(None),
                Payment.paid_at >= start_dt,
                Payment.paid_at < end_excl,
            )
        )
    ).scalar_one()
    rev = float(revenue)
    facts["ship_plan"] = rev
    facts["payments_vat"] = rev

    calls_all = (
        await session.execute(
            select(func.count())
            .select_from(CallLog)
            .where(CallLog.started_at >= start_dt, CallLog.started_at < end_excl)
        )
    ).scalar_one()
    facts["calls_all"] = float(calls_all)

    calls_cold = (
        await session.execute(
            select(func.count())
            .select_from(CallLog)
            .where(
                CallLog.started_at >= start_dt,
                CallLog.started_at < end_excl,
                CallLog.direction == "out",
            )
        )
    ).scalar_one()
    facts["calls_cold"] = float(calls_cold)

    new_deals = (
        await session.execute(
            select(func.count())
            .select_from(Deal)
            .where(Deal.created_at >= start_dt, Deal.created_at < end_excl)
        )
    ).scalar_one()
    facts["new_deals_count"] = float(new_deals)

    won_rows = (
        await session.execute(
            select(func.count(), func.coalesce(func.sum(Deal.amount), 0)).where(
                Deal.stage.in_(WON_STAGES),
                Deal.stage_changed_at >= start_dt,
                Deal.stage_changed_at < end_excl,
            )
        )
    ).one()
    won_count = int(won_rows[0] or 0)
    won_sum = float(won_rows[1] or 0)
    facts["won_count"] = float(won_count)
    facts["won_sum"] = won_sum
    facts["avg_deal"] = round(won_sum / won_count, 2) if won_count else 0.0

    # gross_profit и конверсия — позже (нужен landed cost / счета).
    facts["gross_profit"] = 0.0
    facts["invoice_payment_conv"] = 0.0

    return facts
