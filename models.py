"""Собственные модели модуля Sales (схема ``sales.*``).

На этапе каркаса — лёгкая Pydantic-модель сделки и перечень стадий воронки.
В части 7 «Карточка сделки» становится SQLAlchemy-моделью со своей схемой и
миграциями; здесь — демонстрация принципа «модуль владеет своими таблицами» (§2.4).
"""
from __future__ import annotations

from enum import Enum

from pydantic import BaseModel


class DealStage(str, Enum):
    """Стадии воронки «Новые клиенты» (как на макете)."""

    NEW = "Новая заявка"
    QUALIFICATION = "Квалификация"
    PROPOSAL = "Коммерческое предложение"
    APPROVAL = "Согласование"
    CLOSED = "Закрыто"


class Deal(BaseModel):
    """Сделка CRM."""

    id: int | None = None
    number: str
    title: str
    counterparty: str
    amount: float = 0.0
    stage: DealStage = DealStage.NEW
    priority: str = "Средний"
