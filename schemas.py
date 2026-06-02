"""Pydantic-схемы API модуля Sales (вход/выход), отдельно от ORM-моделей."""
from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict


class DealStage(str, Enum):
    """Стадии воронки «Новые клиенты» (как на макете)."""

    NEW = "Новая заявка"
    QUALIFICATION = "Квалификация"
    PROPOSAL = "Коммерческое предложение"
    APPROVAL = "Согласование"
    CLOSED = "Закрыто"


class DealCreate(BaseModel):
    """Данные для создания сделки."""

    number: str
    title: str
    counterparty: str
    amount: float = 0.0
    stage: DealStage = DealStage.NEW
    priority: str = "Средний"


class DealRead(BaseModel):
    """Представление сделки в ответах API."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    number: str
    title: str
    counterparty: str
    amount: float
    stage: str
    priority: str
