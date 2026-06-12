"""Стадии воронки «Новые клиенты» — источник истины для доски и группировки.

Продажная под-воронка — 9 активных стадий (§0.1 ТЗ Сделки 2.0):
  new → in_work → qual → meeting → price_req → price_ready →
  invoice_sent → invoice_appr → contract
Терминальные: cond_lost (ждёт согласования РОПа) → lost, won.

Цвета — для UI; порядок в списке = порядок колонок на канбане.
"""

STAGES: list[dict] = [
    # --- продажная под-воронка ---
    {"id": "new",          "title": "Новая заявка",         "color": "#3B82F6"},
    {"id": "in_work",      "title": "Взят в работу",        "color": "#6366F1"},
    {"id": "qual",         "title": "Квалифицирован (ЛПР)", "color": "#8B5CF6"},
    {"id": "meeting",      "title": "Встреча назначена",    "color": "#F59E0B"},
    {"id": "price_req",    "title": "Цена запрошена",       "color": "#F97316"},
    {"id": "price_ready",  "title": "Есть цена",            "color": "#EAB308"},
    {"id": "invoice_sent", "title": "Счёт отправлен",       "color": "#14B8A6"},
    {"id": "invoice_appr", "title": "Счёт защищён",         "color": "#06B6D4"},
    {"id": "contract",     "title": "Договор / Предоплата", "color": "#10B981"},
    # --- терминалы ---
    {"id": "cond_lost",    "title": "Условный отказ",       "color": "#FB923C"},
    {"id": "won",          "title": "Закрыто: Успешно",     "color": "#22C55E"},
    {"id": "lost",         "title": "Закрыто: Отказ",       "color": "#EF4444"},
]

# Стадии, в которых сделка считается закрытой (исключаются из «висяков»).
TERMINAL_STAGES: frozenset[str] = frozenset({"won", "lost"})

# Дефолтная вероятность закрытия (%) по стадии (SALES-44).
# Переопределяется полем Deal.probability, если задано вручную.
PROBABILITY_BY_STAGE: dict[str, int] = {
    "new":          5,
    "in_work":     10,
    "qual":        20,
    "meeting":     35,
    "price_req":   45,
    "price_ready": 55,
    "invoice_sent": 70,
    "invoice_appr": 80,
    "contract":    90,
    "cond_lost":    5,
    "won":        100,
    "lost":         0,
}

# Быстрый lookup: id → dict (для валидации и UI).
STAGE_BY_ID: dict[str, dict] = {s["id"]: s for s in STAGES}
