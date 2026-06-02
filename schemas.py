"""Pydantic-схемы API модуля Sales (вход/выход), отдельно от ORM-моделей."""
from __future__ import annotations

import datetime

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


class DealUpdate(BaseModel):
    """Частичное обновление сделки (все поля опциональны)."""

    title: str | None = None
    counterparty: str | None = None
    amount: float | None = None
    priority: str | None = None
    stage: str | None = None
    owner: str | None = None
    next_step: str | None = None
    deal_date: str | None = None
    closed_date: str | None = None
    focus: bool | None = None
    starred: bool | None = None


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


class KpiOut(BaseModel):
    """Показатель «План на сегодня»: факт vs план."""

    key: str
    title: str
    target: float
    actual: float
    percent: int
    unit: str
    icon: str
    tone: str


class DealItemOut(BaseModel):
    """Позиция номенклатуры сделки (с данными связанного SKU)."""

    sku_id: int
    code: str
    title: str
    unit: str
    qty: float


class DocumentCreate(BaseModel):
    """Запрос на формирование документа сделки (счёт/договор/заказ)."""

    kind: str = "invoice"  # invoice | contract | order
    requested_by: str = ""  # инициатор (для согласования договора)


class DocumentDecision(BaseModel):
    """Решение по документу, требующему согласования (договор): провести/отклонить."""

    approved: bool
    by: str = ""


class DocumentOut(BaseModel):
    """Документ сделки: тип, состояние и номер/ссылка в 1С после записи."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    kind: str
    number: str
    status: str
    onec_ref: str | None = None
    amount: float


class DealDetailOut(DealRead):
    """Сделка с позициями номенклатуры и документами (для экрана карточки)."""

    items: list[DealItemOut] = []
    documents: list[DocumentOut] = []


class ActivityCreate(BaseModel):
    """Отметка факта активности (звонок, заявка, отгрузка)."""

    kpi_key: str
    value: float = 1.0
    owner: str = ""
    date: datetime.date | None = None
