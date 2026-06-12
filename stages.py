"""Стадии воронки «Новые клиенты» — источник истины для доски и группировки.

Цвета — для UI; порядок в списке = порядок колонок на канбане.
"""

STAGES: list[dict] = [
    {"id": "new", "title": "Новая заявка", "color": "#3B82F6"},
    {"id": "qual", "title": "Квалификация", "color": "#8B5CF6"},
    {"id": "prop", "title": "Коммерческое предл.", "color": "#F59E0B"},
    {"id": "appr", "title": "Согласование", "color": "#14B8A6"},
    {"id": "won", "title": "Закрыто: Успешно", "color": "#22C55E"},
    {"id": "lost", "title": "Закрыто: Отказ", "color": "#EF4444"},
]

# Стадии, в которых сделка считается закрытой (исключаются из «висяков»).
TERMINAL_STAGES: frozenset[str] = frozenset({"won", "lost"})

# Дефолтная вероятность закрытия (%) по стадии (SALES-44).
PROBABILITY_BY_STAGE: dict[str, int] = {
    "new":   10,
    "qual":  30,
    "prop":  50,
    "appr":  75,
    "won":  100,
    "lost":   0,
}

# Быстрый lookup: id → dict (для валидации и UI).
STAGE_BY_ID: dict[str, dict] = {s["id"]: s for s in STAGES}
