"""Pydantic-схемы API модуля Sales (вход/выход), отдельно от ORM-моделей."""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class DealCreate(BaseModel):
    """Данные для создания сделки."""

    number: str
    title: str
    counterparty: str
    amount: float = 0.0
    priority: str = "Средний"
    stage: str = "new"
    owner: str = ""
    next_step: str | None = None
    deal_date: str | None = None
    closed_date: str | None = None
    focus: bool = False
    starred: bool = False


class DealRead(BaseModel):
    """Представление сделки в ответах API."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    number: str
    title: str
    counterparty: str
    amount: float
    priority: str
    stage: str
    owner: str
    next_step: str | None = None
    deal_date: str | None = None
    closed_date: str | None = None
    focus: bool
    starred: bool


class StageBoard(BaseModel):
    """Колонка канбана: стадия + её сделки и агрегаты."""

    id: str
    title: str
    color: str
    count: int
    sum: float
    deals: list[DealRead]


class BoardOut(BaseModel):
    """Вся доска сделок."""

    stages: list[StageBoard]
