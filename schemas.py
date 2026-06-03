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
    """Позиция номенклатуры сделки (с данными связанного SKU и ценами клиенту)."""

    id: int = 0
    sku_id: int
    code: str
    title: str
    unit: str
    qty: float
    last_price: float | None = None  # последняя цена клиенту (Price Engine)
    min_price: float | None = None  # минимальная цена клиенту


class DealItemCreate(BaseModel):
    """Добавить позицию номенклатуры в сделку."""

    sku_id: int
    qty: float = 1.0


class DealItemUpdate(BaseModel):
    """Изменить позицию (количество)."""

    qty: float


class SkuOut(BaseModel):
    """Позиция справочника номенклатуры (для подбора в сделку)."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    code: str
    title: str
    unit: str


class ContactOut(BaseModel):
    """Контактное лицо контрагента (sales-13)."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    full_name: str
    phone: str | None = None
    email: str | None = None
    is_primary: bool


class ContactCreate(BaseModel):
    """Добавить контакт контрагенту сделки."""

    full_name: str
    phone: str | None = None
    email: str | None = None
    is_primary: bool = False


class ChatOut(BaseModel):
    """Диалог по сделке для панели «Чаты и дела» (последнее сообщение)."""

    deal_id: int
    number: str
    company: str
    last_text: str
    channel: str
    direction: str


class PriceQuoteCreate(BaseModel):
    """Зафиксировать котировку цены SKU клиенту (Price Engine)."""

    sku_code: str
    counterparty: str = ""
    price: float


class PriceInfo(BaseModel):
    """Сводка цен по SKU (история → последняя/минимальная цена клиенту)."""

    sku_code: str
    last_price: float | None = None
    min_price: float | None = None
    count: int = 0


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


class MessageCreate(BaseModel):
    """Новое сообщение по сделке (омниканальная переписка)."""

    channel: str = "whatsapp"  # whatsapp|telegram|email|phone|viber
    text: str
    author: str = ""
    direction: str = "out"  # out — от менеджера, in — от клиента


class MessageOut(BaseModel):
    """Сообщение по сделке (история переписки по каналам)."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    channel: str
    direction: str
    author: str
    text: str
    created_at: datetime.datetime


class AiDraftOut(BaseModel):
    """Черновик ответа, сгенерированный AI-подмодулем (Итерация 1)."""

    text: str
    model: str


class AiAssistRequest(BaseModel):
    """Запрос к AI-ассистенту сделки."""

    kind: str = "summary"  # summary | next_step


class AiTextOut(BaseModel):
    """Текст AI-ассистента (резюме сделки / следующий шаг)."""

    kind: str
    text: str
    model: str


class LeadCreate(BaseModel):
    """Приём лида из канала (сайт/мессенджер/e-mail/телефония/тендер)."""

    source: str = "site"  # site|telegram|whatsapp|email|phone|tender
    name: str = ""
    company: str = ""
    phone: str | None = None
    email: str | None = None
    region: str = ""
    product: str = ""
    message: str = ""


class LeadOut(BaseModel):
    """Лид в ответах API (вход воронки: приём → квалификация → распределение)."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    source: str
    name: str
    company: str
    phone: str | None = None
    email: str | None = None
    region: str
    product: str
    message: str
    status: str
    score: int
    qualification: str
    reason: str
    assigned_to: str
    funnel: str
    deal_id: int | None = None


class LeadQualifyOut(BaseModel):
    """Результат квалификации лида: балл, вердикт и (опц.) AI-обоснование."""

    id: int
    status: str
    score: int
    qualification: str
    reason: str
    ai_rationale: str | None = None
    model: str | None = None


class LeadRouteOut(BaseModel):
    """Результат распределения лида: назначенный менеджер и тип воронки."""

    id: int
    status: str
    assigned_to: str
    funnel: str


class LeadConvertOut(BaseModel):
    """Результат конвертации лида в сделку."""

    lead_id: int
    deal_id: int
    number: str
    status: str
