"""Pydantic-схемы API модуля Sales (вход/выход), отдельно от ORM-моделей."""
from __future__ import annotations

import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


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
    next_step_at: datetime.datetime | None = None
    deal_date: str | None = None
    closed_date: str | None = None
    focus: bool = False
    starred: bool = False
    probability: int | None = None
    expected_close_date: str | None = None
    funnel: str = "new_clients"
    # Крайняя дата отгрузки + штрафные санкции за опоздание (дата уходит в закупки).
    # Границы режут переполнение колонок БД (Numeric(6,2)/String) в 422 на валидации, а не в 500.
    ship_deadline: str | None = Field(default=None, max_length=32)
    penalty_rate_pct: float | None = Field(default=None, ge=0, le=9999.99)
    penalty_cap_pct: float | None = Field(default=None, ge=0, le=9999.99)
    penalty_terms: str | None = Field(default=None, max_length=512)


class DealUpdate(BaseModel):
    """Частичное обновление сделки (все поля опциональны)."""

    title: str | None = None
    counterparty: str | None = None
    amount: float | None = None
    priority: str | None = None
    stage: str | None = None
    owner: str | None = None
    next_step: str | None = None
    next_step_at: datetime.datetime | None = None
    deal_date: str | None = None
    closed_date: str | None = None
    focus: bool | None = None
    starred: bool | None = None
    probability: int | None = None
    expected_close_date: str | None = None
    # Смена воронки (мульти-воронки): фиксируется в истории как смена стадии.
    funnel: str | None = None
    # Крайняя дата отгрузки + штраф за опоздание; смена даты → сигнал в закупки.
    # Границы (см. DealCreate) режут переполнение колонок БД в 422, а не в 500.
    ship_deadline: str | None = Field(default=None, max_length=32)
    penalty_rate_pct: float | None = Field(default=None, ge=0, le=9999.99)
    penalty_cap_pct: float | None = Field(default=None, ge=0, le=9999.99)
    penalty_terms: str | None = Field(default=None, max_length=512)


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
    next_step_at: datetime.datetime | None = None
    deal_date: str | None = None
    closed_date: str | None = None
    focus: bool
    starred: bool
    probability: int | None = None
    expected_close_date: str | None = None
    created_at: datetime.datetime | None = None
    stage_changed_at: datetime.datetime | None = None
    lost_reason_code: str | None = None
    lost_comment: str | None = None
    funnel: str = "new_clients"
    # Крайняя дата отгрузки + штрафные санкции за опоздание.
    ship_deadline: str | None = None
    penalty_rate_pct: float | None = None
    penalty_cap_pct: float | None = None
    penalty_terms: str | None = None
    # Живой бейдж «под приход» (П6 UI ТЗ) — читается из OutboxEvent, не из колонки.
    supply_arrived_at: datetime.datetime | None = None
    supply_arrived_sku: str | None = None


class StageBoard(BaseModel):
    """Колонка канбана: стадия + её сделки и агрегаты."""

    id: str
    title: str
    color: str
    count: int
    sum: float
    weighted: float = 0.0  # SALES-44: Σ(amount × вероятность) по колонке
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
    unread: int = 0  # SALES-49: непрочитанных входящих по диалогу


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
    valid_until: datetime.date | None = None  # SALES-51: срок действия счёта (резерв)
    reserve_status: str = "none"  # none | reserved | consumed | released


class ContractTemplateOut(BaseModel):
    """Шаблон договора для выбора в окне «Подготовить договор» (SALES-53)."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    code: str
    name: str


class ContractTemplateCreate(BaseModel):
    """Создание/сидирование шаблона договора (SALES-53)."""

    code: str
    name: str
    body: str  # текст с плейсхолдерами {{...}}


class ContractPrepareIn(BaseModel):
    """SALES-53: подготовить договор по шаблону + реквизиты покупателя по УНП."""

    template_code: str
    unp: str = ""  # УНП покупателя → core.services.registry.lookup (graceful при выкл)
    payment_terms: str = ""
    delivery_terms: str = ""
    terms: dict | None = None  # прочие согласованные условия (структурно)
    requested_by: str = ""  # инициатор согласования


class PackageSentOut(BaseModel):
    """SALES-53: результат отправки пакета «счёт + договор»."""

    deal_id: int
    invoice_number: str
    contract_number: str
    channel: str
    sent: bool = True


class CounterpartyRef(BaseModel):
    """Резолв контрагента сделки в MDM-витрине (golden record) — провенанс для карточки.

    Связь сделки с контрагентом — мягкая, по имени; резолвится на чтении (без дубля MDM,
    [[spravochniki-mdm-decision]]). ``None`` на выходе, если имени нет в MDM → honest-empty
    на фронте. ``sources`` — внешние системы-источники из ``CounterpartyAlias`` (1c|bitrix|
    erp|merge); ``unp`` — natural key РБ для бейджа ``<SourceTag>``.
    """

    id: int
    name: str
    unp: str | None = None
    sources: list[str] = []
    is_active: bool = True
    merged_into_id: int | None = None


class DealDetailOut(DealRead):
    """Сделка с позициями номенклатуры и документами (для экрана карточки)."""

    items: list[DealItemOut] = []
    documents: list[DocumentOut] = []
    # Контрагент из MDM (резолв по имени на чтении); None — нет в витрине (honest-empty).
    counterparty_ref: CounterpartyRef | None = None


StageKind = Literal["normal", "won", "cond_lost", "lost"]


class StageOut(BaseModel):
    """Стадия воронки для доски/редактора (Сделки 2.0)."""

    model_config = ConfigDict(from_attributes=True)

    code: str
    title: str
    sort_order: int
    probability: int
    kind: StageKind
    color: str
    is_active: bool
    funnel: str = "new_clients"


class StageCreate(BaseModel):
    """Создание стадии воронки (редактор стадий)."""

    code: str = Field(min_length=1, max_length=32)
    title: str = Field(min_length=1, max_length=128)
    sort_order: int = 0
    probability: int = Field(default=0, ge=0, le=100)
    kind: StageKind = "normal"
    color: str = "#64748B"
    is_active: bool = True
    funnel: str = "new_clients"


class StageUpdate(BaseModel):
    """Частичное обновление стадии (порядок/вероятность/тип/имя/цвет/активность/воронки)."""

    title: str | None = Field(default=None, min_length=1, max_length=128)
    sort_order: int | None = None
    probability: int | None = Field(default=None, ge=0, le=100)
    kind: StageKind | None = None
    color: str | None = None
    is_active: bool | None = None
    funnel: str | None = None


class FunnelOut(BaseModel):
    """Воронка sales (мульти-воронки): код + титул + счётчик активных сделок."""

    code: str
    title: str
    active_deals: int = 0


class HandoffItem(BaseModel):
    """Позиция в handoff-передаче в исполнение."""

    sku_code: str
    title: str
    qty: float


class DealHandoffOut(BaseModel):
    """Сводка «передано в исполнение» по выигранной сделке (П10 ТЗ): полезная нагрузка
    события ``sales.deal.handoff`` (контракт для downstream — логистика/финансы/офис).
    None — события ещё нет (сделка не won или handoff не эмитнут)."""

    deal_id: int
    number: str
    counterparty: str
    amount: float
    owner: str
    funnel: str
    items: list[HandoffItem] = []
    gross_profit: float | None = None
    handed_off_at: datetime.datetime | None = None


PlanStatus = Literal["draft", "pending_approval", "approved", "rejected"]


class PlanTargetOut(BaseModel):
    """План показателя продавца (PlanTarget) для UI: id/owner/метрика/период/цель/статус."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    owner_id: int
    metric: str
    period_type: str
    period_key: str
    target: float
    status: PlanStatus
    approved_by: str | None = None
    approved_at: datetime.datetime | None = None


class PlanTargetIn(BaseModel):
    """Upsert плана продавца: продавец заявляет (или редактирует draft) свою цель."""

    owner_id: int
    metric: str = Field(min_length=1, max_length=32)
    period_type: Literal["day", "week", "month", "quarter", "year"]
    period_key: str = Field(min_length=1, max_length=10)
    target: float = Field(ge=0)


class PlanDecisionIn(BaseModel):
    """Решение РОП по плану: approve/reject + опц. комментарий."""

    approved: bool
    comment: str | None = None


class StageAnalytics(BaseModel):
    """Аналитика стадии воронки (П6 ТЗ): количество, суммы, конверсия, средний возраст."""

    id: str
    title: str
    color: str
    count: int
    sum: float
    weighted: float
    avg_age_days: float | None = None  # средний возраст текущих сделок в стадии
    next_conv_pct: int | None = None    # доля сделок этой стадии, ушедших в следующую (по истории)


class PipelineAnalyticsOut(BaseModel):
    """Pipeline-аналитика воронки: стадии + сводные показатели по воронке (forecast/cycle)."""

    funnel: str
    stages: list[StageAnalytics]
    forecast_weighted: float
    avg_cycle_days: float | None = None  # средний цикл won-сделок (created_at→closed)
    won_count: int = 0


MarginLineStatus = Literal["priced", "no_price", "no_cost"]


class MarginLine(BaseModel):
    """Маржа позиции сделки: цена клиенту × кол-во минус landed себестоимость × кол-во.

    ``status`` — honest-разбивка: ``priced`` (есть цена И landed → попадает в gross);
    ``no_price`` (нет последней котировки клиенту); ``no_cost`` (нет закрытого landed по SKU).
    Позиции без обоих в gross НЕ попадают (не маскируем дыру нулём — [[landed_cost]]).
    """

    sku_code: str
    title: str
    qty: float
    unit_price: float | None = None
    revenue: float | None = None
    unit_landed_cost: float | None = None
    cogs: float | None = None
    margin_pct: float | None = None
    status: MarginLineStatus
    # Провенанс себестоимости из landed: shipment_id/fixed_at/fx_rate (None если нет landed).
    cost_shipment_id: int | None = None
    cost_fixed_at: datetime.datetime | None = None
    cost_fx_rate: float | None = None


class DealMarginOut(BaseModel):
    """Факт-маржа сделки через landed cost ([[pricing-calculation-todo]] — методику не изобретаем).

    ``revenue``/``cogs_landed``/``gross_profit`` — суммы по позициям со статусом ``priced``;
    ``margin_pct`` — round(gross/revenue×100) или ``None`` при revenue=0. ``priced_count``/
    ``total_count`` — частичная оценка для бейджа «N из M». ``reason`` — причина деградации
    (нет landed-фасада или ничего не оценено), None — есть хоть одна priced-позиция.
    """

    deal_id: int
    revenue: float
    cogs_landed: float | None
    gross_profit: float | None
    margin_pct: int | None
    priced_count: int
    total_count: int
    reason: str | None = None
    lines: list[MarginLine] = []


class MarginForecastOut(BaseModel):
    """Взвешенный прогноз валовой маржи воронки (S3-1) — маржа из карточки на уровень воронки.

    ``revenue_weighted`` — Σ(выручка по цене клиенту × вероятность стадии); НЕ зависит от
    landed, всегда число. ``gross_weighted`` — Σ(вал.прибыль ``priced``-позиций × вероятность);
    ``None`` при отсутствии фасада landed_cost (честная деградация, НЕ 0). ``margin_pct_blended``
    — round(gross/revenue×100) или None. ``deals_priced``/``deals_total`` — покрытие маржой
    (по скольким активным сделкам она вообще считается). ``reason`` — причина деградации.
    """

    funnel: str
    owner: str | None = None
    revenue_weighted: float
    gross_weighted: float | None
    margin_pct_blended: int | None
    deals_priced: int
    deals_total: int
    reason: str | None = None


class MarginReconcileOut(BaseModel):
    """Сверка прогнозной маржи sales с фактической себестоимостью из аудита (S3-4, ось A).

    Уровень — sku/агрегат сделки: событие ``procurement.landed_cost.calculated`` НЕ несёт
    deal_id (PO обслуживает много сделок), поэтому сверяем по ``sku_code`` позиций.
    ``sales_forecast_gross`` — наш расчёт (landed snapshot фасада); ``finance_actual_gross`` —
    та же выручка минус landed из аудита событий шины; ``None`` если фактов нет.
    ``delta`` = sales − finance. ``status``: ``converged`` (|delta|<0.01) / ``diverged`` /
    ``no_finance`` (нет landed-событий по позициям) — никогда не 500.
    """

    deal_id: int
    sales_forecast_gross: float | None
    finance_actual_gross: float | None
    delta: float | None
    level: Literal["sku_aggregate"] = "sku_aggregate"
    status: Literal["converged", "diverged", "no_finance"]


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


class CallScriptOut(BaseModel):
    """SALES-54: скрипт результативного звонка по стадии (со-пилот продавца).

    Каркас (goal/target_action/talking_points/questions) детерминирован по стадии —
    работает и без AI. ``ai_hint`` — доп. подсказка AI (None при выключенном AI-слое).
    """

    stage: str
    goal: str
    target_action: str
    talking_points: list[str]
    questions: list[str]
    ai_hint: str | None = None
    model: str  # "static" | имя модели | "mock"


class ObjectionReplyIn(BaseModel):
    """SALES-54: реплика-возражение клиента для подсказки ответа."""

    objection: str


class ObjectionReplyOut(BaseModel):
    """SALES-54: категория возражения + ответ-подсказка (+ AI-подсказка при вкл. AI)."""

    category: str  # price | stock | think | competitor | other
    reply: str
    ai_hint: str | None = None
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


class LossReasonOut(BaseModel):
    """Причина отказа из справочника (SALES-40)."""

    model_config = ConfigDict(from_attributes=True)

    code: str
    title: str


class LoseRequest(BaseModel):
    """Закрыть сделку в отказ: причина (обязательна) + комментарий."""

    reason_code: str
    comment: str | None = None


class StageEventOut(BaseModel):
    """Запись истории смены стадий сделки (SALES-43)."""

    model_config = ConfigDict(from_attributes=True)

    from_stage: str | None = None
    to_stage: str
    changed_by: str
    changed_at: datetime.datetime


class TaskCreate(BaseModel):
    """Новая задача по сделке (SALES-41)."""

    title: str
    kind: str = "other"  # call|meeting|email|chat|doc|other
    assignee_id: int | None = None
    due_at: datetime.datetime | None = None


class TaskUpdate(BaseModel):
    """Изменение задачи: перенос срока, исполнение, отмена."""

    title: str | None = None
    kind: str | None = None
    assignee_id: int | None = None
    due_at: datetime.datetime | None = None
    status: str | None = None  # open|done|canceled
    result: str | None = None


class TaskOut(BaseModel):
    """Задача по сделке (``overdue`` — вычисляемый флаг просрочки)."""

    id: int
    deal_id: int
    title: str
    kind: str
    assignee_id: int | None = None
    due_at: datetime.datetime | None = None
    status: str
    result: str | None = None
    overdue: bool = False


class CallOut(BaseModel):
    """Запись журнала звонков (SALES-50)."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    call_id: str
    direction: str
    phone_e164: str | None = None
    did: str | None = None
    agent_ext: str | None = None
    owner: str
    owner_id: int | None = None
    counterparty_id: int | None = None
    contact_id: int | None = None
    deal_id: int | None = None
    status: str
    result: str | None = None
    comment: str | None = None
    recording_url: str | None = None
    started_at: datetime.datetime
    answered_at: datetime.datetime | None = None
    ended_at: datetime.datetime | None = None
    duration_sec: int | None = None
    hold_sec: int | None = None


class CallCommentIn(BaseModel):
    """Комментарий к звонку."""

    comment: str


class CallResultIn(BaseModel):
    """Итог/классификация звонка."""

    result: str


class CallLinkDealIn(BaseModel):
    """Привязка звонка к сделке или создание новой сделки из звонка."""

    deal_id: int | None = None
    create: bool = False


class TelephonyEventIn(BaseModel):
    """Валидированное событие телефонии для прямого приёма ``POST /telephony/incoming``.

    Прямой приём (fallback/тест) минует шину, поэтому валидируем строго: только известные
    поля (``extra='forbid'``) с проверенными типами — иначе недоверенное тело могло бы
    фабриковать ``CallLog`` с произвольными полями (security-review HIGH). Набор полей —
    ровно те, что читают обработчики ``calls.record_event``/``EVENT_HANDLERS``.
    """

    model_config = ConfigDict(extra="forbid")

    event_type: str = "telephony.call.incoming"
    call_id: str = Field(min_length=1)
    direction: str | None = None
    phone_e164: str | None = None
    did: str | None = None
    agent_ext: str | None = None
    status: str | None = None
    event: str | None = None
    to_ext: str | None = None
    duration_sec: int | None = None
    hold_sec: int | None = None
    recording_url: str | None = None
