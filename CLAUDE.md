# Модуль sales — контекст для Claude

**Тип:** git submodule → https://github.com/aidzmitry-gif/CRM.git  (правка = коммит в этот репозиторий, а не в суперпроект)
**API-префикс:** `/sales`
**Схема БД:** `sales`
**Статус:** наполнен (рабочий сквозной модуль; эталон-образец для остальных). Заглушки остаются в `workflows.py` (тело DealApproval) и `telegram.py` (команда /deals).

## Назначение
CRM-модуль: ведёт сделки по воронке (канбан-доска), вход воронки через лиды
(приём → квалификация → распределение → конвертация в сделку), позиции номенклатуры
со ссылкой на SKU shared-kernel, документы сделки с записью в 1С и согласованием
договоров, омниканальную переписку, KPI «План/Факт», ценовой движок (история котировок)
и AI-ассистента (черновики ответов, резюме, квалификация лидов) за feature-flag.

## Файлы
- `module.py` — `SalesModule(ModuleContract)` + фабрика `get_module()`; метод `register()` подключает всё к ядру.
- `models.py` — 8 ORM-моделей схемы `sales` (Deal, KpiTarget, Activity, DealItem, DealDocument, Message, PriceQuote, Lead).
- `schemas.py` — Pydantic-схемы API (создание/чтение/обновление сделок, лидов, позиций, документов, KPI, AI и т.д.).
- `routes.py` — весь HTTP-API (`router`, tags=`["sales"]`); монтируется под `/sales`.
- `events.py` — обработчики событий (реакция на свои и межмодульные события).
- `repository.py` — `DealRepository(Repository[Deal])`, метод `create()`.
- `permissions.py` — RBAC: `PERMISSIONS` (3 права) и `ROLES` (Менеджер, РОП).
- `workflows.py` — `DealApprovalWorkflow(Workflow)` — заглушка (тело — часть 9 поверх Temporal).
- `telegram.py` — `COMMANDS` = [`/deals`] (заглушка; бот в части 11).
- `stages.py` — `STAGES`: 5 стадий воронки (источник истины для доски/группировки).
- `leads.py` — Lead Qualifier & Router: детерминированный скоринг (`score_lead`), маршрутизация (`route_lead`, `choose_funnel`), `lead_priority`, `MANAGERS`, `LEAD_SOURCES`.
- `ai.py` — AI-подмодуль: `draft_reply`, `summarize`, `next_step`, `qualify_lead` (через шлюз `core.services.llm`).
- `__init__.py` — пустой докстринг-маркер пакета.

## Что регистрирует в ядре (из register())
- Роуты: префикс `/sales`, ~35 эндпоинтов (сделки, доска, KPI, активности, позиции, контакты, чаты, документы, сообщения, цены, лиды, AI).
- Подписки на события:
  - `sales.deal.created` → `on_deal_created`
  - `sales.message.sent` → `on_incoming_message_ai` (AI-агент реактивно)
  - `marketing.campaign.launched` → `on_campaign_launched` (лиды → воронка)
  - `finance.payment.paid` → `on_payment_paid` (оплата → документ paid)
  - `logistics.shipment.delivered` → `on_shipment_delivered` (доставка → сделка won)
- Workflow: `DealApproval` (`DealApprovalWorkflow`, тело — заглушка).
- Permissions: `sales.deal.read`, `sales.deal.write`, `sales.deal.approve`.
- Roles: `Менеджер` (read+write), `РОП` (read+write+approve).
- Telegram-команды: `/deals` (заглушка).
- Widget: `Widget("sales_pipeline", "Воронка продаж", source="sales.deals")`.
- `on_startup`: `_on_startup` (только лог).

## События
- **Публикует** (emit):
  - `sales.deal.created` (создание сделки и конвертация лида)
  - `sales.item.changed` (добавлена позиция)
  - `sales.document.created` (договор отправлен на согласование)
  - `sales.document.posted` (документ записан в 1С)
  - `sales.document.rejected` (договор отклонён)
  - `sales.stock.reserved` (резерв остатков под заказ)
  - `sales.package.sent` (отправлен пакет «счёт + договор», SALES-53)
  - `sales.message.sent` (новое сообщение)
  - `sales.price.quoted` (зафиксирована котировка)
  - `sales.lead.received` / `sales.lead.qualified` / `sales.lead.routed`
  - AI-события: `ai.lead.qualified`, `ai.draft.generated`, `ai.summary.generated`, `ai.next_step.generated`, `ai.draft.suggested` (с `actor: "AI"`, → audit)
- **Подписан на** (subscribe):
  - своё: `sales.deal.created`, `sales.message.sent`
  - межмодульные: `marketing.campaign.launched`, `finance.payment.paid`, `logistics.shipment.delivered`

## Модель данных (таблицы схемы sales)
- **deal** (`Deal`): сделка. `number` (unique), `title`, `counterparty`, `amount`, `priority`, `stage`, `owner`, `next_step`, даты, `focus`/`starred`. Стадия — строка из `STAGES`.
- **kpi_target** (`KpiTarget`): план показателя. `key` (unique), `title`, `target`, `unit`, `icon`/`tone` (UI), `sort_order`.
- **activity** (`Activity`): факт активности за дату. `kpi_key`, `owner`, `value`, `date`. Факт KPI = сумма value за окно периода.
- **deal_item** (`DealItem`): позиция сделки. FK `deal_id` → sales.deal; `sku_id` (мягкая ссылка на shared-kernel `Sku`, без cross-schema FK); `qty`.
- **deal_document** (`DealDocument`): документ сделки. FK `deal_id`; `kind` (invoice|contract|order), `number`, `status` (draft→posted / pending_approval→posted|rejected / paid), `onec_ref`, `amount`, `created_at`/`posted_at`; резерв (SALES-51: `valid_until`/`reserve_status`/…); договор (SALES-53: `template_id`, `payment_terms`, `delivery_terms`, `terms_json` — в т.ч. реквизиты покупателя по УНП, чтобы не править shared-схему).
- **contract_template** (`ContractTemplate`, SALES-53): шаблон договора. `code` (unique), `name`, `body` (текст с плейсхолдерами `{{...}}`), `is_active`.
- **message** (`Message`): сообщение. FK `deal_id`; `channel` (whatsapp|telegram|email|phone|viber), `direction` (in|out), `author`, `text`, `created_at`.
- **price_quote** (`PriceQuote`): котировка цены. `sku_code`, `counterparty`, `price`, `created_at`. Из истории — последняя/минимальная цена клиенту.
- **lead** (`Lead`): вход воронки. `source`, контакты, `region`, `product`, `message`; `status` (new→qualified→routed→converted / rejected), `score`, `qualification` (target|non-target), `reason`, `assigned_to`, `funnel`, `deal_id` (после конвертации).

Внешние (shared kernel, `core.domain.models`): `Sku`, `Counterparty`, `Contact`, `Approval` — используются в роутах через join/связь по имени.

## API-эндпоинты (основные)
- `GET /ping` — модуль смонтирован.
- `GET /board` — канбан-доска: сделки по стадиям + агрегаты (count/sum).
- `GET /kpis?period=day|week|month|quarter|year` — План/Факт за период.
- `POST /activities` — отметить активность.
- `GET /deals`, `POST /deals`, `GET /deals/{id}` (detail с позициями+документами), `PATCH /deals/{id}`.
- `POST /deals/{id}/request-approval` — отправить сделку на согласование.
- `GET /skus` — справочник номенклатуры.
- `GET|POST /deals/{id}/items`, `PATCH|DELETE /deal-items/{item_id}` — позиции.
- `GET|POST /deals/{id}/contacts`, `PATCH /contacts/{id}/primary` — контакты контрагента.
- `GET /chats` — диалоги для панели «Чаты и дела».
- `GET|POST /deals/{id}/documents`, `POST /documents/{id}/decide` — документы (счёт/договор/заказ); decide требует право `sales.deal.approve`.
- **SALES-53 (договор по шаблону):** `GET|POST /contract-templates` (шаблоны с плейсхолдерами `{{...}}`); `POST /deals/{id}/contract` — подготовить договор по шаблону + реквизиты покупателя по УНП (`core.services.registry.lookup`, graceful), условия предзаполнены из сделки, уходит на согласование (409 если активный договор уже есть); `GET /documents/{id}/render` — печатная HTML-форма (право `sales.deal.read`); `POST /deals/{id}/send-package` — пакет «счёт+договор» одной записью после согласования. Реквизиты продавца — конфиг `AIOS_SELLER_*`.
- `GET|POST /deals/{id}/messages` — омниканальная переписка.
- `GET /prices/{sku_code}`, `POST /prices` — Price Engine.
- `GET /leads?status=`, `POST /leads`, `GET /leads/{id}` — приём лидов.
- `POST /leads/{id}/qualify`, `/route`, `/convert` — квалификация → распределение → сделка.
- `POST /deals/{id}/ai/draft-reply`, `/ai/assist` — AI (503 при выключенном feature-flag).

## Межмодульные связи и зависимости
- Реагирует на события: `marketing.campaign.launched` (создаёт лиды в приёме), `finance.payment.paid` (счёт → paid по `number == ref`), `logistics.shipment.delivered` (сделка → `won` по `deal_id`).
- Сервисы ядра (`core.services`): `onec` (запись документов в 1С — `post_document`), `stock` (резерв остатков под заказ — `reserve`), `approvals` (согласование договоров/сделок — `request`/`decide`), `llm` (AI-шлюз, `enabled`/`model`/`complete`), `event_bus` (outbox), `auth` (`require_permission`).
- Shared kernel: `Sku`, `Counterparty`, `Contact`, `Approval` из `core.domain.models`.

## Подводные камни / важные детали
- **Стадии воронки** (`stages.py`, id): `new`, `qual`, `prop`, `appr`, `won`. Модели по умолчанию `stage="new"`. Логистика закрывает в `won`.
- **Лид-воронка**: статусы `new → qualified → routed → converted` (или `rejected`). Конвертация требует статус `routed` (иначе 409); создаёт сделку `CRM-LEAD-{id}` со стадией `new`.
- **Документы**: счёт/заказ пишутся в 1С сразу (`draft→posted`); договор (`REQUIRES_APPROVAL={"contract"}`) идёт через согласование (`pending_approval`), запись в 1С — после `POST /documents/{id}/decide`. Заказ (`RESERVES_STOCK={"order"}`) резервирует остатки. Отсутствие интеграции 1С → 503.
- **KPI план/факт**: факт — сумма `Activity.value` в окне `PERIOD_DAYS` от последней даты активности; план — `target * PERIOD_MULT` (рабочие дни периода).
- **AI за feature-flag** `AIOS_AI_ENABLED`: при выкл AI-эндпоинты возвращают 503, квалификация лида падает на детерминированный скоринг (`leads.score_lead`, порог `QUALIFY_THRESHOLD=50`), эмитит `sales.lead.qualified` вместо `ai.lead.qualified`.
- **AI-агент реактивно**: `on_incoming_message_ai` срабатывает на `sales.message.sent` только для `direction == "in"` и при включённом AI (обработчик с `(payload, ctx)`).
- **Связь с SKU** — мягкая (без cross-schema FK), резолвится join-ом по `sku_id`/`code` в роутах. Контрагент сделки связывается по имени (`counterparty == name`), создаётся при добавлении контакта.
- Репозиторий не коммитит — транзакцией владеет роут; `event_bus.emit` пишет в outbox в той же транзакции до `session.commit()`.
- `_utcnow()` — наивный UTC (единообразие SQLite/PostgreSQL).