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


def canonical_stages() -> list[dict]:
    """Канон 11 стадий как полные строки — сид таблицы ``sales.stage``, фолбэк редактора."""
    return [
        {
            "code": s["id"],
            "title": s["title"],
            "sort_order": i,
            "probability": PROBABILITY_BY_STAGE.get(s["id"], 0),
            "kind": KIND_BY_STAGE.get(s["id"], "normal"),
            "color": s["color"],
            "is_active": True,
        }
        for i, s in enumerate(STAGES)
    ]
