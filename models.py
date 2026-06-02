"""ORM-модели модуля Sales (собственная схема ``sales.*``).

Модуль владеет своими таблицами в отдельной схеме (§2.4). Поле `counterparty`
пока денормализовано в строку (как на макете); связь с shared-kernel
`Counterparty` через FK — отдельный шаг, когда понадобится.
"""
from __future__ import annotations

from decimal import Decimal

from sqlalchemy import Numeric, String
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
    stage: Mapped[str] = mapped_column(String(64), default="Новая заявка", server_default="Новая заявка")
    priority: Mapped[str] = mapped_column(String(32), default="Средний", server_default="Средний")
