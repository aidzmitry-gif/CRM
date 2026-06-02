"""ORM-модели модуля Sales (собственная схема ``sales.*``)."""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Date, DateTime, ForeignKey, Numeric, String, Text, func
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


class DealDocument(Base):
    """Документ сделки — счёт / договор / заказ — и его запись в 1С (часть 9).

    ``status`` отражает этап (sales-9..11): кнопки в карточке меняют состояние.
    Счёт пишется в 1С сразу (``draft`` → ``posted``); для договора добавится
    ветка через согласование (часть 4). ``onec_ref`` — номер/ссылка документа
    в 1С после записи; запись идёт через фасад ядра ``core.services.onec``.
    """

    __tablename__ = "deal_document"
    __table_args__ = {"schema": "sales"}

    id: Mapped[int] = mapped_column(primary_key=True)
    deal_id: Mapped[int] = mapped_column(ForeignKey("sales.deal.id"))
    kind: Mapped[str] = mapped_column(String(32))  # invoice | contract | order
    number: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), default="draft", server_default="draft")
    onec_ref: Mapped[str | None] = mapped_column(String(64))
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0"), server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    posted_at: Mapped[datetime | None] = mapped_column(DateTime)


class Message(Base):
    """Сообщение по сделке — омниканальная история переписки (часть 10, sales-16).

    ``channel`` — канал (whatsapp/telegram/email/phone/viber), ``direction`` —
    входящее от клиента (``in``) или исходящее от менеджера (``out``).
    """

    __tablename__ = "message"
    __table_args__ = {"schema": "sales"}

    id: Mapped[int] = mapped_column(primary_key=True)
    deal_id: Mapped[int] = mapped_column(ForeignKey("sales.deal.id"))
    channel: Mapped[str] = mapped_column(String(16))  # whatsapp|telegram|email|phone|viber
    direction: Mapped[str] = mapped_column(String(8), default="out", server_default="out")  # in|out
    author: Mapped[str] = mapped_column(String(128), default="", server_default="")
    text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class PriceQuote(Base):
    """Котировка цены SKU клиенту — история цен и минимальная цена (часть 10, sales-22).

    Накапливает предложенные цены по (``sku_code``, ``counterparty``); из истории
    считаются последняя и минимальная цена, отдаваемая клиенту.
    """

    __tablename__ = "price_quote"
    __table_args__ = {"schema": "sales"}

    id: Mapped[int] = mapped_column(primary_key=True)
    sku_code: Mapped[str] = mapped_column(String(64))
    counterparty: Mapped[str] = mapped_column(String(255), default="", server_default="")
    price: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
