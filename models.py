"""ORM-модели модуля Sales (собственная схема ``sales.*``)."""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    JSON,
    Date,
    DateTime,
    ForeignKey,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
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
    # SALES-44: прогноз — вероятность (0..100) и ожидаемая дата закрытия
    probability: Mapped[int | None] = mapped_column()
    expected_close_date: Mapped[str | None] = mapped_column(String(32))
    # SALES-43: возраст в стадии и цикл сделки
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    stage_changed_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    # SALES-40: причина отказа (код из справочника loss_reason) и комментарий
    lost_reason_code: Mapped[str | None] = mapped_column(String(32))
    lost_comment: Mapped[str | None] = mapped_column(String(255))
    # Мульти-воронки: код воронки (sales.stage.funnel), дефолт — «новые клиенты».
    funnel: Mapped[str] = mapped_column(
        String(32), default="new_clients", server_default="new_clients"
    )


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
    owner_id: Mapped[int | None] = mapped_column()  # SALES-47: мягкая ссылка на hr.employee
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
    # SALES-51: резерв под счёт/заказ + срок действия счёта (valid_until = +AIOS_INVOICE_VALID_DAYS дн.).
    # reserve_status: none → reserved → consumed (оплата) | released (истёк/снят).
    valid_until: Mapped[date | None] = mapped_column(Date)
    reserve_status: Mapped[str] = mapped_column(String(16), default="none", server_default="none")
    reserved_at: Mapped[datetime | None] = mapped_column(DateTime)
    reminded_at: Mapped[datetime | None] = mapped_column(DateTime)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime)
    # SALES-53: договор по шаблону — ссылка на шаблон + согласованные условия. terms_json
    # хранит и реквизиты покупателя по УНП (registry.lookup), чтобы не править shared-схему
    # Counterparty (адрес/директор там не хранятся).
    template_id: Mapped[int | None] = mapped_column()
    payment_terms: Mapped[str | None] = mapped_column(String(255))
    delivery_terms: Mapped[str | None] = mapped_column(String(255))
    terms_json: Mapped[dict | None] = mapped_column(JSON)


class ContractTemplate(Base):
    """Шаблон договора (SALES-53). ``body`` — текст с плейсхолдерами ``{{...}}``
    (``{{seller.name}}``, ``{{buyer.unp}}``, ``{{items}}``, ``{{payment_terms}}`` …)."""

    __tablename__ = "contract_template"
    __table_args__ = {"schema": "sales"}

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(255))
    body: Mapped[str] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(default=True, server_default="1")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


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
    read_at: Mapped[datetime | None] = mapped_column(DateTime)  # SALES-49: когда прочитано (входящее)


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


class Lead(Base):
    """Лид — вход воронки CRM (приём → квалификация → распределение → сделка).

    Front-of-funnel из ФАЗЫ 1: входящие заявки из каналов (сайт, мессенджеры,
    e-mail, телефония, тендеры) собираются здесь до превращения в ``Deal``.
    ``score``/``qualification`` заполняет квалификатор (эвристики + AI-обоснование,
    §2.5), ``assigned_to``/``funnel`` — движок распределения (правила: география,
    продукт, нагрузка, тип воронки). После конвертации ``deal_id`` ссылается на
    созданную сделку, а ``status`` = ``converted``.
    """

    __tablename__ = "lead"
    __table_args__ = {"schema": "sales"}

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(16), default="site", server_default="site")
    name: Mapped[str] = mapped_column(String(255), default="", server_default="")
    company: Mapped[str] = mapped_column(String(255), default="", server_default="")
    phone: Mapped[str | None] = mapped_column(String(64))
    email: Mapped[str | None] = mapped_column(String(128))
    region: Mapped[str] = mapped_column(String(64), default="", server_default="")
    product: Mapped[str] = mapped_column(String(128), default="", server_default="")
    message: Mapped[str] = mapped_column(Text, default="", server_default="")
    # new → qualified → routed → converted (или rejected при отказе)
    status: Mapped[str] = mapped_column(String(16), default="new", server_default="new")
    score: Mapped[int] = mapped_column(default=0, server_default="0")
    qualification: Mapped[str] = mapped_column(String(16), default="", server_default="")
    reason: Mapped[str] = mapped_column(String(255), default="", server_default="")
    assigned_to: Mapped[str] = mapped_column(String(128), default="", server_default="")
    funnel: Mapped[str] = mapped_column(String(16), default="", server_default="")
    deal_id: Mapped[int | None] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class LossReason(Base):
    """Справочник причин отказа по сделке (SALES-40)."""

    __tablename__ = "loss_reason"
    __table_args__ = {"schema": "sales"}

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True)
    title: Mapped[str] = mapped_column(String(128))
    sort_order: Mapped[int] = mapped_column(default=0, server_default="0")
    active: Mapped[bool] = mapped_column(default=True)


class Stage(Base):
    """Стадия воронки — редактируемый справочник (Сделки 2.0, редактор стадий).

    Источник истины доски/группировки, когда таблица заполнена; иначе код падает на канон
    ``stages.py`` (fallback). ``code`` = значение ``Deal.stage``. ``kind`` — тип стадии:
    ``normal`` | ``won`` (успех) | ``cond_lost`` (условный отказ, реанимируемый) | ``lost``
    (отказ, терминал). ``probability`` — дефолтная вероятность закрытия (0..100, SALES-44).
    """

    __tablename__ = "stage"
    __table_args__ = {"schema": "sales"}

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True)
    title: Mapped[str] = mapped_column(String(128))
    sort_order: Mapped[int] = mapped_column(default=0, server_default="0")
    probability: Mapped[int] = mapped_column(default=0, server_default="0")
    kind: Mapped[str] = mapped_column(String(16), default="normal", server_default="normal")
    color: Mapped[str] = mapped_column(String(16), default="#64748B", server_default="#64748B")
    is_active: Mapped[bool] = mapped_column(default=True, server_default="true")
    # Воронка-владелец стадии (мульти-воронки: new_clients / repeat_clients и т.п.).
    funnel: Mapped[str] = mapped_column(
        String(32), default="new_clients", server_default="new_clients"
    )


class DealStageEvent(Base):
    """История смены стадий сделки (SALES-43): из стадии → в стадию, кто и когда."""

    __tablename__ = "deal_stage_event"
    __table_args__ = {"schema": "sales"}

    id: Mapped[int] = mapped_column(primary_key=True)
    deal_id: Mapped[int] = mapped_column(ForeignKey("sales.deal.id"))
    from_stage: Mapped[str | None] = mapped_column(String(32))
    to_stage: Mapped[str] = mapped_column(String(32))
    changed_by: Mapped[str] = mapped_column(String(128), default="", server_default="")
    changed_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class DealTask(Base):
    """Задача по сделке (SALES-41): что сделать, ответственный, дедлайн, статус.

    ``assignee_id`` — мягкая ссылка на ``hr.employee`` (без cross-schema FK). Просрочка
    вычисляется на лету: ``status == open`` и ``due_at < now``."""

    __tablename__ = "deal_task"
    __table_args__ = {"schema": "sales"}

    id: Mapped[int] = mapped_column(primary_key=True)
    deal_id: Mapped[int] = mapped_column(ForeignKey("sales.deal.id"))
    title: Mapped[str] = mapped_column(String(255))
    kind: Mapped[str] = mapped_column(String(16), default="other", server_default="other")
    assignee_id: Mapped[int | None] = mapped_column()
    due_at: Mapped[datetime | None] = mapped_column(DateTime)
    status: Mapped[str] = mapped_column(String(16), default="open", server_default="open")
    result: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    done_at: Mapped[datetime | None] = mapped_column(DateTime)


class CallLog(Base):
    """Журнал звонков (SALES-50) — события телефонии, склеенные по ``call_id``.

    Источник — коннектор ``integrations`` (облачная АТС): доменные события
    ``telephony.call.*`` апсертятся в одну запись по уникальному id вызова провайдера
    (``call_id``, идемпотентность). Резолв продавца (``owner``) — серверная логика
    sales (знает сделки/контрагентов), коннектор про owner не знает. Мягкие ссылки на
    shared kernel / hr / контакт — без cross-schema FK; ``deal_id`` — FK на свою схему.
    """

    __tablename__ = "call_log"
    __table_args__ = {"schema": "sales"}

    id: Mapped[int] = mapped_column(primary_key=True)
    call_id: Mapped[str] = mapped_column(String(64), unique=True)  # uniqueid провайдера
    direction: Mapped[str] = mapped_column(String(8), default="in", server_default="in")  # in|out
    phone_e164: Mapped[str | None] = mapped_column(String(32))  # клиент (нормализованный)
    did: Mapped[str | None] = mapped_column(String(32))  # внешняя линия (на какую звонил клиент)
    agent_ext: Mapped[str | None] = mapped_column(String(8))  # внутренний номер сотрудника
    owner: Mapped[str] = mapped_column(String(128), default="", server_default="")  # резолвленный продавец
    owner_id: Mapped[int | None] = mapped_column()  # мягкая ссылка hr.employee
    counterparty_id: Mapped[int | None] = mapped_column()  # мягкая ссылка shared kernel
    contact_id: Mapped[int | None] = mapped_column()  # мягкая ссылка shared kernel
    deal_id: Mapped[int | None] = mapped_column(ForeignKey("sales.deal.id"))
    # ringing → answered → ended | missed | busy | failed
    status: Mapped[str] = mapped_column(String(16), default="ringing", server_default="ringing")
    result: Mapped[str | None] = mapped_column(String(255))  # итог/классификация
    comment: Mapped[str | None] = mapped_column(String(1000))
    recording_url: Mapped[str | None] = mapped_column(String(255))
    started_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    answered_at: Mapped[datetime | None] = mapped_column(DateTime)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime)
    duration_sec: Mapped[int | None] = mapped_column()  # разговор, сек
    hold_sec: Mapped[int | None] = mapped_column()  # общее время вызова, сек
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class PlanTarget(Base):
    """Личный план продаж по периоду (SALES-47).

    Продавец ставит себе план, согласует с РОПом через approvals-движок.
    Метрики (``metric``) — 16 показателей: calls, cold_calls, leads, deal_activities,
    new_deals_count, new_deals_amount, invoice_payment_conv, tenders_count,
    tenders_amount, won_count, won_amount, future_won, payments_vat, shipments,
    gross_profit, avg_deal. Валюта: BYN.

    ``period_type`` / ``period_key``: day/2026-06-12, week/2026-W24,
    month/2026-06, quarter/2026-Q2, year/2026.

    Статусы: ``draft`` → ``pending_approval`` → ``approved`` / ``rejected``.
    """

    __tablename__ = "plan_target"
    __table_args__ = (
        UniqueConstraint("owner_id", "metric", "period_type", "period_key", name="uq_plan_target"),
        {"schema": "sales"},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    owner_id: Mapped[int] = mapped_column()  # мягкая ссылка на hr.employee (чей план)
    metric: Mapped[str] = mapped_column(String(32))
    period_type: Mapped[str] = mapped_column(String(8))  # day/week/month/quarter/year
    period_key: Mapped[str] = mapped_column(String(10))  # 2026-06-12 / 2026-W24 / 2026-06 / 2026-Q2 / 2026
    target: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    status: Mapped[str] = mapped_column(String(16), default="draft", server_default="draft")
    approved_by: Mapped[str | None] = mapped_column(String(128))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime)
