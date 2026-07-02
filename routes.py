"""HTTP-API модуля Sales. Монтируется ядром под префиксом ``/sales``."""
from __future__ import annotations

import os
import re
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from core.domain.models import (
    Approval,
    Contact,
    Counterparty,
    CounterpartyAlias,
    OutboxEvent,
    Sku,
)
from core.runtime.core import Core
from core.runtime.deps import get_core, get_session
from core.services.approvals import ApprovalOut, ApprovalRequest
from core.services.auth import CurrentUser, get_current_user, require_permission
from modules.sales.ai import (
    call_script_hint,
    classify_objection,
    draft_reply,
    next_step,
    objection_hint,
    qualify_lead,
    static_call_script,
    summarize,
)
from modules.sales.leads import lead_priority, route_lead, score_lead
from modules.sales.models import (
    Activity,
    ContractTemplate,
    Deal,
    DealDocument,
    DealItem,
    DealStageEvent,
    DealTask,
    KpiTarget,
    Lead,
    LossReason,
    Message,
    PlanTarget,
    PriceQuote,
    Stage,
)
from modules.sales.repository import DealRepository, record_stage
from modules.sales.schemas import (
    ActivityCreate,
    AiAssistRequest,
    AiDraftOut,
    AiTextOut,
    BoardOut,
    CallCommentIn,
    CallLinkDealIn,
    CallOut,
    CallResultIn,
    CallScriptOut,
    ChatOut,
    ContactCreate,
    ContactOut,
    ContractPrepareIn,
    ContractTemplateCreate,
    ContractTemplateOut,
    CounterpartyRef,
    DealCreate,
    DealDetailOut,
    DealHandoffOut,
    DealItemCreate,
    DealItemOut,
    DealItemUpdate,
    DealMarginOut,
    DealRead,
    DealUpdate,
    DocumentCreate,
    DocumentDecision,
    DocumentOut,
    FunnelOut,
    HandoffItem,
    KpiOut,
    LeadConvertOut,
    LeadCreate,
    LeadOut,
    LeadQualifyOut,
    LeadRouteOut,
    LoseRequest,
    LossReasonOut,
    MarginForecastOut,
    MarginLine,
    MarginReconcileOut,
    MessageCreate,
    MessageOut,
    ObjectionReplyIn,
    ObjectionReplyOut,
    PackageSentOut,
    PipelineAnalyticsOut,
    PlanDecisionIn,
    PlanTargetIn,
    PlanTargetOut,
    PriceInfo,
    PriceQuoteCreate,
    SkuOut,
    StageAnalytics,
    StageBoard,
    StageCreate,
    StageEventOut,
    StageOut,
    StageUpdate,
    TaskCreate,
    TaskOut,
    TaskUpdate,
    TelephonyEventIn,
)
from modules.sales.stages import (
    DEFAULT_FUNNEL,
    FUNNELS,
    PROBABILITY_BY_STAGE,
    STAGES,
    TERMINAL_STAGES,
    canonical_stages,
)

router = APIRouter(tags=["sales"])
# Лиды (вход воронки) — отдельный роутер. Монтируется и на /leads (фронт бьёт туда), и на
# /sales/leads (back-compat). Полный вынос в modules/leads — Шаг 2 ТЗ принятия выноса лидов.
leads_router = APIRouter(tags=["leads"])

# Префикс номера и человекочитаемое название документа по типу.
DOC_NUMBER_PREFIX = {"invoice": "СЧ", "contract": "ДГ", "order": "ЗК"}
DOC_TITLES = {"invoice": "Счёт", "contract": "Договор", "order": "Заказ"}
# Типы документов, требующие согласования до записи в 1С (договор → юрист, ч.4).
REQUIRES_APPROVAL = {"contract"}
# Типы документов, резервирующие складские остатки при проведении (счёт и заказ, SALES-51).
RESERVES_STOCK = {"invoice", "order"}
# План/факт по периодам (sales-34): окно факта (дней) и множитель плана (рабочих дней).
PERIOD_DAYS = {"day": 1, "week": 7, "month": 30, "quarter": 90, "year": 365}
PERIOD_MULT = {"day": 1, "week": 5, "month": 22, "quarter": 65, "year": 250}


def _deal_weight(deal: Deal, prob_by_stage: dict[str, int] | None = None) -> float:
    """Взвешенная сумма сделки: amount × вероятность (своя или дефолт стадии).

    ``prob_by_stage`` — карта стадия→вероятность из редактируемой таблицы стадий; нет →
    канон ``PROBABILITY_BY_STAGE`` (фолбэк до материализации таблицы).
    """
    defaults = prob_by_stage if prob_by_stage is not None else PROBABILITY_BY_STAGE
    p = deal.probability if deal.probability is not None else defaults.get(deal.stage, 0)
    return float(deal.amount) * p / 100


async def _board_stages(session: AsyncSession, funnel: str = DEFAULT_FUNNEL) -> list[dict]:
    """Активные стадии воронки ``funnel`` (порядок=колонки) из таблицы ``sales.stage``.

    Таблица — редактируемый источник истины (редактор стадий); пусто → канон ``stages.py``
    (значения идентичны сиду миграции). Пустая воронка (таблица заполнена, но в этой
    воронке стадий нет) → пустой список — UI решает, как показать пустую доску.
    """
    rows = (
        await session.execute(
            select(Stage)
            .where(Stage.is_active, Stage.funnel == funnel)
            .order_by(Stage.sort_order)
        )
    ).scalars().all()
    if rows:
        return [
            {"id": r.code, "title": r.title, "color": r.color, "probability": r.probability}
            for r in rows
        ]
    # Таблица пуста ВООБЩЕ (до материализации канона) → fallback для дефолтной воронки.
    any_row = (await session.execute(select(Stage).limit(1))).scalars().first()
    if any_row is not None:
        return []  # таблица заполнена, но конкретная воронка без стадий — honest-empty
    if funnel != DEFAULT_FUNNEL:
        return []
    return [
        {"id": s["id"], "title": s["title"], "color": s["color"],
         "probability": PROBABILITY_BY_STAGE.get(s["id"], 0)}
        for s in STAGES
    ]


def _utcnow() -> datetime:
    # наивный UTC — единообразно для SQLite и PostgreSQL
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _task_out(task: DealTask) -> TaskOut:
    """Представление задачи с вычисляемым флагом просрочки (SALES-41)."""
    overdue = task.status == "open" and task.due_at is not None and task.due_at < _utcnow()
    return TaskOut(
        id=task.id,
        deal_id=task.deal_id,
        title=task.title,
        kind=task.kind,
        assignee_id=task.assignee_id,
        due_at=task.due_at,
        status=task.status,
        result=task.result,
        overdue=overdue,
    )


async def _deal_stock_items(session: AsyncSession, deal_id: int) -> list[dict]:
    """Позиции сделки как ``[{sku_code, qty}]`` (для резервирования остатков в 1С)."""
    rows = (
        await session.execute(select(DealItem).where(DealItem.deal_id == deal_id))
    ).scalars().all()
    if not rows:
        return []
    skus = {
        s.id: s
        for s in (
            await session.execute(select(Sku).where(Sku.id.in_([r.sku_id for r in rows])))
        ).scalars().all()
    }
    return [{"sku_code": skus[r.sku_id].code, "qty": float(r.qty)} for r in rows if r.sku_id in skus]


async def _price_summary(session: AsyncSession, sku_code: str, counterparty: str = "") -> PriceInfo:
    """Сводка цен по SKU (для клиента, если задан): последняя и минимальная цена."""
    query = select(PriceQuote.price).where(PriceQuote.sku_code == sku_code)
    if counterparty:
        query = query.where(PriceQuote.counterparty == counterparty)
    prices = [
        float(p) for p in (await session.execute(query.order_by(PriceQuote.id))).scalars().all()
    ]
    if not prices:
        return PriceInfo(sku_code=sku_code)
    return PriceInfo(sku_code=sku_code, last_price=prices[-1], min_price=min(prices), count=len(prices))


async def _build_item_out(session: AsyncSession, item: DealItem, counterparty: str) -> DealItemOut:
    """Представление позиции с данными SKU и ценами клиенту (Price Engine)."""
    sku = await session.get(Sku, item.sku_id)
    price = await _price_summary(session, sku.code if sku else "", counterparty)
    return DealItemOut(
        id=item.id,
        sku_id=item.sku_id,
        code=sku.code if sku else "",
        title=sku.title if sku else "",
        unit=sku.unit if sku else "",
        qty=float(item.qty),
        last_price=price.last_price,
        min_price=price.min_price,
    )


async def _counterparty_for_deal(
    session: AsyncSession, deal: Deal, create: bool = False
) -> Counterparty | None:
    """Найти контрагента сделки по имени (связь по названию); опц. создать."""
    cp = (
        await session.execute(select(Counterparty).where(Counterparty.name == deal.counterparty))
    ).scalars().first()
    if cp is None and create:
        cp = Counterparty(name=deal.counterparty)
        session.add(cp)
        await session.flush()
    return cp


async def _counterparty_ref(session: AsyncSession, deal: Deal) -> CounterpartyRef | None:
    """Резолв контрагента сделки в MDM для карточки (id/УНП/источники). None — нет в витрине."""
    cp = await _counterparty_for_deal(session, deal)
    if cp is None:
        return None
    sources = (
        await session.execute(
            select(CounterpartyAlias.source).where(CounterpartyAlias.counterparty_id == cp.id)
        )
    ).scalars().all()
    return CounterpartyRef(
        id=cp.id,
        name=cp.name,
        unp=cp.unp,
        sources=sorted(set(sources)),
        is_active=cp.is_active,
        merged_into_id=cp.merged_into_id,
    )


async def _clear_primary(session: AsyncSession, counterparty_id: int) -> None:
    """Снять признак основного со всех контактов контрагента."""
    rows = (
        await session.execute(
            select(Contact).where(Contact.counterparty_id == counterparty_id, Contact.is_primary)
        )
    ).scalars().all()
    for contact in rows:
        contact.is_primary = False


async def _post_document_to_1c(
    core: Core, session: AsyncSession, doc: DealDocument, counterparty: str
) -> None:
    """Записать документ в 1С через фасад ядра и пометить проведённым (posted).

    Событие ``sales.document.posted`` уходит в шину (→ audit). Используется и при
    мгновенной записи счёта, и при проведении договора после согласования.
    """
    result = await core.services.onec.post_document(
        doc.kind,
        {"number": doc.number, "counterparty": counterparty, "amount": float(doc.amount)},
    )
    doc.onec_ref = result.get("ref")
    doc.status = "posted"
    doc.posted_at = _utcnow()
    core.event_bus.emit(
        session,
        "sales.document.posted",
        {
            "document_id": doc.id,
            "deal_id": doc.deal_id,
            "kind": doc.kind,
            "number": doc.number,
            "onec_ref": doc.onec_ref,
            "counterparty": counterparty,
            "amount": float(doc.amount),
            "entity_ref": f"deal:{doc.deal_id}",
        },
    )


@router.get("/ping")
async def ping() -> dict:
    """Проверка, что модуль смонтирован."""
    return {"module": "sales", "status": "ok"}


async def _supply_arrivals(
    session: AsyncSession, deal_ids: list[int], window_days: int = 7
) -> dict[int, dict]:
    """Бейдж «🚚 под приход» (П6 UI ТЗ) — читаем живьём из аудита событий
    ``sales.supply.arrived`` (эмитит ``on_procurement_received``, БЕЗ новой колонки/миграции —
    паттерн как в ``_audit_landed_unit_by_sku``). Берём события за последние ``window_days``
    (иначе бейдж висел бы вечно); на сделку — самое свежее.
    """
    if not deal_ids:
        return {}
    idset = set(deal_ids)
    cutoff = _utcnow() - timedelta(days=window_days)
    events = (
        await session.execute(
            select(OutboxEvent)
            .where(OutboxEvent.event_type == "sales.supply.arrived")
            .where(OutboxEvent.created_at >= cutoff)
            .order_by(OutboxEvent.id)
        )
    ).scalars().all()
    out: dict[int, dict] = {}
    for ev in events:  # id по возрастанию → самое свежее для сделки побеждает
        payload = ev.payload or {}
        sku = payload.get("sku_code")
        for deal_id in payload.get("deal_ids") or []:
            if deal_id in idset:
                out[deal_id] = {"supply_arrived_at": ev.created_at, "supply_arrived_sku": sku}
    return out


@router.get("/board", response_model=BoardOut)
async def board(
    owner: str = "",
    funnel: str = DEFAULT_FUNNEL,
    session: AsyncSession = Depends(get_session),
) -> BoardOut:
    """Доска сделок воронки ``funnel``: сделки по стадиям с агрегатами. ``owner`` —
    фильтр по ответственному (видимость «по менеджеру», SALES-42). Сделки фильтруются
    по ``Deal.funnel == funnel`` (дефолт ``new_clients``); колонки — стадии этой воронки.
    """
    deals = await DealRepository(session).list()
    deals = [d for d in deals if d.funnel == funnel]
    if owner:
        deals = [d for d in deals if d.owner == owner]
    by_stage: dict[str, list[Deal]] = defaultdict(list)
    for deal in deals:
        by_stage[deal.stage].append(deal)

    board_stages = await _board_stages(session, funnel)
    prob_by_stage = {s["id"]: s["probability"] for s in board_stages}
    arrivals = await _supply_arrivals(session, [d.id for d in deals])
    stages = [
        StageBoard(
            id=s["id"],
            title=s["title"],
            color=s["color"],
            count=len(by_stage.get(s["id"], [])),
            sum=float(sum(d.amount for d in by_stage.get(s["id"], []))),
            weighted=float(sum(_deal_weight(d, prob_by_stage) for d in by_stage.get(s["id"], []))),
            deals=[
                DealRead.model_validate(d).model_copy(update=arrivals.get(d.id, {}))
                for d in by_stage.get(s["id"], [])
            ],
        )
        for s in board_stages
    ]
    return BoardOut(stages=stages)


@router.get("/pipeline/analytics", response_model=PipelineAnalyticsOut)
async def pipeline_analytics(
    funnel: str = DEFAULT_FUNNEL,
    owner: str = "",
    session: AsyncSession = Depends(get_session),
    _: CurrentUser = Depends(require_permission("sales.deal.read")),
):
    """Pipeline-аналитика воронки (П6 ТЗ): по каждой стадии — count/sum/weighted/avg_age/
    conv→следующая; по воронке — взвеш.прогноз + средняя длина цикла won-сделок.

    Конверсия стадия→next: доля сделок, чья история имеет переход (from=stage, to=next_stage)
    среди тех, кто хотя бы был в этой стадии. honest-empty: пусто, если истории нет.
    """
    stage_rows = await _board_stages(session, funnel)
    deals = await DealRepository(session).list()
    deals = [d for d in deals if d.funnel == funnel]
    if owner:
        deals = [d for d in deals if d.owner == owner]
    prob_by_stage = {s["id"]: s["probability"] for s in stage_rows}
    by_stage: dict[str, list[Deal]] = defaultdict(list)
    for d in deals:
        by_stage[d.stage].append(d)

    # История стадий: события для всех сделок этой воронки.
    deal_ids = [d.id for d in deals]
    events: list[DealStageEvent] = []
    if deal_ids:
        events = (
            await session.execute(
                select(DealStageEvent).where(DealStageEvent.deal_id.in_(deal_ids))
            )
        ).scalars().all()
    # Кто вообще был в стадии (был as `to_stage` хоть раз) и кто ушёл из неё (был as
    # `from_stage` хоть раз, переход в неконечную/следующую стадию).
    been_in: dict[str, set[int]] = defaultdict(set)
    moved_to_next: dict[tuple[str, str], set[int]] = defaultdict(set)
    for ev in events:
        if ev.to_stage:
            been_in[ev.to_stage].add(ev.deal_id)
        if ev.from_stage and ev.to_stage:
            moved_to_next[(ev.from_stage, ev.to_stage)].add(ev.deal_id)
    # Текущие сделки тоже учитываем как «были в стадии» (на случай создания без события).
    for d in deals:
        been_in[d.stage].add(d.id)

    stage_codes = [s["id"] for s in stage_rows]
    now = _utcnow()
    out_stages: list[StageAnalytics] = []
    for idx, s in enumerate(stage_rows):
        sid = s["id"]
        bucket = by_stage.get(sid, [])
        # средний возраст в стадии — из stage_changed_at
        ages = [
            (now - d.stage_changed_at).total_seconds() / 86400.0
            for d in bucket
            if d.stage_changed_at is not None
        ]
        avg_age = round(sum(ages) / len(ages), 1) if ages else None

        next_sid = stage_codes[idx + 1] if idx + 1 < len(stage_codes) else None
        conv: int | None = None
        if next_sid is not None:
            denom = len(been_in.get(sid, set()))
            if denom > 0:
                gone_next = len(moved_to_next.get((sid, next_sid), set()))
                conv = round(gone_next / denom * 100)

        out_stages.append(
            StageAnalytics(
                id=sid,
                title=s["title"],
                color=s["color"],
                count=len(bucket),
                sum=float(sum(d.amount for d in bucket)),
                weighted=float(sum(_deal_weight(d, prob_by_stage) for d in bucket)),
                avg_age_days=avg_age,
                next_conv_pct=conv,
            )
        )

    # Сводно: взвеш. прогноз = сумма по всем нетерминальным стадиям; средняя длина цикла
    # won-сделок (created_at → closed_date/stage_changed_at).
    forecast = sum(
        s.weighted for s in out_stages if s.id not in TERMINAL_STAGES and s.id != "cond_lost"
    )
    won_deals = [d for d in deals if d.stage == "won"]
    cycles: list[float] = []
    for d in won_deals:
        end = d.stage_changed_at  # переход в won зафиксирован тут
        if d.created_at is not None and end is not None:
            cycles.append((end - d.created_at).total_seconds() / 86400.0)
    avg_cycle = round(sum(cycles) / len(cycles), 1) if cycles else None

    return PipelineAnalyticsOut(
        funnel=funnel,
        stages=out_stages,
        forecast_weighted=float(forecast),
        avg_cycle_days=avg_cycle,
        won_count=len(won_deals),
    )


# ── Редактор стадий воронки (Сделки 2.0): CRUD справочника sales.stage ─────────────
@router.get("/funnels", response_model=list[FunnelOut])
async def list_funnels(
    session: AsyncSession = Depends(get_session),
    _: CurrentUser = Depends(require_permission("sales.deal.read")),
):
    """Воронки sales: код + титул + сколько активных сделок. Имена — из ``FUNNELS``
    (справочник в коде), порядок — как там; неизвестные коды (созданы через редактор
    стадий, но не описаны в справочнике) добавляются в конец с code как title.

    Graceful fallback: если колонки ``Deal.funnel``/``Stage.funnel`` нет (старая dev.db
    создана до миграции 0062), считаем все сделки относящимися к дефолтной воронке
    и возвращаем только ``FUNNELS``-справочник — без падения 500 и без ремонта схемы.
    """
    from sqlalchemy.exc import OperationalError, ProgrammingError

    # код → активных сделок (исключаем терминальные стадии)
    try:
        stage_counts = (
            await session.execute(
                select(Deal.funnel, func.count())
                .where(Deal.stage.notin_(TERMINAL_STAGES))
                .group_by(Deal.funnel)
            )
        ).all()
    except (OperationalError, ProgrammingError):
        # старый dev.db без колонки funnel — отдаём только справочник, без счётчиков
        await session.rollback()
        return [FunnelOut(code=f["code"], title=f["title"], active_deals=0) for f in FUNNELS]
    counts = {code: n for code, n in stage_counts}
    seen: set[str] = set()
    rows: list[FunnelOut] = []
    for f in FUNNELS:
        rows.append(FunnelOut(code=f["code"], title=f["title"], active_deals=counts.get(f["code"], 0)))
        seen.add(f["code"])
    # Воронки из таблицы stage, не описанные в FUNNELS — показываем как есть.
    try:
        extras = (
            await session.execute(select(Stage.funnel).distinct())
        ).scalars().all()
    except (OperationalError, ProgrammingError):
        await session.rollback()
        extras = []
    for code in extras:
        if code not in seen:
            rows.append(FunnelOut(code=code, title=code, active_deals=counts.get(code, 0)))
    return rows


@router.get("/stages", response_model=list[StageOut])
async def list_stages(
    funnel: str | None = None,
    session: AsyncSession = Depends(get_session),
    _: CurrentUser = Depends(require_permission("sales.deal.read")),
):
    """Стадии воронки для доски/редактора. Без ``funnel`` — все стадии всех воронок.
    Первый вызов лениво материализует канон (``stages.py``) в таблицу — дальше источник
    истины редактируемый."""
    stmt = select(Stage).order_by(Stage.funnel, Stage.sort_order)
    rows = (await session.execute(stmt)).scalars().all()
    if not rows:
        session.add_all([Stage(**row) for row in canonical_stages()])
        try:
            await session.commit()
        except IntegrityError:  # гонка параллельного первого GET — сид уже сделан рядом
            await session.rollback()
        rows = (await session.execute(stmt)).scalars().all()
    if funnel is not None:
        rows = [r for r in rows if r.funnel == funnel]
    return rows


@router.post("/stages", response_model=StageOut, status_code=201)
async def create_stage(
    payload: StageCreate,
    session: AsyncSession = Depends(get_session),
    _: CurrentUser = Depends(require_permission("sales.deal.write")),
):
    """Добавить стадию воронки (редактор стадий)."""
    exists = (
        await session.execute(select(Stage).where(Stage.code == payload.code))
    ).scalars().first()
    if exists is not None:
        raise HTTPException(status_code=409, detail="Стадия с таким кодом уже есть")
    stage = Stage(**payload.model_dump())
    session.add(stage)
    await session.commit()
    return stage


@router.patch("/stages/{code}", response_model=StageOut)
async def update_stage(
    code: str,
    payload: StageUpdate,
    session: AsyncSession = Depends(get_session),
    _: CurrentUser = Depends(require_permission("sales.deal.write")),
):
    """Изменить стадию (имя/порядок/вероятность/тип/цвет/активность)."""
    stage = (
        await session.execute(select(Stage).where(Stage.code == code))
    ).scalars().first()
    if stage is None:
        raise HTTPException(status_code=404, detail="Стадия не найдена")
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(stage, field, value)
    await session.commit()
    return stage


@router.delete("/stages/{code}", status_code=204)
async def delete_stage(
    code: str,
    session: AsyncSession = Depends(get_session),
    _: CurrentUser = Depends(require_permission("sales.deal.write")),
):
    """Удалить стадию. 409, если в стадии есть сделки (целостность ``Deal.stage``)."""
    stage = (
        await session.execute(select(Stage).where(Stage.code == code))
    ).scalars().first()
    if stage is None:
        raise HTTPException(status_code=404, detail="Стадия не найдена")
    in_use = (
        await session.execute(select(func.count()).select_from(Deal).where(Deal.stage == code))
    ).scalar()
    if in_use:
        raise HTTPException(
            status_code=409,
            detail=f"В стадии есть сделки ({in_use}) — перенесите их или деактивируйте стадию",
        )
    await session.delete(stage)
    await session.commit()


@router.get("/kpis", response_model=list[KpiOut])
async def kpis(period: str = "day", session: AsyncSession = Depends(get_session)):
    """Показатели «План/Факт» за период (день/неделя/месяц/квартал/год, sales-34).

    Факт — сумма активностей за окно периода (от последней даты назад); план —
    дневная цель, масштабированная на число рабочих дней периода.
    """
    targets = (
        await session.execute(select(KpiTarget).order_by(KpiTarget.sort_order))
    ).scalars().all()

    latest = (await session.execute(select(func.max(Activity.date)))).scalar()
    actuals: dict[str, float] = {}
    if latest is not None:
        start = latest - timedelta(days=PERIOD_DAYS.get(period, 1) - 1)
        rows = await session.execute(
            select(Activity.kpi_key, func.coalesce(func.sum(Activity.value), 0))
            .where(Activity.date >= start, Activity.date <= latest)
            .group_by(Activity.kpi_key)
        )
        actuals = {key: float(total) for key, total in rows.all()}

    mult = PERIOD_MULT.get(period, 1)
    result: list[KpiOut] = []
    for t in targets:
        actual = actuals.get(t.key, 0.0)
        target = float(t.target) * mult
        percent = round(min(100.0, actual / target * 100)) if target else 0
        result.append(
            KpiOut(
                key=t.key,
                title=t.title,
                target=target,
                actual=actual,
                percent=percent,
                unit=t.unit,
                icon=t.icon,
                tone=t.tone,
            )
        )
    return result


@router.post("/activities", status_code=201)
async def create_activity(payload: ActivityCreate, session: AsyncSession = Depends(get_session)):
    """Отметить активность. Без даты — добавляется в текущий отчётный день."""
    day = payload.date
    if day is None:
        day = (await session.execute(select(func.max(Activity.date)))).scalar() or date.today()
    session.add(
        Activity(
            kpi_key=payload.kpi_key,
            owner=payload.owner,
            value=Decimal(str(payload.value)),
            date=day,
        )
    )
    await session.commit()
    return {"ok": True, "date": str(day)}


@router.get("/deals", response_model=list[DealRead])
async def list_deals(
    stuck_days: int = 0,
    has_open_task: bool | None = None,
    session: AsyncSession = Depends(get_session),
):
    """Плоский список сделок. ``stuck_days>0`` — только «висяки» (SALES-43): открытые
    сделки без смены стадии дольше N дней. ``has_open_task=false`` — открытые сделки
    без единой открытой задачи (отчёт «брошенные», SALES-41)."""
    if stuck_days > 0:
        cutoff = _utcnow() - timedelta(days=stuck_days)
        return (
            await session.execute(
                select(Deal)
                .where(Deal.stage.notin_(TERMINAL_STAGES), Deal.stage_changed_at < cutoff)
                .order_by(Deal.id)
            )
        ).scalars().all()
    if has_open_task is False:
        open_deal_ids = (
            select(DealTask.deal_id).where(DealTask.status == "open").distinct()
        )
        return (
            await session.execute(
                select(Deal)
                .where(Deal.stage.notin_(TERMINAL_STAGES), Deal.id.notin_(open_deal_ids))
                .order_by(Deal.id)
            )
        ).scalars().all()
    return await DealRepository(session).list()


@router.get("/deals/{deal_id}", response_model=DealDetailOut)
async def get_deal(deal_id: int, session: AsyncSession = Depends(get_session)):
    """Одна сделка по id с позициями номенклатуры (со связью к SKU)."""
    deal = await DealRepository(session).get(deal_id)
    if deal is None:
        raise HTTPException(status_code=404, detail="Сделка не найдена")

    rows = (
        await session.execute(select(DealItem).where(DealItem.deal_id == deal_id))
    ).scalars().all()
    skus: dict[int, Sku] = {}
    if rows:
        sku_ids = [r.sku_id for r in rows]
        skus = {
            s.id: s
            for s in (
                await session.execute(select(Sku).where(Sku.id.in_(sku_ids)))
            ).scalars().all()
        }
    # цены клиенту по позициям (Price Engine): последняя и минимальная
    price_map: dict[str, tuple[float, float]] = {}
    codes = [skus[r.sku_id].code for r in rows if r.sku_id in skus]
    if codes:
        quotes = (
            await session.execute(
                select(PriceQuote)
                .where(
                    PriceQuote.counterparty == deal.counterparty,
                    PriceQuote.sku_code.in_(codes),
                )
                .order_by(PriceQuote.id)
            )
        ).scalars().all()
        grouped: dict[str, list[float]] = defaultdict(list)
        for q in quotes:
            grouped[q.sku_code].append(float(q.price))
        price_map = {c: (v[-1], min(v)) for c, v in grouped.items()}

    items = [
        DealItemOut(
            id=r.id,
            sku_id=r.sku_id,
            code=skus[r.sku_id].code if r.sku_id in skus else "",
            title=skus[r.sku_id].title if r.sku_id in skus else "",
            unit=skus[r.sku_id].unit if r.sku_id in skus else "",
            qty=float(r.qty),
            last_price=price_map.get(skus[r.sku_id].code, (None, None))[0]
            if r.sku_id in skus
            else None,
            min_price=price_map.get(skus[r.sku_id].code, (None, None))[1]
            if r.sku_id in skus
            else None,
        )
        for r in rows
    ]

    docs = (
        await session.execute(
            select(DealDocument).where(DealDocument.deal_id == deal_id).order_by(DealDocument.id)
        )
    ).scalars().all()
    documents = [DocumentOut.model_validate(d) for d in docs]

    return DealDetailOut(
        **DealRead.model_validate(deal).model_dump(),
        items=items,
        documents=documents,
        counterparty_ref=await _counterparty_ref(session, deal),
    )


async def _emit_ship_deadline(session: AsyncSession, core: Core, deal: Deal) -> None:
    """Сигнал в закупки о крайней дате отгрузки сделки (``sales.deal.ship_deadline.set``).

    Несёт дату + сводку штрафа за опоздание + позиции (sku/qty) — чтобы закупки видели риск
    срыва и что закупать к сроку. Новое ребро sales→procurement (потребитель подключится позже).
    """
    items_rows = (
        await session.execute(select(DealItem).where(DealItem.deal_id == deal.id))
    ).scalars().all()
    sku_ids = [r.sku_id for r in items_rows]
    sku_map: dict[int, Sku] = {}
    if sku_ids:
        sku_map = {
            s.id: s for s in (
                await session.execute(select(Sku).where(Sku.id.in_(sku_ids)))
            ).scalars().all()
        }
    items = [
        {
            "sku_code": sku_map[r.sku_id].code if r.sku_id in sku_map else "",
            "title": sku_map[r.sku_id].title if r.sku_id in sku_map else "",
            "qty": float(r.qty),
        }
        for r in items_rows
    ]
    core.event_bus.emit(
        session,
        "sales.deal.ship_deadline.set",
        {
            "deal_id": deal.id,
            "number": deal.number,
            "counterparty": deal.counterparty,
            "ship_deadline": deal.ship_deadline,
            "penalty_rate_pct": (
                float(deal.penalty_rate_pct) if deal.penalty_rate_pct is not None else None
            ),
            "penalty_cap_pct": (
                float(deal.penalty_cap_pct) if deal.penalty_cap_pct is not None else None
            ),
            "penalty_terms": deal.penalty_terms,
            "items": items,
            "actor": "sales",
            "entity_ref": f"deal:{deal.id}",
        },
    )


@router.patch("/deals/{deal_id}", response_model=DealRead)
async def update_deal(
    deal_id: int,
    payload: DealUpdate,
    core: Core = Depends(get_core),
    session: AsyncSession = Depends(get_session),
    user: CurrentUser = Depends(get_current_user),
):
    """Частично обновить сделку. Смена стадии (drag&drop) пишется в историю и
    обновляет ``stage_changed_at`` через единый хелпер ``record_stage`` (SALES-43)."""
    repo = DealRepository(session)
    deal = await repo.get(deal_id)
    if deal is None:
        raise HTTPException(status_code=404, detail="Сделка не найдена")
    data = payload.model_dump(exclude_unset=True)
    new_stage = data.pop("stage", None)
    new_funnel = data.pop("funnel", None)
    # R5-3: нельзя двинуть сделку в стадию ЧУЖОЙ воронки — иначе сделка выпадает с обеих досок
    # (funnel=new_clients + stage=rp_won не существует ни в одной колонке). Валидируем против
    # стадий целевой воронки (новой, если меняем; иначе текущей).
    target_funnel = new_funnel if new_funnel is not None else deal.funnel
    if new_stage is not None and new_stage != deal.stage:
        valid_codes = {
            r.code for r in (
                await session.execute(
                    select(Stage).where(Stage.funnel == target_funnel, Stage.is_active)
                )
            ).scalars().all()
        }
        if not valid_codes:  # таблица стадий не материализована → канон
            valid_codes = {s["code"] for s in canonical_stages() if s["funnel"] == target_funnel}
        if new_stage not in valid_codes:
            raise HTTPException(
                status_code=422,
                detail=f"Стадия {new_stage!r} не принадлежит воронке {target_funnel!r}",
            )
    # Смена воронки фиксируется в истории как смена стадии (источник → стадия первой стадии
    # новой воронки), чтобы фронт-таймлайн не терял этот шаг; реальный новый стадия-код может
    # прилететь следующим PATCH (drag&drop на доске уже другой воронки).
    if new_funnel is not None and new_funnel != deal.funnel:
        from_stage = deal.stage
        deal.funnel = new_funnel
        record_stage(session, deal, deal.stage, by=user.username)
        # ponytail: stage остаётся прежним кодом; если код несуществует в новой воронке,
        # доска покажет сделку «вне колонок» — UI должен сменить стадию следующим действием.
        deal.next_step = f"Воронка: {from_stage} → {new_funnel}"
    if new_stage is not None and new_stage != deal.stage:
        record_stage(session, deal, new_stage, by=user.username)
    old_deadline = deal.ship_deadline
    await repo.update(deal, data)
    # Крайняя дата отгрузки выставлена/изменена → сигнал в закупки (ребро sales→procurement).
    if "ship_deadline" in data and deal.ship_deadline and deal.ship_deadline != old_deadline:
        await _emit_ship_deadline(session, core, deal)
    await session.commit()
    return deal


async def _deal_margin(
    session: AsyncSession, core: Core, deal: Deal
) -> tuple[list[MarginLine], bool]:
    """Маржа позиций сделки: список ``MarginLine`` + признак отсутствия landed-фасада.

    Единый расчёт для карточки (``GET /deals/{id}/margin``) и прогноза воронки
    (``GET /pipeline/margin-forecast``) — DRY, обе считают одинаково. Каждая строка несёт
    цену клиенту (``revenue = price×qty`` при наличии котировки, НЕ зависит от landed) и
    landed-себес (``cogs`` при возврате партии фасадом). Агрегаты считает вызывающий:
    карточка — по ``priced``-позициям, прогноз — выручку по цене, прибыль по ``priced``.
    Пустой список = у сделки нет позиций.
    """
    rows = (
        await session.execute(select(DealItem).where(DealItem.deal_id == deal.id))
    ).scalars().all()
    facade_missing = getattr(core.services, "landed_cost", None) is None
    if not rows:
        return [], facade_missing

    skus = {
        s.id: s for s in (
            await session.execute(select(Sku).where(Sku.id.in_([r.sku_id for r in rows])))
        ).scalars().all()
    }
    codes = sorted({skus[r.sku_id].code for r in rows if r.sku_id in skus})

    # Последняя цена клиенту по (sku_code, counterparty) — как ``_price_summary``.
    last_price: dict[str, float] = {}
    if codes:
        quotes = (
            await session.execute(
                select(PriceQuote)
                .where(PriceQuote.counterparty == deal.counterparty, PriceQuote.sku_code.in_(codes))
                .order_by(PriceQuote.id)
            )
        ).scalars().all()
        for q in quotes:
            last_price[q.sku_code] = float(q.price)  # перезаписываем — побеждает последняя

    # Landed себестоимость через фасад ядра (None → procurement не подключён → честная деградация).
    landed_facade = getattr(core.services, "landed_cost", None)
    landed_map: dict[str, dict | None] = {}
    if not facade_missing and codes:
        landed_map = await landed_facade.last_landed_cost_batch(session, codes)

    lines: list[MarginLine] = []
    for r in rows:
        sku = skus.get(r.sku_id)
        code = sku.code if sku else ""
        title = sku.title if sku else ""
        qty = float(r.qty)
        price = last_price.get(code)
        cost_row = landed_map.get(code) if not facade_missing else None
        unit_cost = float(cost_row["unit_landed_cost_byn"]) if cost_row else None
        lines.append(
            MarginLine(
                sku_code=code, title=title, qty=qty,
                unit_price=price,
                revenue=price * qty if price is not None else None,
                unit_landed_cost=unit_cost,
                cogs=unit_cost * qty if unit_cost is not None else None,
                margin_pct=(
                    round((price - unit_cost) / price * 100)
                    if price and unit_cost is not None and price > 0 else None
                ),
                status=(
                    "priced" if price is not None and unit_cost is not None
                    else ("no_cost" if price is not None else "no_price")
                ),
                cost_shipment_id=cost_row.get("shipment_id") if cost_row else None,
                cost_fixed_at=cost_row.get("fixed_at") if cost_row else None,
                cost_fx_rate=(
                    float(cost_row["fx_rate"])
                    if cost_row and cost_row.get("fx_rate") is not None else None
                ),
            )
        )
    return lines, facade_missing


async def _audit_landed_unit_by_sku(
    session: AsyncSession, codes: list[str]
) -> dict[str, Decimal]:
    """Актуальная landed-себестоимость по ``sku_code`` из аудита событий
    ``procurement.landed_cost.calculated`` (через outbox/шину, БЕЗ импорта procurement/finance).

    Берём ПОСЛЕДНЕЕ событие на sku_code (по ``id`` — позже зафиксированный ``actual`` бьёт
    ранний ``estimated``). Возвращаем ``{sku_code: unit_landed_cost_byn}``; пусто = нет фактов.

    ponytail: full-scan по event_type без БД-фильтра по sku и без LIMIT — приемлемо для dev/MVP;
    при росте append-only outbox_event сузить (JSON-фильтр payload->>'sku_code' IN codes на PG /
    последнее событие на sku через подзапрос). На event_type индекса пока нет.
    """
    if not codes:
        return {}
    codeset = set(codes)
    events = (
        await session.execute(
            select(OutboxEvent)
            .where(OutboxEvent.event_type == "procurement.landed_cost.calculated")
            .order_by(OutboxEvent.id)
        )
    ).scalars().all()
    out: dict[str, Decimal] = {}
    for ev in events:  # id по возрастанию → последнее (actual) побеждает estimated
        payload = ev.payload or {}
        sku = payload.get("sku_code")
        if sku not in codeset:
            continue
        val = payload.get("unit_landed_cost_byn")
        if val is None:
            continue
        try:
            out[sku] = Decimal(str(val))
        except (InvalidOperation, ValueError):
            continue
    return out


@router.get("/deals/{deal_id}/margin", response_model=DealMarginOut)
async def deal_margin(
    deal_id: int,
    core: Core = Depends(get_core),
    session: AsyncSession = Depends(get_session),
    _: CurrentUser = Depends(require_permission("sales.deal.read")),
):
    """Факт-маржа сделки: цена из ``PriceQuote`` × qty минус landed × qty (по позициям).

    Цена — последняя котировка клиенту (``PriceQuote(sku_code, counterparty)``); себес —
    через фасад ``core.services.landed_cost.last_landed_cost_batch`` (модуль procurement,
    результат закрытой партии). Деградация honest: фасад ``None`` → ``cogs_landed=None`` +
    причина; позиции без цены/себеса в gross НЕ попадают (``no_price``/``no_cost``).
    Методику установки цены НЕ изобретаем — отдаём ФАКТ-маржу где данные уже есть.
    """
    deal = await DealRepository(session).get(deal_id)
    if deal is None:
        raise HTTPException(status_code=404, detail="Сделка не найдена")

    lines, facade_missing = await _deal_margin(session, core, deal)
    if not lines:
        return DealMarginOut(
            deal_id=deal_id, revenue=0.0, cogs_landed=0.0, gross_profit=0.0,
            margin_pct=None, priced_count=0, total_count=0,
            reason="Позиций нет — маржа не рассчитывается",
        )

    revenue = sum((ln.revenue or 0.0) for ln in lines if ln.status == "priced")
    cogs = sum((ln.cogs or 0.0) for ln in lines if ln.status == "priced")
    priced = sum(1 for ln in lines if ln.status == "priced")
    total = len(lines)

    if facade_missing:
        return DealMarginOut(
            deal_id=deal_id, revenue=revenue, cogs_landed=None, gross_profit=None,
            margin_pct=None, priced_count=priced, total_count=total,
            reason="Себестоимость закупок не подключена (procurement не реализовал фасад landed_cost)",
            lines=lines,
        )
    if priced == 0:
        # R5-4: фасад есть, но ни одна позиция не оценена → маржа НЕИЗВЕСТНА (None), а не 0.
        # 0 ≠ «неизвестно»: продавец не должен принять «нулевую маржу» вместо «нет данных».
        return DealMarginOut(
            deal_id=deal_id, revenue=revenue, cogs_landed=None, gross_profit=None,
            margin_pct=None, priced_count=0, total_count=total,
            reason="Ни по одной позиции нет одновременно цены клиенту и landed cost",
            lines=lines,
        )
    gross = revenue - cogs
    margin_pct = round(gross / revenue * 100) if revenue > 0 else None
    return DealMarginOut(
        deal_id=deal_id, revenue=revenue, cogs_landed=cogs, gross_profit=gross,
        margin_pct=margin_pct, priced_count=priced, total_count=total, lines=lines,
    )


@router.get("/pipeline/margin-forecast", response_model=MarginForecastOut)
async def pipeline_margin_forecast(
    funnel: str = DEFAULT_FUNNEL,
    owner: str = "",
    core: Core = Depends(get_core),
    session: AsyncSession = Depends(get_session),
    _: CurrentUser = Depends(require_permission("sales.deal.read")),
):
    """Взвешенный прогноз ВАЛОВОЙ МАРЖИ воронки (S3-1) — маржа из карточки на уровень воронки.

    По активным (нетерминальным) сделкам считаем factual-маржу тем же путём, что
    ``/deals/{id}/margin`` (общий хелпер ``_deal_margin``), и взвешиваем на вероятность стадии:
    ``revenue_weighted`` — по позициям с ценой клиенту (не зависит от landed, всегда число),
    ``gross_weighted`` — по ``priced``-позициям (есть и цена, и landed). Нет фасада landed_cost
    → ``gross_weighted=null`` + причина (честная деградация, НЕ 0), выручка остаётся числом.

    ponytail: O(сделок) вызовов фасада (по сделке) — допустимо для десятков активных сделок;
    батч-расчёт по всей воронке за один проход — если вырастет.
    """
    stage_rows = await _board_stages(session, funnel)
    prob_by_stage = {s["id"]: s["probability"] for s in stage_rows}

    deals = await DealRepository(session).list()
    deals = [
        d for d in deals
        if d.funnel == funnel and d.stage not in TERMINAL_STAGES and d.stage != "cond_lost"
    ]
    if owner:
        deals = [d for d in deals if d.owner == owner]

    facade_missing = getattr(core.services, "landed_cost", None) is None
    revenue_weighted = 0.0
    gross_weighted: float | None = None if facade_missing else 0.0
    deals_priced = 0
    for d in deals:
        lines, _fm = await _deal_margin(session, core, d)
        prob = d.probability if d.probability is not None else prob_by_stage.get(d.stage, 0)
        w = prob / 100
        revenue_weighted += sum((ln.revenue or 0.0) for ln in lines if ln.revenue is not None) * w
        if not facade_missing and any(ln.status == "priced" for ln in lines):
            deal_gross = sum(
                (ln.revenue or 0.0) - (ln.cogs or 0.0) for ln in lines if ln.status == "priced"
            )
            gross_weighted = (gross_weighted or 0.0) + deal_gross * w
            deals_priced += 1

    # R5-4: фасад есть, но ни одна активная сделка не оценена → вал.прибыль НЕИЗВЕСТНА (null), не 0.
    if not facade_missing and deals_priced == 0:
        gross_weighted = None
    margin_pct_blended: int | None = None
    if gross_weighted is not None and revenue_weighted > 0:
        margin_pct_blended = round(gross_weighted / revenue_weighted * 100)

    reason: str | None = None
    if facade_missing:
        reason = "Себестоимость закупок не подключена (procurement не реализовал фасад landed_cost)"
    elif deals_priced == 0:
        reason = "Ни по одной активной сделке нет одновременно цены клиенту и landed cost"

    return MarginForecastOut(
        funnel=funnel,
        owner=owner or None,
        revenue_weighted=round(revenue_weighted, 2),
        gross_weighted=round(gross_weighted, 2) if gross_weighted is not None else None,
        margin_pct_blended=margin_pct_blended,
        deals_priced=deals_priced,
        deals_total=len(deals),
        reason=reason,
    )


@router.get("/deals/{deal_id}/margin/reconcile", response_model=MarginReconcileOut)
async def deal_margin_reconcile(
    deal_id: int,
    core: Core = Depends(get_core),
    session: AsyncSession = Depends(get_session),
    _: CurrentUser = Depends(require_permission("sales.deal.read")),
):
    """Сверка прогнозной маржи sales с фактической себестоимостью из аудита шины (S3-4, ось A).

    Уровень — sku/агрегат сделки: ``procurement.landed_cost.calculated`` НЕ несёт deal_id
    (PO обслуживает много сделок), поэтому сверяем по ``sku_code`` позиций. ``sales_forecast_gross``
    — наш расчёт (landed snapshot фасада, как карточка); ``finance_actual_gross`` — та же выручка
    минус landed из аудита событий (БЕЗ импорта finance/procurement). Нет landed-событий по
    позициям → ``no_finance`` (никогда не 500). ``delta`` = sales − finance.
    """
    deal = await DealRepository(session).get(deal_id)
    if deal is None:
        raise HTTPException(status_code=404, detail="Сделка не найдена")

    lines, facade_missing = await _deal_margin(session, core, deal)
    priced_lines = [ln for ln in lines if ln.status == "priced"]
    sales_forecast_gross: float | None = None
    if not facade_missing and priced_lines:
        sales_forecast_gross = round(
            sum((ln.revenue or 0.0) - (ln.cogs or 0.0) for ln in priced_lines), 2
        )

    # Факт себестоимости из аудита шины по тем же sku (агрегат, не по сделке).
    audit_unit = await _audit_landed_unit_by_sku(
        session, sorted({ln.sku_code for ln in priced_lines if ln.sku_code})
    )
    finance_actual_gross: float | None = None
    if audit_unit:
        total = 0.0
        matched = False
        for ln in priced_lines:
            unit_actual = audit_unit.get(ln.sku_code)
            if unit_actual is not None and ln.unit_price is not None:
                matched = True
                total += (ln.unit_price - float(unit_actual)) * ln.qty
        if matched:
            finance_actual_gross = round(total, 2)

    delta: float | None = None
    if sales_forecast_gross is not None and finance_actual_gross is not None:
        delta = round(sales_forecast_gross - finance_actual_gross, 2)
    if finance_actual_gross is None:
        status = "no_finance"
    elif delta is None:
        status = "diverged"  # факт есть, но sales-сторона недоступна (нет фасада/priced)
    else:
        status = "converged" if abs(delta) < 0.01 else "diverged"

    return MarginReconcileOut(
        deal_id=deal_id,
        sales_forecast_gross=sales_forecast_gross,
        finance_actual_gross=finance_actual_gross,
        delta=delta,
        level="sku_aggregate",
        status=status,
    )


@router.get("/deals/{deal_id}/handoff", response_model=DealHandoffOut | None)
async def deal_handoff(
    deal_id: int,
    session: AsyncSession = Depends(get_session),
    _: CurrentUser = Depends(require_permission("sales.deal.read")),
):
    """Передача выигранной сделки в исполнение (П10 ТЗ): последний эмитнутый
    ``sales.deal.handoff`` по этой сделке. None — события ещё нет (сделка не won
    или handoff не эмитнут / событие в outbox без processed_at)."""
    from core.domain.models import OutboxEvent

    rows = (
        await session.execute(
            select(OutboxEvent)
            .where(OutboxEvent.event_type == "sales.deal.handoff")
            .order_by(OutboxEvent.id.desc())
        )
    ).scalars().all()
    for ev in rows:
        if ev.payload.get("deal_id") == deal_id:
            payload = ev.payload
            return DealHandoffOut(
                deal_id=deal_id,
                number=payload.get("number") or "",
                counterparty=payload.get("counterparty") or "",
                amount=float(payload.get("amount") or 0),
                owner=payload.get("owner") or "",
                funnel=payload.get("funnel") or "",
                items=[HandoffItem(**it) for it in payload.get("items") or []],
                gross_profit=payload.get("gross_profit"),
                handed_off_at=ev.created_at,
            )
    return None


# ── Встречное планирование РОП (PlanTarget): продавец предлагает, РОП согласует ────
@router.get("/plans", response_model=list[PlanTargetOut])
async def list_plans(
    owner_id: int | None = None,
    period_type: str | None = None,
    period_key: str | None = None,
    session: AsyncSession = Depends(get_session),
    _: CurrentUser = Depends(require_permission("sales.deal.read")),
):
    """Список планов продавца по фильтрам. Пусто → []."""
    stmt = select(PlanTarget)
    if owner_id is not None:
        stmt = stmt.where(PlanTarget.owner_id == owner_id)
    if period_type is not None:
        stmt = stmt.where(PlanTarget.period_type == period_type)
    if period_key is not None:
        stmt = stmt.where(PlanTarget.period_key == period_key)
    return (await session.execute(stmt.order_by(PlanTarget.metric))).scalars().all()


@router.post("/plans", response_model=PlanTargetOut)
async def upsert_plan(
    payload: PlanTargetIn,
    session: AsyncSession = Depends(get_session),
    _: CurrentUser = Depends(require_permission("sales.deal.write")),
):
    """Поставить/изменить ``draft`` цель по (owner_id, metric, period_type, period_key).

    Upsert по UniqueConstraint; уже согласованный план (``approved``) трогать нельзя — 409.
    """
    existing = (
        await session.execute(
            select(PlanTarget).where(
                PlanTarget.owner_id == payload.owner_id,
                PlanTarget.metric == payload.metric,
                PlanTarget.period_type == payload.period_type,
                PlanTarget.period_key == payload.period_key,
            )
        )
    ).scalars().first()
    if existing is not None:
        if existing.status == "approved":
            raise HTTPException(status_code=409, detail="План уже согласован — изменить нельзя")
        existing.target = payload.target
        # сброс отказа на draft (продавец может пересогласовать новым значением)
        if existing.status == "rejected":
            existing.status = "draft"
            existing.approved_by = None
            existing.approved_at = None
        await session.commit()
        return existing
    plan = PlanTarget(
        owner_id=payload.owner_id,
        metric=payload.metric,
        period_type=payload.period_type,
        period_key=payload.period_key,
        target=payload.target,
        status="draft",
    )
    session.add(plan)
    await session.commit()
    return plan


@router.post("/plans/{plan_id}/submit", response_model=PlanTargetOut)
async def submit_plan(
    plan_id: int,
    core: Core = Depends(get_core),
    session: AsyncSession = Depends(get_session),
    user: CurrentUser = Depends(require_permission("sales.deal.write")),
):
    """Продавец отправляет ``draft`` план на согласование РОПу (через approvals)."""
    plan = await session.get(PlanTarget, plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail="План не найден")
    if plan.status != "draft":
        raise HTTPException(status_code=409, detail=f"Нельзя отправить план в статусе {plan.status}")
    plan.status = "pending_approval"
    await core.services.approvals.request(
        session,
        kind="sales_plan",
        entity_ref=f"plan:{plan.id}",
        subject=f"План {plan.metric} {plan.period_type} {plan.period_key} = {float(plan.target)}",
        requested_by=user.username,
    )
    await session.commit()
    return plan


@router.post("/plans/{plan_id}/decide", response_model=PlanTargetOut)
async def decide_plan(
    plan_id: int,
    payload: PlanDecisionIn,
    core: Core = Depends(get_core),
    session: AsyncSession = Depends(get_session),
    user: CurrentUser = Depends(require_permission("sales.deal.approve")),
):
    """РОП согласует или отклоняет план: approve → approved, иначе → rejected.

    Эмитит ``sales.plan.approved`` / ``sales.plan.rejected`` (actor=РОП → audit). Право
    ``sales.deal.approve`` уже есть только у роли «РОП».
    """
    plan = await session.get(PlanTarget, plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail="План не найден")
    if plan.status != "pending_approval":
        raise HTTPException(
            status_code=409,
            detail=f"План в статусе {plan.status} — согласовать нельзя (нужен pending_approval)",
        )
    plan.status = "approved" if payload.approved else "rejected"
    plan.approved_by = user.username
    plan.approved_at = _utcnow()
    core.event_bus.emit(
        session,
        "sales.plan.approved" if payload.approved else "sales.plan.rejected",
        {
            "plan_id": plan.id,
            "owner_id": plan.owner_id,
            "metric": plan.metric,
            "period_type": plan.period_type,
            "period_key": plan.period_key,
            "target": float(plan.target),
            "by": user.username,
            "comment": payload.comment,
            "actor": "РОП",
            "entity_ref": f"plan:{plan.id}",
        },
    )
    await session.commit()
    return plan


@router.get("/loss-reasons", response_model=list[LossReasonOut])
async def loss_reasons(session: AsyncSession = Depends(get_session)):
    """Справочник активных причин отказа (для выпадашки модалки «Отказ», SALES-40)."""
    return (
        await session.execute(
            select(LossReason).where(LossReason.active).order_by(LossReason.sort_order)
        )
    ).scalars().all()


@router.post("/deals/{deal_id}/lose", response_model=DealRead)
async def lose_deal(
    deal_id: int,
    payload: LoseRequest,
    core: Core = Depends(get_core),
    session: AsyncSession = Depends(get_session),
    user: CurrentUser = Depends(get_current_user),
):
    """Закрыть сделку в отказ с обязательной причиной (SALES-40).

    Причина обязательна; если справочник заполнен — должна быть активным кодом.
    Ставит стадию ``lost`` (через ``record_stage`` → история + ``stage_changed_at``),
    дату закрытия и публикует ``sales.deal.lost`` (→ audit)."""
    deal = await DealRepository(session).get(deal_id)
    if deal is None:
        raise HTTPException(status_code=404, detail="Сделка не найдена")
    if deal.stage == "lost":
        raise HTTPException(status_code=409, detail="Сделка уже закрыта в отказ")
    code = (payload.reason_code or "").strip()
    if not code:
        raise HTTPException(status_code=422, detail="Нужна причина отказа")
    active_codes = set(
        (await session.execute(select(LossReason.code).where(LossReason.active))).scalars().all()
    )
    if active_codes and code not in active_codes:
        raise HTTPException(status_code=422, detail="Неизвестная причина отказа")

    deal.lost_reason_code = code
    deal.lost_comment = payload.comment
    deal.closed_date = date.today().strftime("%d.%m.%Y")
    record_stage(session, deal, "lost", by=user.username)
    core.event_bus.emit(
        session,
        "sales.deal.lost",
        {
            "deal_id": deal.id, "number": deal.number, "reason_code": code,
            "amount": float(deal.amount), "owner": deal.owner, "entity_ref": f"deal:{deal.id}",
        },
    )
    await session.commit()
    return deal


@router.post("/deals/{deal_id}/win", response_model=DealRead)
async def win_deal(
    deal_id: int,
    core: Core = Depends(get_core),
    session: AsyncSession = Depends(get_session),
    user: CurrentUser = Depends(get_current_user),
):
    """Закрыть сделку успешно (SALES-40). Единый путь с логистикой (`record_stage`):
    стадия ``won``, дата закрытия, событие ``sales.deal.won`` (→ audit)."""
    deal = await DealRepository(session).get(deal_id)
    if deal is None:
        raise HTTPException(status_code=404, detail="Сделка не найдена")
    if deal.stage == "won":
        raise HTTPException(status_code=409, detail="Сделка уже выиграна")
    deal.closed_date = date.today().strftime("%d.%m.%Y")
    record_stage(session, deal, "won", by=user.username)
    core.event_bus.emit(
        session,
        "sales.deal.won",
        {
            "deal_id": deal.id, "number": deal.number, "amount": float(deal.amount),
            "owner": deal.owner, "entity_ref": f"deal:{deal.id}",
        },
    )
    await session.commit()
    return deal


@router.get("/deals/{deal_id}/history", response_model=list[StageEventOut])
async def deal_history(deal_id: int, session: AsyncSession = Depends(get_session)):
    """Хронология смен стадий сделки (SALES-43)."""
    return (
        await session.execute(
            select(DealStageEvent)
            .where(DealStageEvent.deal_id == deal_id)
            .order_by(DealStageEvent.id)
        )
    ).scalars().all()


@router.get("/deals/{deal_id}/tasks", response_model=list[TaskOut])
async def list_tasks(deal_id: int, session: AsyncSession = Depends(get_session)):
    """Задачи по сделке (SALES-41): открытые — первыми, по сроку."""
    rows = (
        await session.execute(
            select(DealTask)
            .where(DealTask.deal_id == deal_id)
            .order_by((DealTask.status == "open").desc(), DealTask.due_at, DealTask.id)
        )
    ).scalars().all()
    return [_task_out(t) for t in rows]


@router.post("/deals/{deal_id}/tasks", response_model=TaskOut, status_code=201)
async def create_task(
    deal_id: int,
    payload: TaskCreate,
    core: Core = Depends(get_core),
    session: AsyncSession = Depends(get_session),
):
    """Поставить задачу по сделке (SALES-41) — событие ``sales.task.created`` (→ audit)."""
    deal = await DealRepository(session).get(deal_id)
    if deal is None:
        raise HTTPException(status_code=404, detail="Сделка не найдена")
    task = DealTask(
        deal_id=deal_id,
        title=payload.title,
        kind=payload.kind,
        assignee_id=payload.assignee_id,
        due_at=payload.due_at,
    )
    session.add(task)
    await session.flush()
    core.event_bus.emit(
        session,
        "sales.task.created",
        {"task_id": task.id, "deal_id": deal_id, "entity_ref": f"deal:{deal_id}"},
    )
    await session.commit()
    return _task_out(task)


@router.patch("/tasks/{task_id}", response_model=TaskOut)
async def update_task(
    task_id: int,
    payload: TaskUpdate,
    core: Core = Depends(get_core),
    session: AsyncSession = Depends(get_session),
):
    """Изменить задачу: перенос срока / исполнение / отмена. При закрытии (``done``)
    ставит ``done_at`` и публикует ``sales.task.completed`` (→ audit, SALES-41)."""
    task = await session.get(DealTask, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Задача не найдена")
    data = payload.model_dump(exclude_unset=True)
    becoming_done = data.get("status") == "done" and task.status != "done"
    for key, value in data.items():
        setattr(task, key, value)
    if becoming_done:
        task.done_at = _utcnow()
        core.event_bus.emit(
            session,
            "sales.task.completed",
            {"task_id": task.id, "deal_id": task.deal_id, "entity_ref": f"deal:{task.deal_id}"},
        )
    await session.commit()
    return _task_out(task)


@router.post("/deals", response_model=DealRead, status_code=201)
async def create_deal(
    payload: DealCreate,
    session: AsyncSession = Depends(get_session),
    core: Core = Depends(get_core),
):
    """Создать сделку и опубликовать доменное событие через шину ядра."""
    try:
        deal = await DealRepository(session).create(payload)
        core.event_bus.emit(session, "sales.deal.created", {"number": deal.number, "title": deal.title})
        if deal.ship_deadline:
            await _emit_ship_deadline(session, core, deal)
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(status_code=409, detail="Сделка с таким номером уже существует")
    return deal


@router.post("/deals/{deal_id}/request-approval", response_model=ApprovalOut, status_code=201)
async def request_approval(
    deal_id: int,
    payload: ApprovalRequest,
    core: Core = Depends(get_core),
    session: AsyncSession = Depends(get_session),
):
    """Отправить сделку на согласование (например, договор → юристу)."""
    deal = await DealRepository(session).get(deal_id)
    if deal is None:
        raise HTTPException(status_code=404, detail="Сделка не найдена")
    approval = await core.services.approvals.request(
        session,
        payload.kind,
        f"deal:{deal_id}",
        f"{deal.number} — {deal.counterparty}",
        payload.requested_by,
    )
    await session.commit()
    return approval


@router.get("/skus", response_model=list[SkuOut])
async def list_skus(session: AsyncSession = Depends(get_session)):
    """Справочник номенклатуры (для подбора позиций в сделку, sales-12)."""
    return (await session.execute(select(Sku).order_by(Sku.code))).scalars().all()


@router.get("/deals/{deal_id}/items", response_model=list[DealItemOut])
async def list_deal_items(deal_id: int, session: AsyncSession = Depends(get_session)):
    """Позиции номенклатуры сделки (с данными SKU и ценами клиенту)."""
    deal = await DealRepository(session).get(deal_id)
    counterparty = deal.counterparty if deal else ""
    rows = (
        await session.execute(
            select(DealItem).where(DealItem.deal_id == deal_id).order_by(DealItem.id)
        )
    ).scalars().all()
    return [await _build_item_out(session, r, counterparty) for r in rows]


@router.post("/deals/{deal_id}/items", response_model=DealItemOut, status_code=201)
async def add_deal_item(
    deal_id: int,
    payload: DealItemCreate,
    core: Core = Depends(get_core),
    session: AsyncSession = Depends(get_session),
):
    """Добавить позицию номенклатуры в сделку (подбор из SKU, sales-12)."""
    deal = await DealRepository(session).get(deal_id)
    if deal is None:
        raise HTTPException(status_code=404, detail="Сделка не найдена")
    if await session.get(Sku, payload.sku_id) is None:
        raise HTTPException(status_code=404, detail="Номенклатура не найдена")
    item = DealItem(deal_id=deal_id, sku_id=payload.sku_id, qty=Decimal(str(payload.qty)))
    session.add(item)
    await session.flush()
    core.event_bus.emit(
        session,
        "sales.item.changed",
        {"deal_id": deal_id, "action": "added", "entity_ref": f"deal:{deal_id}"},
    )
    await session.commit()
    return await _build_item_out(session, item, deal.counterparty)


@router.patch("/deal-items/{item_id}", response_model=DealItemOut)
async def update_deal_item(
    item_id: int,
    payload: DealItemUpdate,
    session: AsyncSession = Depends(get_session),
):
    """Изменить количество в позиции сделки."""
    item = await session.get(DealItem, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Позиция не найдена")
    item.qty = Decimal(str(payload.qty))
    deal = await DealRepository(session).get(item.deal_id)
    await session.commit()
    return await _build_item_out(session, item, deal.counterparty if deal else "")


@router.delete("/deal-items/{item_id}", status_code=204)
async def delete_deal_item(item_id: int, session: AsyncSession = Depends(get_session)):
    """Удалить позицию номенклатуры из сделки."""
    item = await session.get(DealItem, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Позиция не найдена")
    await session.delete(item)
    await session.commit()


@router.get("/deals/{deal_id}/contacts", response_model=list[ContactOut])
async def list_contacts(deal_id: int, session: AsyncSession = Depends(get_session)):
    """Контакты контрагента сделки (основной — первым), sales-13."""
    deal = await DealRepository(session).get(deal_id)
    if deal is None:
        return []
    cp = await _counterparty_for_deal(session, deal)
    if cp is None:
        return []
    return (
        await session.execute(
            select(Contact)
            .where(Contact.counterparty_id == cp.id)
            .order_by(Contact.is_primary.desc(), Contact.id)
        )
    ).scalars().all()


@router.post("/deals/{deal_id}/contacts", response_model=ContactOut, status_code=201)
async def add_contact(
    deal_id: int, payload: ContactCreate, session: AsyncSession = Depends(get_session)
):
    """Добавить контакт контрагенту сделки (контрагент создаётся по имени при нужде)."""
    deal = await DealRepository(session).get(deal_id)
    if deal is None:
        raise HTTPException(status_code=404, detail="Сделка не найдена")
    cp = await _counterparty_for_deal(session, deal, create=True)
    assert cp is not None
    if payload.is_primary:
        await _clear_primary(session, cp.id)
    contact = Contact(
        counterparty_id=cp.id,
        full_name=payload.full_name,
        phone=payload.phone,
        email=payload.email,
        is_primary=payload.is_primary,
    )
    session.add(contact)
    await session.flush()
    await session.commit()
    return contact


@router.get("/chats", response_model=list[ChatOut])
async def list_chats(session: AsyncSession = Depends(get_session)):
    """Диалоги для панели «Чаты и дела»: сделки с последним сообщением переписки.

    Graceful fallback: при ``OperationalError`` (колонка/таблица отсутствует — старый
    dev.db до миграции 0062) возвращаем ``[]`` — фронт честно покажет «нет диалогов»
    вместо 500.
    """
    from sqlalchemy.exc import OperationalError, ProgrammingError

    try:
        msgs = (
            await session.execute(select(Message).order_by(Message.id.desc()).limit(100))
        ).scalars().all()
        deals = {d.id: d for d in (await session.execute(select(Deal))).scalars().all()}
    except (OperationalError, ProgrammingError):
        await session.rollback()
        return []
    # SALES-49: непрочитанные входящие по сделкам (для бейджа в панели чатов)
    unread_map = {
        deal_id: int(n)
        for deal_id, n in (
            await session.execute(
                select(Message.deal_id, func.count())
                .where(Message.direction == "in", Message.read_at.is_(None))
                .group_by(Message.deal_id)
            )
        ).all()
    }
    chats: list[ChatOut] = []
    seen: set[int] = set()
    for m in msgs:
        if m.deal_id in seen or m.deal_id not in deals:
            continue
        seen.add(m.deal_id)
        deal = deals[m.deal_id]
        chats.append(
            ChatOut(
                deal_id=m.deal_id,
                number=deal.number,
                company=deal.counterparty,
                last_text=m.text,
                channel=m.channel,
                direction=m.direction,
                unread=unread_map.get(m.deal_id, 0),
            )
        )
        if len(chats) >= 20:
            break
    return chats


@router.patch("/contacts/{contact_id}/primary", response_model=ContactOut)
async def set_primary_contact(contact_id: int, session: AsyncSession = Depends(get_session)):
    """Назначить контакт основным (снимая признак с остальных контактов контрагента)."""
    contact = await session.get(Contact, contact_id)
    if contact is None:
        raise HTTPException(status_code=404, detail="Контакт не найден")
    if contact.counterparty_id is not None:
        await _clear_primary(session, contact.counterparty_id)
    contact.is_primary = True
    await session.commit()
    return contact


@router.get("/deals/{deal_id}/documents", response_model=list[DocumentOut])
async def list_documents(deal_id: int, session: AsyncSession = Depends(get_session)):
    """Документы сделки (счета/договоры/заказы) с их состоянием и номерами в 1С."""
    return (
        await session.execute(
            select(DealDocument).where(DealDocument.deal_id == deal_id).order_by(DealDocument.id)
        )
    ).scalars().all()


@router.post("/deals/{deal_id}/documents", response_model=DocumentOut, status_code=201)
async def create_document(
    deal_id: int,
    payload: DocumentCreate,
    core: Core = Depends(get_core),
    session: AsyncSession = Depends(get_session),
):
    """Сформировать документ сделки (часть 9).

    Счёт/заказ пишутся в 1С сразу (``draft`` → ``posted``). Договор сначала
    уходит на согласование юристу (движок ч.4, маршрут ``deal.contract``) —
    статус ``pending_approval``; в 1С он записывается только после одобрения
    (``POST /sales/documents/{id}/decide``). Здесь ядро и CRM смыкаются в поток.
    """
    deal = await DealRepository(session).get(deal_id)
    if deal is None:
        raise HTTPException(status_code=404, detail="Сделка не найдена")
    if core.services.onec is None:
        raise HTTPException(status_code=503, detail="Интеграция 1С не подключена")

    prefix = DOC_NUMBER_PREFIX.get(payload.kind, "ДОК")
    number = f"{prefix}-{deal.number}"
    doc = DealDocument(deal_id=deal_id, kind=payload.kind, number=number, amount=deal.amount)
    session.add(doc)
    await session.flush()

    if payload.kind in REQUIRES_APPROVAL:
        # договор: на согласование юристу (ч.4); запись в 1С — после одобрения
        await _submit_contract_for_approval(core, session, doc, deal, payload.requested_by)
    else:
        # счёт/заказ: пишем в 1С сразу; счёт и заказ дополнительно резервируют остатки (SALES-51)
        if payload.kind in RESERVES_STOCK and core.services.stock is not None:
            reserved = await core.services.stock.reserve(
                session, await _deal_stock_items(session, deal_id)
            )
            if reserved:
                # фиксируем резерв на документе + срок действия счёта (5 дней по счёт-протоколу)
                valid_days = int(os.getenv("AIOS_INVOICE_VALID_DAYS", "5"))
                doc.reserve_status = "reserved"
                doc.reserved_at = _utcnow()
                doc.valid_until = _utcnow().date() + timedelta(days=valid_days)
                core.event_bus.emit(
                    session,
                    "sales.stock.reserved",
                    {
                        "document_id": doc.id,
                        "deal_id": deal_id,
                        "items": reserved,
                        "valid_until": doc.valid_until.isoformat(),
                        "entity_ref": f"deal:{deal_id}",
                    },
                )
        await _post_document_to_1c(core, session, doc, deal.counterparty)

    await session.commit()
    return doc


@router.post("/documents/{doc_id}/decide", response_model=DocumentOut)
async def decide_document(
    doc_id: int,
    payload: DocumentDecision,
    core: Core = Depends(get_core),
    session: AsyncSession = Depends(get_session),
    _: object = Depends(require_permission("sales.deal.approve")),
):
    """Решение по документу на согласовании (договор): провести в 1С или отклонить.

    Решает связанное согласование (движок ч.4) и, при одобрении, проводит документ
    в 1С (часть 9) — всё в одной транзакции. Согласование и проведение фиксируются
    событиями (→ audit log).
    """
    doc = await session.get(DealDocument, doc_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="Документ не найден")
    if doc.status != "pending_approval":
        raise HTTPException(status_code=409, detail="Документ не на согласовании")
    if core.services.onec is None:
        raise HTTPException(status_code=503, detail="Интеграция 1С не подключена")

    approval = (
        await session.execute(
            select(Approval).where(
                Approval.entity_ref == f"document:{doc_id}", Approval.status == "pending"
            )
        )
    ).scalars().first()
    if approval is not None:
        await core.services.approvals.decide(session, approval, payload.approved, payload.by)

    if payload.approved:
        deal = await DealRepository(session).get(doc.deal_id)
        await _post_document_to_1c(core, session, doc, deal.counterparty if deal else "")
    else:
        doc.status = "rejected"
        core.event_bus.emit(
            session,
            "sales.document.rejected",
            {
                "document_id": doc.id,
                "deal_id": doc.deal_id,
                "kind": doc.kind,
                "number": doc.number,
                "entity_ref": f"deal:{doc.deal_id}",
            },
        )

    await session.commit()
    return doc


# ──────────────────────── Договор по шаблону (SALES-53) ────────────────────────

# Плейсхолдеры тела шаблона: {{seller.name}}, {{buyer.unp}}, {{items}}, {{total}}, …
_PLACEHOLDER = re.compile(r"\{\{\s*([\w.]+)\s*\}\}")


def _seller_requisites(core: Core) -> dict[str, str]:
    """Реквизиты своей организации (продавца) из конфига (ТЗ C.5, не shared-схема)."""
    c = core.config
    return {
        "name": c.seller_name, "unp": c.seller_unp, "address": c.seller_address,
        "director": c.seller_director, "phone": c.seller_phone, "email": c.seller_email,
    }


async def _buyer_requisites(
    session: AsyncSession, core: Core, deal: Deal, unp: str
) -> dict[str, str]:
    """Реквизиты покупателя: из ЕГР по УНП (graceful) + обогащение Counterparty.unp.

    Реестр выключен или УНП не найден → минимум из сделки (вводится вручную). Имя
    контрагента в Counterparty не перезатираем (связь сделки — по имени). Внешний
    вызов реестра делаем ДО создания контрагента, чтобы не держать открытую запись/блок
    в БД на время сетевого запроса в ЕГР.
    """
    info = None
    if unp and core.services.registry is not None:
        info = await core.services.registry.lookup(unp)
    cp = await _counterparty_for_deal(session, deal, create=True)
    buyer = {"name": deal.counterparty, "unp": unp or (cp.unp if cp else "") or ""}
    if info:
        buyer.update({k: str(v) for k, v in info.items() if v})
    if cp is not None and unp:
        cp.unp = cp.unp or unp
    return buyer


async def _contract_items(session: AsyncSession, deal_id: int) -> list[str]:
    """Строки спецификации договора из позиций сделки (title — qty unit)."""
    rows = (
        await session.execute(
            select(DealItem, Sku)
            .join(Sku, Sku.id == DealItem.sku_id, isouter=True)
            .where(DealItem.deal_id == deal_id)
            .order_by(DealItem.id)
        )
    ).all()
    lines = []
    for item, sku in rows:
        title = sku.title if sku else f"позиция #{item.sku_id}"
        unit = sku.unit if sku else "шт"
        lines.append(f"{title} — {item.qty} {unit}")
    return lines


def _render_contract(body: str, ctx: dict[str, str]) -> str:
    """Подставить плейсхолдеры {{key}} (плоские ключи seller.name/buyer.unp/…)."""
    return _PLACEHOLDER.sub(lambda m: ctx.get(m.group(1), ""), body)


async def _submit_contract_for_approval(
    core: Core,
    session: AsyncSession,
    doc: DealDocument,
    deal: Deal,
    requested_by: str,
    extra_event: dict | None = None,
) -> None:
    """Договор → на согласование юристу (ч.4) + событие ``sales.document.created``.

    Общий путь для обоих способов создания договора: универсального
    ``POST /documents`` и ``POST /deals/{id}/contract`` (SALES-53) — чтобы маршрут
    согласования и форма события не разъезжались.
    """
    doc.status = "pending_approval"
    await core.services.approvals.request(
        session,
        "deal.contract",
        f"document:{doc.id}",
        f"{deal.number} — {DOC_TITLES['contract']} ({deal.counterparty})",
        requested_by,
    )
    payload = {
        "document_id": doc.id,
        "deal_id": deal.id,
        "kind": "contract",
        "number": doc.number,
        "entity_ref": f"deal:{deal.id}",
    }
    if extra_event:
        payload.update(extra_event)
    core.event_bus.emit(session, "sales.document.created", payload)


@router.get("/contract-templates", response_model=list[ContractTemplateOut])
async def list_contract_templates(session: AsyncSession = Depends(get_session)):
    """Активные шаблоны договора для окна «Подготовить договор» (SALES-53)."""
    return (
        await session.execute(
            select(ContractTemplate)
            .where(ContractTemplate.is_active.is_(True))
            .order_by(ContractTemplate.name)
        )
    ).scalars().all()


@router.post("/contract-templates", response_model=ContractTemplateOut, status_code=201)
async def create_contract_template(
    payload: ContractTemplateCreate,
    session: AsyncSession = Depends(get_session),
    _: object = Depends(require_permission("sales.deal.write")),
):
    """Завести/сидировать шаблон договора (SALES-53)."""
    tpl = ContractTemplate(code=payload.code, name=payload.name, body=payload.body)
    session.add(tpl)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(status_code=409, detail="Шаблон с таким кодом уже есть")
    return tpl


@router.post("/deals/{deal_id}/contract", response_model=DocumentOut, status_code=201)
async def prepare_contract(
    deal_id: int,
    payload: ContractPrepareIn,
    core: Core = Depends(get_core),
    session: AsyncSession = Depends(get_session),
    _: object = Depends(require_permission("sales.deal.write")),
):
    """SALES-53: подготовить договор по шаблону + реквизиты покупателя по УНП.

    Реквизиты покупателя подтягиваются из ЕГР по УНП (graceful при выкл реестра),
    Counterparty обогащается УНП, условия частично предзаполнены из сделки. Договор
    создаётся как DealDocument(kind=contract) и уходит на согласование; запись в 1С —
    после одобрения (/documents/{id}/decide), поэтому шлюз 1С здесь не требуется.
    """
    deal = await DealRepository(session).get(deal_id)
    if deal is None:
        raise HTTPException(status_code=404, detail="Сделка не найдена")
    tpl = (
        await session.execute(
            select(ContractTemplate).where(
                ContractTemplate.code == payload.template_code,
                ContractTemplate.is_active.is_(True),
            )
        )
    ).scalars().first()
    if tpl is None:
        raise HTTPException(status_code=404, detail="Шаблон договора не найден")
    # один активный договор на сделку: номер ДГ-{deal} не уникален в БД, дубль создал бы
    # два договора с одинаковым номером (отклонённый можно перевыставить).
    existing = (
        await session.execute(
            select(DealDocument).where(
                DealDocument.deal_id == deal_id,
                DealDocument.kind == "contract",
                DealDocument.status != "rejected",
            )
        )
    ).scalars().first()
    if existing is not None:
        raise HTTPException(status_code=409, detail="Договор по сделке уже подготовлен")

    buyer = await _buyer_requisites(session, core, deal, payload.unp.strip())
    doc = DealDocument(
        deal_id=deal_id,
        kind="contract",
        number=f"{DOC_NUMBER_PREFIX['contract']}-{deal.number}",
        amount=deal.amount,
        template_id=tpl.id,
        payment_terms=payload.payment_terms or None,
        delivery_terms=payload.delivery_terms or None,
        terms_json={"buyer": buyer, "custom": payload.terms or {}},
    )
    session.add(doc)
    await session.flush()
    await _submit_contract_for_approval(
        core, session, doc, deal, payload.requested_by,
        extra_event={"template": tpl.code, "buyer_unp": buyer.get("unp", "")},
    )
    await session.commit()
    return doc


@router.get("/documents/{doc_id}/render", response_class=HTMLResponse)
async def render_contract(
    doc_id: int,
    core: Core = Depends(get_core),
    session: AsyncSession = Depends(get_session),
    _: object = Depends(require_permission("sales.deal.read")),
):
    """Рендер договора по шаблону в HTML (печатная форма, ТЗ C.2). Только kind=contract.

    Гард ``sales.deal.read``: форма содержит реквизиты продавца и покупателя (ЕГР) —
    не отдаём анонимно (прод публичен).
    """
    doc = await session.get(DealDocument, doc_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="Документ не найден")
    if doc.kind != "contract":
        raise HTTPException(status_code=400, detail="Рендер по шаблону — только для договора")
    tpl = await session.get(ContractTemplate, doc.template_id) if doc.template_id else None
    if tpl is None:
        raise HTTPException(status_code=409, detail="У договора не задан шаблон")
    deal = await DealRepository(session).get(doc.deal_id)
    buyer = (doc.terms_json or {}).get("buyer", {})
    ctx = {
        "number": doc.number,
        "items": "; ".join(await _contract_items(session, doc.deal_id)),
        "total": f"{float(doc.amount):.2f} BYN",
        "payment_terms": doc.payment_terms or "",
        "delivery_terms": doc.delivery_terms or "",
        "valid_until": doc.valid_until.isoformat() if doc.valid_until else "",
        "deal": deal.number if deal else "",
    }
    ctx.update({f"seller.{k}": v for k, v in _seller_requisites(core).items()})
    ctx.update({f"buyer.{k}": str(v) for k, v in buyer.items()})
    return HTMLResponse(_render_contract(tpl.body, ctx))


@router.post("/deals/{deal_id}/send-package", response_model=PackageSentOut)
async def send_package(
    deal_id: int,
    core: Core = Depends(get_core),
    session: AsyncSession = Depends(get_session),
    _: object = Depends(require_permission("sales.deal.write")),
):
    """SALES-53 C.4: отправить клиенту пакет «счёт + договор» одной записью.

    Берём последний проведённый счёт и последний согласованный (проведённый) договор
    сделки — по ТЗ пакет уходит ПОСЛЕ согласования договора. Эмитим ``sales.package.sent``
    и пишем ОДНУ запись в историю переписки. Реальная доставка (email/Telegram, B.3) —
    отдельный слой; здесь фиксируем факт отправки пакета.
    """
    deal = await DealRepository(session).get(deal_id)
    if deal is None:
        raise HTTPException(status_code=404, detail="Сделка не найдена")
    docs = (
        await session.execute(
            select(DealDocument)
            .where(
                DealDocument.deal_id == deal_id,
                DealDocument.status.in_(("posted", "paid")),
            )
            .order_by(DealDocument.id.desc())
        )
    ).scalars().all()
    # счёт — проведённый/оплаченный; договор — проведённый (после согласования)
    invoice = next((d for d in docs if d.kind == "invoice"), None)
    contract = next((d for d in docs if d.kind == "contract"), None)
    if invoice is None or contract is None:
        raise HTTPException(
            status_code=409, detail="Нужны проведённый счёт и согласованный договор"
        )

    channel = "email"
    session.add(
        Message(
            deal_id=deal_id,
            channel=channel,
            direction="out",
            author="Система",
            text=f"Отправлен пакет: счёт {invoice.number} + договор {contract.number}",
        )
    )
    core.event_bus.emit(
        session,
        "sales.package.sent",
        {
            "deal_id": deal_id,
            "invoice_number": invoice.number,
            "contract_number": contract.number,
            "channel": channel,
            "entity_ref": f"deal:{deal_id}",
        },
    )
    await session.commit()
    return PackageSentOut(
        deal_id=deal_id,
        invoice_number=invoice.number,
        contract_number=contract.number,
        channel=channel,
    )


@router.get("/deals/{deal_id}/messages", response_model=list[MessageOut])
async def list_messages(deal_id: int, session: AsyncSession = Depends(get_session)):
    """Омниканальная история переписки по сделке (часть 10)."""
    return (
        await session.execute(
            select(Message).where(Message.deal_id == deal_id).order_by(Message.id)
        )
    ).scalars().all()


@router.post("/deals/{deal_id}/messages", response_model=MessageOut, status_code=201)
async def create_message(
    deal_id: int,
    payload: MessageCreate,
    core: Core = Depends(get_core),
    session: AsyncSession = Depends(get_session),
):
    """Отправить/зафиксировать сообщение по сделке (канал + текст) — событие в шину."""
    deal = await DealRepository(session).get(deal_id)
    if deal is None:
        raise HTTPException(status_code=404, detail="Сделка не найдена")
    msg = Message(
        deal_id=deal_id,
        channel=payload.channel,
        direction=payload.direction,
        author=payload.author,
        text=payload.text,
    )
    session.add(msg)
    await session.flush()
    core.event_bus.emit(
        session,
        "sales.message.sent",
        {
            "message_id": msg.id,
            "deal_id": deal_id,
            "channel": payload.channel,
            "direction": payload.direction,
            "entity_ref": f"deal:{deal_id}",
        },
    )
    await session.commit()
    return msg


@router.post("/deals/{deal_id}/messages/read")
async def mark_messages_read(deal_id: int, session: AsyncSession = Depends(get_session)):
    """Пометить входящие сообщения сделки прочитанными (обнуляет счётчик, SALES-49)."""
    deal = await DealRepository(session).get(deal_id)
    if deal is None:
        raise HTTPException(status_code=404, detail="Сделка не найдена")
    rows = (
        await session.execute(
            select(Message).where(
                Message.deal_id == deal_id,
                Message.direction == "in",
                Message.read_at.is_(None),
            )
        )
    ).scalars().all()
    now = _utcnow()
    for m in rows:
        m.read_at = now
    await session.commit()
    return {"ok": True, "read": len(rows)}


@router.get("/prices/{sku_code}", response_model=PriceInfo)
async def price_info(
    sku_code: str, counterparty: str = "", session: AsyncSession = Depends(get_session)
):
    """История цен по SKU → последняя и минимальная цена клиенту (Price Engine, sales-22)."""
    return await _price_summary(session, sku_code, counterparty)


@router.post("/prices", status_code=201)
async def create_price_quote(
    payload: PriceQuoteCreate,
    core: Core = Depends(get_core),
    session: AsyncSession = Depends(get_session),
):
    """Зафиксировать котировку цены SKU клиенту (пополняет историю Price Engine)."""
    session.add(
        PriceQuote(
            sku_code=payload.sku_code,
            counterparty=payload.counterparty,
            price=Decimal(str(payload.price)),
        )
    )
    core.event_bus.emit(
        session,
        "sales.price.quoted",
        {"sku_code": payload.sku_code, "counterparty": payload.counterparty, "price": payload.price},
    )
    await session.commit()
    return {"ok": True}


# --- Приём, квалификация и распределение лидов (вход воронки, ФАЗА 1) ---


async def _known_customer(session: AsyncSession, company: str) -> bool:
    """Лид от действующего контрагента? (повышает балл, даёт воронку «постоянные»)."""
    if not company:
        return False
    cp = (
        await session.execute(select(Counterparty).where(Counterparty.name == company))
    ).scalars().first()
    return cp is not None


async def _manager_loads(session: AsyncSession) -> dict[str, int]:
    """Загрузка менеджеров: активные распределённые лиды + открытые сделки (для роутинга)."""
    loads: dict[str, int] = {}
    lead_rows = (
        await session.execute(
            select(Lead.assigned_to, func.count())
            .where(Lead.status == "routed", Lead.assigned_to != "")
            .group_by(Lead.assigned_to)
        )
    ).all()
    deal_rows = (
        await session.execute(
            select(Deal.owner, func.count())
            .where(Deal.stage != "won", Deal.owner != "")
            .group_by(Deal.owner)
        )
    ).all()
    for name, n in [*lead_rows, *deal_rows]:
        loads[name] = loads.get(name, 0) + n
    return loads


@leads_router.get("", response_model=list[LeadOut])
async def list_leads(status: str = "", session: AsyncSession = Depends(get_session)):
    """Приём лидов: входящие заявки воронки (новые — первыми; опц. фильтр по статусу)."""
    query = select(Lead).order_by(Lead.id.desc())
    if status:
        query = query.where(Lead.status == status)
    return (await session.execute(query)).scalars().all()


@leads_router.post("", response_model=LeadOut, status_code=201)
async def create_lead(
    payload: LeadCreate,
    core: Core = Depends(get_core),
    session: AsyncSession = Depends(get_session),
):
    """Принять лид из канала (сайт/мессенджер/e-mail/телефония/тендер) → событие в шину."""
    lead = Lead(**payload.model_dump())
    session.add(lead)
    await session.flush()
    core.event_bus.emit(
        session,
        "sales.lead.received",
        {"lead_id": lead.id, "source": lead.source, "entity_ref": f"lead:{lead.id}"},
    )
    await session.commit()
    return lead


@leads_router.get("/{lead_id}", response_model=LeadOut)
async def get_lead(lead_id: int, session: AsyncSession = Depends(get_session)):
    """Один лид по id."""
    lead = await session.get(Lead, lead_id)
    if lead is None:
        raise HTTPException(status_code=404, detail="Лид не найден")
    return lead


@leads_router.post("/{lead_id}/qualify", response_model=LeadQualifyOut)
async def qualify(
    lead_id: int,
    core: Core = Depends(get_core),
    session: AsyncSession = Depends(get_session),
):
    """Квалифицировать лид (Lead Qualifier): балл + вердикт целевой/нецелевой.

    Скоринг детерминирован и работает без AI. При включённом AI-слое добавляется
    текстовое обоснование через общий шлюз, действие фиксируется ``ai.lead.qualified``
    (→ audit, §3.3); без AI — событие ``sales.lead.qualified``. Под-фича за
    feature-flag, без переписывания механики (§2.5).
    """
    lead = await session.get(Lead, lead_id)
    if lead is None:
        raise HTTPException(status_code=404, detail="Лид не найден")

    known = await _known_customer(session, lead.company)
    score, verdict, reason = score_lead(lead, known)
    lead.score = score
    lead.qualification = verdict
    lead.reason = reason
    if lead.status == "new":
        lead.status = "qualified"

    rationale: str | None = None
    model: str | None = None
    llm = core.services.llm
    if llm.enabled:
        rationale = await qualify_lead(llm, lead, score, verdict)
        model = llm.model or "mock"
        core.event_bus.emit(
            session,
            "ai.lead.qualified",
            {
                "lead_id": lead.id, "score": score, "verdict": verdict, "model": model,
                "actor": "AI", "entity_ref": f"lead:{lead.id}",
            },
        )
    else:
        core.event_bus.emit(
            session,
            "sales.lead.qualified",
            {"lead_id": lead.id, "score": score, "verdict": verdict, "entity_ref": f"lead:{lead.id}"},
        )
    await session.commit()
    return LeadQualifyOut(
        id=lead.id, status=lead.status, score=score, qualification=verdict,
        reason=reason, ai_rationale=rationale, model=model,
    )


@leads_router.post("/{lead_id}/route", response_model=LeadRouteOut)
async def route(
    lead_id: int,
    core: Core = Depends(get_core),
    session: AsyncSession = Depends(get_session),
):
    """Распределить лид на менеджера по правилам (география/продукт/нагрузка/воронка).

    Публикует ``sales.lead.routed`` (→ audit). Уже сконвертированный лид — 409.
    """
    lead = await session.get(Lead, lead_id)
    if lead is None:
        raise HTTPException(status_code=404, detail="Лид не найден")
    if lead.status == "converted":
        raise HTTPException(status_code=409, detail="Лид уже сконвертирован в сделку")

    known = await _known_customer(session, lead.company)
    loads = await _manager_loads(session)
    manager, funnel = route_lead(lead, loads, known)
    lead.assigned_to = manager
    lead.funnel = funnel
    lead.status = "routed"
    core.event_bus.emit(
        session,
        "sales.lead.routed",
        {
            "lead_id": lead.id, "assigned_to": manager, "funnel": funnel,
            "entity_ref": f"lead:{lead.id}",
        },
    )
    await session.commit()
    return LeadRouteOut(id=lead.id, status=lead.status, assigned_to=manager, funnel=funnel)


@leads_router.post("/{lead_id}/convert", response_model=LeadConvertOut, status_code=201)
async def convert_lead(
    lead_id: int,
    core: Core = Depends(get_core),
    session: AsyncSession = Depends(get_session),
):
    """Превратить распределённый лид в сделку — вход воронки замыкается на ``Deal``.

    Создаёт сделку (стадия ``new``, ответственный = назначенный менеджер, приоритет
    по баллу) и публикует ``sales.deal.created``; лид помечается ``converted`` со
    ссылкой на сделку. Требует предварительного распределения (иначе 409).
    """
    lead = await session.get(Lead, lead_id)
    if lead is None:
        raise HTTPException(status_code=404, detail="Лид не найден")
    if lead.status == "converted":
        raise HTTPException(status_code=409, detail="Лид уже сконвертирован в сделку")
    if lead.status != "routed":
        raise HTTPException(status_code=409, detail="Сначала распределите лид на менеджера")

    deal = Deal(
        number=f"CRM-LEAD-{lead.id}",
        title=lead.product or (lead.message[:60] if lead.message else "") or "Лид",
        counterparty=lead.company or lead.name or "Новый лид",
        owner=lead.assigned_to,
        stage="new",
        priority=lead_priority(lead.score),
    )
    session.add(deal)
    await session.flush()
    lead.status = "converted"
    lead.deal_id = deal.id
    core.event_bus.emit(
        session,
        "sales.deal.created",
        {
            "number": deal.number, "title": deal.title, "lead_id": lead.id,
            "entity_ref": f"deal:{deal.id}",
        },
    )
    await session.commit()
    return LeadConvertOut(lead_id=lead.id, deal_id=deal.id, number=deal.number, status=lead.status)


@router.post("/deals/{deal_id}/ai/draft-reply", response_model=AiDraftOut)
async def ai_draft_reply(
    deal_id: int,
    core: Core = Depends(get_core),
    session: AsyncSession = Depends(get_session),
):
    """AI-черновик ответа клиенту по истории переписки (AI-слой, Итерация 1).

    Под-фича модуля за feature-flag: при выключенном AI — 503. Генерация идёт
    через общий шлюз ``core.services.llm``; AI-действие фиксируется событием
    ``ai.draft.generated`` (→ audit, трассировка §3.3).
    """
    if not core.services.llm.enabled:
        raise HTTPException(status_code=503, detail="AI-слой выключен (feature-flag)")
    deal = await DealRepository(session).get(deal_id)
    if deal is None:
        raise HTTPException(status_code=404, detail="Сделка не найдена")

    messages = (
        await session.execute(
            select(Message).where(Message.deal_id == deal_id).order_by(Message.id)
        )
    ).scalars().all()
    text = await draft_reply(core.services.llm, deal, messages)
    model = core.services.llm.model or "mock"

    core.event_bus.emit(
        session,
        "ai.draft.generated",
        {"deal_id": deal_id, "model": model, "actor": "AI", "entity_ref": f"deal:{deal_id}"},
    )
    await session.commit()
    return AiDraftOut(text=text, model=model)


@router.post("/deals/{deal_id}/ai/assist", response_model=AiTextOut)
async def ai_assist(
    deal_id: int,
    payload: AiAssistRequest,
    core: Core = Depends(get_core),
    session: AsyncSession = Depends(get_session),
):
    """AI-ассистент сделки: резюме или следующий шаг (AI-слой, Итерация 1).

    Под-фича модуля за feature-flag (503 если AI выкл). Контекст сделки (позиции,
    документы, переписка) идёт в общий шлюз ``core.services.llm``; AI-действие
    фиксируется событием ``ai.<kind>.generated`` (→ audit, §3.3).
    """
    if not core.services.llm.enabled:
        raise HTTPException(status_code=503, detail="AI-слой выключен (feature-flag)")
    deal = await DealRepository(session).get(deal_id)
    if deal is None:
        raise HTTPException(status_code=404, detail="Сделка не найдена")

    async def _count(model, deal_col) -> int:
        return (
            await session.execute(select(func.count()).select_from(model).where(deal_col == deal_id))
        ).scalar() or 0

    context = (
        f"Позиций: {await _count(DealItem, DealItem.deal_id)}, "
        f"документов: {await _count(DealDocument, DealDocument.deal_id)}, "
        f"сообщений: {await _count(Message, Message.deal_id)}."
    )

    gateway = core.services.llm
    kind = "next_step" if payload.kind == "next_step" else "summary"
    text = await (next_step if kind == "next_step" else summarize)(gateway, deal, context)
    model = gateway.model or "mock"

    core.event_bus.emit(
        session,
        f"ai.{kind}.generated",
        {"deal_id": deal_id, "model": model, "actor": "AI", "entity_ref": f"deal:{deal_id}"},
    )
    await session.commit()
    return AiTextOut(kind=kind, text=text, model=model)


@router.post("/calls/{cid}/ai/script", response_model=CallScriptOut)
async def call_ai_script(
    cid: int,
    core: Core = Depends(get_core),
    session: AsyncSession = Depends(get_session),
    _: CurrentUser = Depends(require_permission("sales.deal.read")),
):
    """SALES-54: скрипт результативного звонка по стадии сделки (со-пилот продавца).

    Каркас (цель/целевое действие/тезисы/вопросы) детерминирован по стадии — работает
    и при ВЫКЛЮЧЕННОМ AI (не 503, продавцу всегда нужен скрипт). Включённый AI добавляет
    контекстную подсказку ``ai_hint`` (событие ``ai.call_script.generated`` → audit).
    Звонок не привязан к сделке → плейбук входа воронки.
    """
    from modules.sales.models import CallLog

    call = await session.get(CallLog, cid)
    if call is None:
        raise HTTPException(status_code=404, detail="Звонок не найден")
    deal = await session.get(Deal, call.deal_id) if call.deal_id else None
    stage = deal.stage if deal else None
    play = static_call_script(stage)

    llm = core.services.llm
    ai_hint, model = None, "static"
    if llm.enabled:
        ai_hint = await call_script_hint(llm, deal, play)
        model = llm.model or "mock"
        core.event_bus.emit(
            session,
            "ai.call_script.generated",
            {
                "call_id": cid,
                "deal_id": call.deal_id,
                "stage": stage or "",
                "model": model,
                "actor": "AI",
                "entity_ref": f"call:{cid}",
            },
        )
        await session.commit()
    return CallScriptOut(
        stage=stage or "new",
        goal=play["goal"],
        target_action=play["target_action"],
        talking_points=play["talking_points"],
        questions=play["questions"],
        ai_hint=ai_hint,
        model=model,
    )


@router.post("/calls/{cid}/ai/objection", response_model=ObjectionReplyOut)
async def call_ai_objection(
    cid: int,
    payload: ObjectionReplyIn,
    core: Core = Depends(get_core),
    session: AsyncSession = Depends(get_session),
    _: CurrentUser = Depends(require_permission("sales.deal.read")),
):
    """SALES-54: подсказка ответа на возражение клиента (со-пилот продавца).

    Категория и базовый ответ — детерминированные (работают без AI); включённый AI
    добавляет ``ai_hint`` (событие ``ai.objection.suggested`` → audit).
    """
    from modules.sales.models import CallLog

    call = await session.get(CallLog, cid)
    if call is None:
        raise HTTPException(status_code=404, detail="Звонок не найден")
    if not payload.objection.strip():
        raise HTTPException(status_code=422, detail="Пустое возражение")
    category, reply = classify_objection(payload.objection)

    llm = core.services.llm
    ai_hint, model = None, "static"
    if llm.enabled:
        deal = await session.get(Deal, call.deal_id) if call.deal_id else None
        ai_hint = await objection_hint(llm, payload.objection, deal)
        model = llm.model or "mock"
        core.event_bus.emit(
            session,
            "ai.objection.suggested",
            {
                "call_id": cid,
                "category": category,
                "model": model,
                "actor": "AI",
                "entity_ref": f"call:{cid}",
            },
        )
        await session.commit()
    return ObjectionReplyOut(category=category, reply=reply, ai_hint=ai_hint, model=model)


# --- Окно входящего звонка (SALES-50): SSE-поток, журнал, действия --------------------
@router.get("/calls/stream")
async def calls_stream(
    owner: str | None = None,
    user: CurrentUser = Depends(require_permission("sales.deal.read")),
):
    """SSE-поток карточек звонков продавца (всплывающее окно входящего звонка).

    Подписка по ``owner`` (= ``Deal.owner``, ФИО продавца); по умолчанию — текущий
    пользователь. ponytail: маппинг username↔Deal.owner закроется реальной
    аутентификацией (Keycloak, P1); сейчас фронт передаёт ``?owner=<ФИО>``.
    """
    import asyncio
    import json

    from fastapi.responses import StreamingResponse

    from modules.sales import calls as calls_mod

    # ponytail: остаточный IDOR (security-review HIGH) — ``owner`` самоназначаемый, любой
    # с sales.deal.read подписывается на чужой поток (чужие звонки: номер/контрагент).
    # Закрыть нечем, пока нет аутентифицированной идентичности продавца: в dev X-User не
    # шлётся (user.username == "anonymous"), фича держится на ?owner=<ФИО>. Апгрейд —
    # Keycloak P1 (username↔Deal.owner) → гейт «свой поток / sales.calls.read_all для РОП».
    target = owner or user.username
    queue = calls_mod.subscribe(target)

    async def _gen():
        try:
            yield ": connected\n\n"
            while True:
                try:
                    card = await asyncio.wait_for(queue.get(), timeout=15.0)
                    yield f"data: {json.dumps(card, ensure_ascii=False)}\n\n"
                except asyncio.TimeoutError:
                    yield ": ping\n\n"  # heartbeat против обрыва простаивающего соединения
        finally:
            calls_mod.unsubscribe(target, queue)

    return StreamingResponse(_gen(), media_type="text/event-stream")


@router.get("/calls", response_model=list[CallOut])
async def list_calls(
    status: str | None = None,
    owner: str | None = None,
    date: str | None = None,
    session: AsyncSession = Depends(get_session),
    _: CurrentUser = Depends(require_permission("sales.deal.read")),
):
    """Журнал звонков с фильтрами: статус / продавец / дата (``YYYY-MM-DD``)."""
    from modules.sales.models import CallLog

    stmt = select(CallLog).order_by(CallLog.started_at.desc())
    if status:
        stmt = stmt.where(CallLog.status == status)
    if owner:
        stmt = stmt.where(CallLog.owner == owner)
    if date:
        try:
            day = datetime.fromisoformat(date)  # param `date` затеняет datetime.date — берём datetime
        except ValueError:
            raise HTTPException(status_code=400, detail="date: ожидается YYYY-MM-DD")
        start = datetime(day.year, day.month, day.day)
        stmt = stmt.where(CallLog.started_at >= start, CallLog.started_at < start + timedelta(days=1))
    return (await session.execute(stmt)).scalars().all()


@router.get("/calls/{cid}", response_model=CallOut)
async def get_call(
    cid: int,
    session: AsyncSession = Depends(get_session),
    _: CurrentUser = Depends(require_permission("sales.deal.read")),
):
    """Карточка одного звонка."""
    from modules.sales.models import CallLog

    call = await session.get(CallLog, cid)
    if call is None:
        raise HTTPException(status_code=404, detail="Звонок не найден")
    return call


@router.post("/calls/{cid}/comment", response_model=CallOut)
async def call_comment(
    cid: int,
    payload: CallCommentIn,
    session: AsyncSession = Depends(get_session),
    _: CurrentUser = Depends(require_permission("sales.deal.write")),
):
    """Заметка по звонку."""
    from modules.sales.models import CallLog

    call = await session.get(CallLog, cid)
    if call is None:
        raise HTTPException(status_code=404, detail="Звонок не найден")
    call.comment = payload.comment
    await session.commit()
    return call


@router.post("/calls/{cid}/result", response_model=CallOut)
async def call_result(
    cid: int,
    payload: CallResultIn,
    session: AsyncSession = Depends(get_session),
    _: CurrentUser = Depends(require_permission("sales.deal.write")),
):
    """Отметить итог/классификацию звонка."""
    from modules.sales.models import CallLog

    call = await session.get(CallLog, cid)
    if call is None:
        raise HTTPException(status_code=404, detail="Звонок не найден")
    call.result = payload.result
    await session.commit()
    return call


@router.post("/calls/{cid}/link-deal", response_model=CallOut)
async def call_link_deal(
    cid: int,
    payload: CallLinkDealIn,
    session: AsyncSession = Depends(get_session),
    core: Core = Depends(get_core),
    _: CurrentUser = Depends(require_permission("sales.deal.write")),
):
    """Привязать звонок к существующей сделке (``deal_id``) или создать новую (``create``)."""
    from modules.sales.models import CallLog

    # ponytail: остаточный риск authz (security-review MED) — нет проверки call.owner ==
    # вызывающий, любой с sales.deal.write привязывает/создаёт сделку по чужому звонку.
    # Тот же блокер, что у /calls/stream: в dev нет идентичности (user.username ==
    # "anonymous"), сверять не с чем. Апгрейд — Keycloak P1 → проверка владельца + set owner.
    call = await session.get(CallLog, cid)
    if call is None:
        raise HTTPException(status_code=404, detail="Звонок не найден")
    if payload.deal_id is not None:
        deal = await session.get(Deal, payload.deal_id)
        if deal is None:
            raise HTTPException(status_code=404, detail="Сделка не найдена")
        call.deal_id = deal.id
    elif payload.create:
        cp_name = ""
        if call.counterparty_id is not None:
            cp = await session.get(Counterparty, call.counterparty_id)
            cp_name = cp.name if cp is not None else ""
        deal = Deal(
            number=f"CRM-CALL-{call.id}",
            title=f"Звонок {call.phone_e164 or call.call_id}",
            counterparty=cp_name or (call.phone_e164 or "Неизвестный номер"),
            owner=call.owner,
            stage="new",
        )
        session.add(deal)
        try:
            await session.flush()
        except IntegrityError:
            await session.rollback()
            raise HTTPException(status_code=409, detail="Сделка по этому звонку уже создана")
        call.deal_id = deal.id
        core.event_bus.emit(session, "sales.deal.created", {"number": deal.number, "title": deal.title})
    else:
        raise HTTPException(status_code=400, detail="Укажите deal_id или create=true")
    await session.commit()
    return call


@router.post("/telephony/incoming")
async def telephony_incoming(
    payload: TelephonyEventIn,
    session: AsyncSession = Depends(get_session),
    core: Core = Depends(get_core),
    _: CurrentUser = Depends(require_permission("sales.deal.write")),
):
    """Прямой приём нормализованного события звонка (fallback/тест, если не через шину).

    Обрабатывает синхронно (апсерт записи + push карточки), минуя задержку relay.
    Тело строго валидируется (``TelephonyEventIn``: только известные поля, проверенные
    типы) — недоверенный вызов не может фабриковать ``CallLog`` (security-review HIGH).
    """
    from core.services.eventbus import EventContext
    from modules.sales import calls as calls_mod

    event_type = payload.event_type
    handler = calls_mod.EVENT_HANDLERS.get(event_type)
    if handler is None:
        raise HTTPException(status_code=400, detail=f"Неизвестный тип события: {event_type}")
    data = payload.model_dump()
    await handler(data, EventContext(session=session, services=core.services))
    await session.commit()
    return {"ok": True, "event_type": event_type, "call_id": payload.call_id}


# ── ROP план/факт менеджеров ───────────────────────────────────────────────
@router.get("/rop/plan-fact", tags=["sales"])
async def rop_plan_fact(
    period: str = "",
    session: AsyncSession = Depends(get_session),
    _: CurrentUser = Depends(require_permission("sales.deal.read")),
):
    """Plan/Fact по менеджерам за месяц (YYYY-MM). Demo-план, если KpiTarget пуст."""
    import calendar

    from modules.sales.models import Deal, KpiTarget

    if not period:
        today = date.today()
        period = today.strftime("%Y-%m")

    try:
        year, month = int(period[:4]), int(period[5:7])
    except (ValueError, IndexError):
        raise HTTPException(status_code=400, detail="period must be YYYY-MM")

    first_day = date(year, month, 1)
    last_day = date(year, month, calendar.monthrange(year, month)[1])

    rows = (await session.execute(
        select(Deal.owner, func.count(Deal.id), func.sum(Deal.amount))
        .where(Deal.stage == "won")
        .where(func.date(Deal.stage_changed_at) >= first_day)
        .where(func.date(Deal.stage_changed_at) <= last_day)
        .group_by(Deal.owner)
    )).all()

    # Попробуем взять планы из KpiTarget (поле revenue_plan на менеджера)
    targets_row = (await session.execute(
        select(KpiTarget).where(KpiTarget.key == "plan_revenue_per_manager")
    )).scalars().first()
    plan_revenue_default = float(targets_row.target) if targets_row else 5000000.0
    plan_deals_default = 5

    managers = [
        {
            "name": owner or "Менеджер",
            "plan_deals": plan_deals_default,
            "fact_deals": int(cnt),
            "plan_revenue": plan_revenue_default,
            "fact_revenue": float(total or 0),
            "conversion_pct": round(int(cnt) / plan_deals_default * 100, 1),
        }
        for owner, cnt, total in rows
    ]

    return {"period": period, "managers": managers, "demo_plans": targets_row is None}
