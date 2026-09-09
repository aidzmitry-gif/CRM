"""Реализация ``core.services.touch_history`` — 360°-досье контрагента (M5).

Ядро (карточка контрагента) читает историю общения через фасад, не залезая в схему
``sales`` (изоляция §2.4). Здесь — реализация, которую ``sales`` регистрирует
(``core.services.touch_history = SalesTouchHistory()``): плоский журнал касаний
контрагента из звонков (``call_log``), сообщений (``message`` по его сделкам) и
самих сделок (``deal``), свежие сверху.

Сделки связаны стабильным counterparty_id. Миграция переносит однозначные старые
связи; оставшиеся записи требуют явного выбора и не смешивают историю.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from modules.sales.models import CallLog, Deal, Message


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt is not None else None


class SalesTouchHistory:
    """История касаний (звонки/сообщения/сделки) контрагента для карточки ядра."""

    async def has_deals(self, session: AsyncSession, counterparty_ids: tuple[int, ...]) -> bool:
        return await session.scalar(select(Deal.id).where(
            Deal.counterparty_id.in_(counterparty_ids),
        ).limit(1)) is not None

    async def _deal_ids(self, session: AsyncSession, counterparty_id: int) -> list[int]:
        return list(await session.scalars(select(Deal.id).where(Deal.counterparty_id == counterparty_id)))

    def _call_filter(self, counterparty_id: int, deal_ids: list[int]):
        # Звонок принадлежит контрагенту, если у него его counterparty_id ИЛИ он привязан
        # к сделке этого контрагента (телефония резолвит не всегда).
        conds = [CallLog.counterparty_id == counterparty_id]
        if deal_ids:
            conds.append(and_(CallLog.counterparty_id.is_(None), CallLog.deal_id.in_(deal_ids)))
        return or_(*conds)

    async def touches(
        self, session: AsyncSession, counterparty_id: int, *, limit: int = 50
    ) -> list[dict]:
        deal_ids = await self._deal_ids(session, counterparty_id)
        out: list[dict] = []

        if deal_ids:
            deals = (
                await session.execute(
                    select(Deal)
                    .where(Deal.id.in_(deal_ids))
                    .order_by(Deal.created_at.desc())
                    .limit(limit)
                )
            ).scalars().all()
            out += [
                {
                    "kind": "deal", "ts": _iso(d.created_at), "channel": None,
                    "direction": None, "title": f"{d.number} · {d.title}", "ref": d.number,
                }
                for d in deals
            ]

        calls = (
            await session.execute(
                select(CallLog)
                .where(self._call_filter(counterparty_id, deal_ids))
                .order_by(CallLog.started_at.desc())
                .limit(limit)
            )
        ).scalars().all()
        out += [
            {
                "kind": "call", "ts": _iso(c.started_at), "channel": "phone",
                "direction": c.direction, "title": c.result or c.status, "ref": c.call_id,
            }
            for c in calls
        ]

        if deal_ids:
            msgs = (
                await session.execute(
                    select(Message)
                    .where(Message.deal_id.in_(deal_ids))
                    .order_by(Message.created_at.desc())
                    .limit(limit)
                )
            ).scalars().all()
            out += [
                {
                    "kind": "message", "ts": _iso(m.created_at), "channel": m.channel,
                    "direction": m.direction, "title": (m.text or "")[:120], "ref": str(m.deal_id),
                }
                for m in msgs
            ]

        # Свежие сверху; записи без ts (теоретически) — в конец.
        out.sort(key=lambda t: t["ts"] or "", reverse=True)
        return out[:limit]

    async def summary(self, session: AsyncSession, counterparty_id: int) -> dict:
        deal_ids = await self._deal_ids(session, counterparty_id)

        async def _count(stmt) -> int:
            return (await session.execute(stmt)).scalar_one()

        async def _max(col, where) -> datetime | None:
            return (
                await session.execute(select(func.max(col)).where(where))
            ).scalar_one_or_none()

        call_where = self._call_filter(counterparty_id, deal_ids)
        calls_n = await _count(select(func.count()).select_from(CallLog).where(call_where))
        last_call = await _max(CallLog.started_at, call_where)

        deals_n = 0
        last_deal: datetime | None = None
        if deal_ids:
            deals_n = await _count(
                select(func.count()).select_from(Deal).where(Deal.id.in_(deal_ids))
            )
            last_deal = await _max(Deal.created_at, Deal.id.in_(deal_ids))

        msgs_n = 0
        last_msg: datetime | None = None
        if deal_ids:
            in_deals = Message.deal_id.in_(deal_ids)
            msgs_n = await _count(select(func.count()).select_from(Message).where(in_deals))
            last_msg = await _max(Message.created_at, in_deals)

        last = max((d for d in (last_call, last_deal, last_msg) if d is not None), default=None)
        return {
            "calls": calls_n,
            "messages": msgs_n,
            "deals": deals_n,
            "total": calls_n + msgs_n + deals_n,
            "last_contact_at": _iso(last),
        }
