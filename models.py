"""ORM-модели модуля Sales (собственная схема ``sales.*``)."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import Date, ForeignKey, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from core.db.base import Base


class Deal(Base):
    """Сделка CRM."""

    __tablename__ = "deal"
    __table_args__ = {"schema": "sales"}

    id: Mapped[int] = mapped_column(primary_key=True)
    number: Mapped[str] = mapped_column(String(64), unique=True)
    title: Mapped[str] = mapped_column(String(255))
    counterparty: Mapped[str] = mapped_column(String(255))
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0"), server_default="0")
    priority: Mapped[str] = mapped_column(String(32), default="Средний", server_default="Средний")
    stage: Mapped[str] = mapped_column(String(32), default="new", server_default="new")
    owner: Mapped[str] = mapped_column(String(128), default="", server_default="")
    next_step: Mapped[str | None] = mapped_column(String(128))
    deal_date: Mapped[str | None] = mapped_column(String(32))
    closed_date: Mapped[str | None] = mapped_column(String(32))
    focus: Mapped[bool] = mapped_column(default=False)
    starred: Mapped[bool] = mapped_column(default=False)


class KpiTarget(Base):
    """Цель (план) показателя «План на сегодня». icon/tone — подсказки для UI."""

    __tablename__ = "kpi_target"
    __table_args__ = {"schema": "sales"}

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(32), unique=True)
    title: Mapped[str] = mapped_column(String(128))
    target: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    unit: Mapped[str] = mapped_column(String(16), default="count", server_default="count")
    icon: Mapped[str] = mapped_column(String(16))
    tone: Mapped[str] = mapped_column(String(16))
    sort_order: Mapped[int] = mapped_column(default=0, server_default="0")


class Activity(Base):
    """Факт активности (звонок, обработка заявки, отгрузка) за дату.

    ``value`` — вклад в показатель: 1 для счётных метрик, сумма для денежных.
    """

    __tablename__ = "activity"
    __table_args__ = {"schema": "sales"}

    id: Mapped[int] = mapped_column(primary_key=True)
    kpi_key: Mapped[str] = mapped_column(String(32))
    owner: Mapped[str] = mapped_column(String(128), default="", server_default="")
    value: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("1"), server_default="1")
    date: Mapped[date] = mapped_column(Date)


class DealItem(Base):
    """Позиция номенклатуры в сделке — ссылка на shared-kernel SKU (§2.4).

    Жёсткий cross-schema FK на ``sku`` не ставим (sku в общем ядре); связь
    разрешается на чтении join-ом в эндпоинте.
    """

    __tablename__ = "deal_item"
    __table_args__ = {"schema": "sales"}

    id: Mapped[int] = mapped_column(primary_key=True)
    deal_id: Mapped[int] = mapped_column(ForeignKey("sales.deal.id"))
    sku_id: Mapped[int] = mapped_column()
    qty: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("1"), server_default="1")
