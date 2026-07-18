"""Стадии воронки «Новые клиенты» — источник истины для доски и группировки.

Цвета — для UI; порядок в списке = порядок колонок на канбане.
9 стадий «Сделок 2.0» (цена ДО встречи) — см. coordination/sales-2.0-contract.md.
"""

STAGES: list[dict] = [
    {"id": "new", "title": "Новая заявка", "color": "#3B82F6"},
    {"id": "qual", "title": "Квалифицирован", "color": "#8B5CF6"},
    {"id": "price_req", "title": "Цена запрошена", "color": "#F59E0B"},
    {"id": "has_price", "title": "Есть цена", "color": "#EAB308"},
    {"id": "meeting", "title": "Встреча назначена", "color": "#14B8A6"},
    {"id": "invoice", "title": "Счёт отправлен", "color": "#0EA5E9"},
    {"id": "protected", "title": "Счёт защищён", "color": "#6366F1"},
    {"id": "contract", "title": "Договор/предоплата", "color": "#10B981"},
    {"id": "won", "title": "Успех", "color": "#22C55E"},
    {"id": "cond_lost", "title": "Условный отказ", "color": "#F97316"},
    {"id": "lost", "title": "Отказ", "color": "#EF4444"},
]

# Стадии, в которых сделка считается закрытой (исключаются из «висяков»).
# cond_lost — реанимируемый, НЕ терминал.
TERMINAL_STAGES: frozenset[str] = frozenset({"won", "lost"})

# Дефолтная вероятность закрытия (%) по стадии (SALES-44).
PROBABILITY_BY_STAGE: dict[str, int] = {
    "new":        10,
    "qual":       25,
    "price_req":  35,
    "has_price":  45,
    "meeting":    55,
    "invoice":    70,
    "protected":  85,
    "contract":   95,
    "won":       100,
    "cond_lost":   5,
    "lost":        0,
}

# Быстрый lookup: id → dict (для валидации и UI).
STAGE_BY_ID: dict[str, dict] = {s["id"]: s for s in STAGES}

# Тип стадии для редактора (Сделки 2.0): успех/отказы — особые, прочие — normal.
KIND_BY_STAGE: dict[str, str] = {"won": "won", "cond_lost": "cond_lost", "lost": "lost"}

# Дефолтная воронка — для legacy-сделок без явного значения и для сидов канона.
DEFAULT_FUNNEL = "new_clients"

# Справочник воронок (полоса CRM, мульти-воронки). Расширяется через редактор/миграции.
FUNNELS: list[dict] = [
    {"code": "new_clients", "title": "Новые клиенты"},
    {"code": "repeat_clients", "title": "Постоянные клиенты"},
    {"code": "tenders", "title": "Тендеры"},
]

# Канон «Постоянные клиенты»: укороченная воронка (перезаказ, цена уже известна) +
# терминалы успех/отказ. ``cond_lost`` опускаем — для перезаказов это шумная стадия.
REPEAT_STAGES: list[tuple[str, str, int, str, str]] = [
    # (code, title, probability, kind, color)
    ("rp_request", "Запрос повтор", 30, "normal", "#3B82F6"),
    ("rp_invoice", "Счёт повтор", 70, "normal", "#0EA5E9"),
    ("rp_contract", "Договор/предоплата", 90, "normal", "#10B981"),
    ("rp_won", "Успех", 100, "won", "#22C55E"),
    ("rp_lost", "Отказ", 0, "lost", "#EF4444"),
]

# Канон «Тендеры»: воронка госзакупок/конкурсов (цвета — sales-board-mockup.html, ~1899-1910).
TENDER_STAGES: list[tuple[str, str, int, str, str]] = [
    # (code, title, probability, kind, color)
    ("tn_announced", "Объявлен", 15, "normal", "#3B82F6"),
    ("tn_submitted", "Заявка подана", 35, "normal", "#F59E0B"),
    ("tn_bidding", "Торги", 60, "normal", "#14B8A6"),
    ("tn_won", "Выигран", 100, "won", "#22C55E"),
    ("tn_lost", "Проигран", 0, "lost", "#EF4444"),
]


def canonical_stages() -> list[dict]:
    """Канон всех воронок как полные строки — сид таблицы ``sales.stage``, фолбэк редактора.

    Возвращает воронку «Новые клиенты» (11 стадий) + «Постоянные клиенты» (5 стадий) +
    «Тендеры» (5 стадий). ``sort_order`` уникален в пределах воронки (порядок колонок доски).
    """
    rows: list[dict] = [
        {
            "code": s["id"],
            "title": s["title"],
            "sort_order": i,
            "probability": PROBABILITY_BY_STAGE.get(s["id"], 0),
            "kind": KIND_BY_STAGE.get(s["id"], "normal"),
            "color": s["color"],
            "is_active": True,
            "funnel": "new_clients",
        }
        for i, s in enumerate(STAGES)
    ]
    rows += [
        {
            "code": code,
            "title": title,
            "sort_order": i,
            "probability": prob,
            "kind": kind,
            "color": color,
            "is_active": True,
            "funnel": "repeat_clients",
        }
        for i, (code, title, prob, kind, color) in enumerate(REPEAT_STAGES)
    ]
    rows += [
        {
            "code": code,
            "title": title,
            "sort_order": i,
            "probability": prob,
            "kind": kind,
            "color": color,
            "is_active": True,
            "funnel": "tenders",
        }
        for i, (code, title, prob, kind, color) in enumerate(TENDER_STAGES)
    ]
    return rows
