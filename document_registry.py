"""Real paginated register. Imported observations stay held and read-only."""
import hashlib
import json
from decimal import Decimal
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from sqlalchemy import JSON, String, cast, exists, func, or_, select
from sqlalchemy.orm import aliased

from config.access import is_super
from core.domain.document_source import DocumentSourceRevision as Source
from core.domain.models import CatalogSourceIdentity, Counterparty, CounterpartyAlias, Sku, SyncLink
from core.runtime.deps import get_core, get_session
from core.services.auth import require_permission
from modules.sales.access import get_deal_access, scope_deals
from modules.sales.accounting_ownership import DealOwnership
from modules.sales.document_register import serialize
from modules.sales.models import Deal, DealDocument

router = APIRouter(tags=["Реестр документов"])
SALES_ENTITIES = frozenset('Document_'+name for name in (
    'ЗаказКлиента', 'СчетНаОплатуКлиенту', 'РеализацияТоваровУслуг',
    'ВозвратТоваровОтКлиента', 'ЗаявкаНаВозвратТоваровОтКлиента',
    'СчетФактураВыданный', 'ТранспортнаяНакладная', 'АктВыполненныхРабот',
))
# Accounting calculations and journal operations are source observations, not
# business documents. Keep them in the source store, outside this register.
ACCOUNTING_OPERATIONS = frozenset('Document_'+name for name in (
    'АмортизацияОС2_4', 'ОперацияБух', 'ОтражениеЗарплатыВФинансовомУчете',
    'ПрочиеДоходыРасходы', 'РаспределениеДоходовПоНаправлениямДеятельности',
    'РаспределениеПрочихЗатрат', 'РасчетКурсовыхРазниц',
    'РасчетСебестоимостиТоваров', 'РегламентнаяОперация',
))
LINE_FIELDS = ('НомерСтроки', 'Количество', 'Цена', 'Сумма', 'СуммаНДС', 'СтавкаНДС',
               'Содержание', 'ЕдиницаИзмерения', 'СуммаВзаиморасчетов')


async def context(org_id: int = Path(gt=0), session=Depends(get_session), core=Depends(get_core),
                  user=Depends(require_permission('sales.deal.read')), access=Depends(get_deal_access)):
    gateway = getattr(core.services, 'accounting', None)
    if gateway is None:
        raise HTTPException(503, 'Сервис организаций недоступен')
    try:
        await gateway.source_member(session, org_id, user)
        yield session, user, access
    finally:
        await session.rollback()


async def source_binding(session, org_id, access):
    if access.own_only:
        raise HTTPException(403, 'Импортированные документы ещё не связаны с личными сделками')
    links = (await session.scalars(select(SyncLink).where(
        SyncLink.entity_type == 'organization', SyncLink.entity_id == org_id,
        SyncLink.system == '1c', SyncLink.origin == '1c', SyncLink.direction == 'in',
        SyncLink.state == 'synced',
    ))).all()
    if len(links) != 1 or not links[0].external_ref:
        raise HTTPException(409, 'Связь организации 1С с бухгалтерской книгой требует подтверждения')
    try:
        if str(UUID(links[0].external_ref)) != links[0].external_ref:
            raise ValueError
    except ValueError:
        raise HTTPException(409, 'Некорректная связь организации 1С') from None
    return links[0].external_ref


def source_query(guid, user):
    later = aliased(Source)
    query = select(Source).where(
        Source.source_scope == 'BOUND_ORGANIZATION',
        Source.organization_sha256 == hashlib.sha256(guid.encode()).hexdigest(),
        Source.entity.not_in(ACCOUNTING_OPERATIONS),
        ~exists(select(later.id).where(later.namespace == Source.namespace,
                                      later.entity == Source.entity,
                                      later.source_guid == Source.source_guid,
                                      later.revision_no > Source.revision_no)),
    )
    if not is_super(user.roles):
        query = query.where(Source.entity.in_(SALES_ENTITIES))
    return query


def parsed_source(row, guid):
    if (hashlib.sha256(row.source_text.encode()).hexdigest() != row.source_sha
            or row.organization_sha256 != hashlib.sha256(guid.encode()).hexdigest()
            or row.source_scope != 'BOUND_ORGANIZATION'):
        raise HTTPException(409, 'Нарушена целостность исходного документа')
    value = json.loads(row.source_text, parse_float=Decimal)
    if (value.get('Организация_Key') != guid or value.get('Ref_Key') != row.source_guid
            or value.get('DataVersion') != row.data_version):
        raise HTTPException(409, 'Исходный документ не соответствует организации или версии')
    return value


def scalar(value):
    return str(value) if isinstance(value, (str, int, Decimal)) and not isinstance(value, bool) else None


async def counterparties(session, values):
    refs = {v.get('Контрагент_Key') for v in values if isinstance(v.get('Контрагент_Key'), str)}
    if not refs:
        return {}
    rows = (await session.execute(select(CounterpartyAlias.external_ref, Counterparty.id, Counterparty.name).join(
        Counterparty, Counterparty.id == CounterpartyAlias.counterparty_id,
    ).where(CounterpartyAlias.source == '1c', CounterpartyAlias.external_ref.in_(refs)))).all()
    return {ref: {'id': id_, 'name': name} for ref, id_, name in rows}


@router.get('/organizations/{org_id}/document-registry')
async def registry(org_id: int, source: Literal['erp', '1c'] = '1c',
                   page: int = Query(1, ge=1, le=100000), limit: int = Query(50, ge=1, le=100),
                   q: str = Query('', max_length=100), kind: str = Query('', max_length=128),
                   date_from: str = Query('', pattern=r'^$|^\d{4}-\d{2}-\d{2}$'),
                   date_to: str = Query('', pattern=r'^$|^\d{4}-\d{2}-\d{2}$'),
                   ctx=Depends(context, scope='function')):
    session, user, access = ctx
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, 'Начало периода позже окончания')
    if source == '1c':
        guid = await source_binding(session, org_id, access)
        query = source_query(guid, user)
        payload = cast(Source.source_text, JSON)
        if kind:
            query = query.where(Source.entity == kind)
        if q.strip():
            matching_clients = select(CounterpartyAlias.external_ref).join(
                Counterparty, Counterparty.id == CounterpartyAlias.counterparty_id,
            ).where(CounterpartyAlias.source == '1c', Counterparty.name.icontains(q.strip(), autoescape=True))
            query = query.where(or_(payload['Number'].as_string().icontains(q.strip(), autoescape=True),
                                   payload['Контрагент_Key'].as_string().in_(matching_clients)))
        if date_from:
            query = query.where(func.substr(payload['Date'].as_string(), 1, 10) >= date_from)
        if date_to:
            query = query.where(func.substr(payload['Date'].as_string(), 1, 10) <= date_to)
        total = await session.scalar(select(func.count()).select_from(query.subquery()))
        rows = (await session.scalars(query.order_by(payload['Date'].as_string().desc(), Source.id.desc())
                                     .offset((page-1)*limit).limit(limit))).all()
        values = [parsed_source(row, guid) for row in rows]
        parties = await counterparties(session, values)
        items = [dict(id=row.id, source='1c', organization_id=org_id, kind=row.entity,
                      number=scalar(v.get('Number')) or 'Без номера', date=scalar(v.get('Date')),
                      amount=scalar(v.get('СуммаДокумента')), currency=scalar(v.get('Валюта')),
                      status='SOURCE_ONLY', revision=row.revision_no,
                      client=parties.get(v.get('Контрагент_Key')), deal_id=None,
                      original_available=False) for row, v in zip(rows, values)]
        kinds = list((await session.scalars(source_query(guid, user).with_only_columns(Source.entity)
                                           .distinct().order_by(Source.entity))).all())
    else:
        query = scope_deals(select(DealDocument).join(Deal, Deal.id == DealDocument.deal_id).join(
            DealOwnership, DealOwnership.deal_id == Deal.id,
        ).where(DealOwnership.organization_id == org_id), access)
        if kind:
            query = query.where(DealDocument.kind == kind)
        if q.strip():
            query = query.where(or_(DealDocument.number.icontains(q.strip(), autoescape=True),
                                   Deal.counterparty.icontains(q.strip(), autoescape=True)))
        day = cast(DealDocument.created_at, String)
        if date_from:
            query = query.where(func.substr(day, 1, 10) >= date_from)
        if date_to:
            query = query.where(func.substr(day, 1, 10) <= date_to)
        total = await session.scalar(select(func.count()).select_from(query.subquery()))
        rows = (await session.scalars(query.order_by(DealDocument.created_at.desc(), DealDocument.id.desc())
                                     .offset((page-1)*limit).limit(limit))).all()
        items = [dict(serialize(row, org_id), source='erp', date=row.created_at, revision=row.version,
                      client=None) for row in rows]
        kinds = ['invoice', 'contract', 'order']
    return dict(organization_id=org_id, source=source, page=page, limit=limit, total=total,
                pages=max(1, (total+limit-1)//limit), items=items, kinds=kinds)


@router.get('/organizations/{org_id}/document-registry/source/{doc_id}')
async def source_preview(org_id: int, doc_id: int = Path(gt=0),
                         line_page: int = Query(1, ge=1, le=100000),
                         ctx=Depends(context, scope='function')):
    session, user, access = ctx
    guid = await source_binding(session, org_id, access)
    row = await session.scalar(source_query(guid, user).where(Source.id == doc_id))
    if row is None:
        raise HTTPException(404, 'Документ не найден в этой организации')
    value = parsed_source(row, guid)
    parties = await counterparties(session, [value])
    lines = value.get('Товары', [])
    if not isinstance(lines, list):
        lines = []
    selected = lines[(line_page-1)*100:line_page*100]
    refs = {line['Номенклатура_Key'] for line in selected if isinstance(line, dict)
            and isinstance(line.get('Номенклатура_Key'), str)}
    names = dict((await session.execute(select(CatalogSourceIdentity.source_guid, Sku.title).join(
        Sku, Sku.id == CatalogSourceIdentity.sku_id,
    ).where(CatalogSourceIdentity.namespace == row.namespace,
            CatalogSourceIdentity.entity == 'Catalog_Номенклатура',
            CatalogSourceIdentity.source_guid.in_(refs)))).all()) if refs else {}
    items = []
    for line in selected:
        if not isinstance(line, dict):
            continue
        ref = line.get('Номенклатура_Key')
        fields = {key: scalar(line[key]) for key in LINE_FIELDS if key in line and scalar(line[key]) is not None}
        items.append(dict(name=names.get(ref) or scalar(line.get('Номенклатура')) or 'Номенклатура не сопоставлена',
                          fields=fields))
    return dict(id=row.id, organization_id=org_id, source='1c', kind=row.entity,
                number=scalar(value.get('Number')), date=scalar(value.get('Date')),
                amount=scalar(value.get('СуммаДокумента')), currency=scalar(value.get('Валюта')),
                client=parties.get(value.get('Контрагент_Key')), source_sha256=row.source_sha,
                revision=row.revision_no, source_guid=row.source_guid,
                status='SOURCE_ONLY', holds=row.holds, native_posted=False, original_available=False,
                source_posted=value.get('Posted'), source_deleted=value.get('DeletionMark'),
                lines=items, line_page=line_page, line_total=len(lines),
                line_pages=max(1, (len(lines)+99)//100),
                explanation='Предпросмотр сохранённых данных 1С. Не является оригиналом, проведением или накладной ERP. Кадровые и банковские поля в этот просмотр не включены.')
