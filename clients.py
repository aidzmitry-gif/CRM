"""Owned CRM clients are independent of shared master-data counterparties."""
from __future__ import annotations

import hashlib
import json

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import func, literal, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from core.domain.models import AuditLog, Counterparty, User
from core.runtime.deps import get_core, get_session
from core.services.auth import CurrentUser, has_permission, require_permission
from core.services.crm_owners import CRM_OWNER_ROLES, list_crm_owners, resolve_crm_owner
from modules.sales.access import (
    DealAccess,
    get_deal_access,
    require_owner_assignment,
    scope_counterparties,
    scope_deals,
)
from modules.sales.models import CrmClient, Deal, DealDocument
from modules.sales.schemas import DocumentOut

router = APIRouter()
MANAGER_FIELD = 'crm_responsible_employee_id'


async def manager_labels(session, employee_ids):
    ids = {value for value in employee_ids if type(value) is int and value > 0}
    if not ids:
        return {}
    rows = (await session.execute(select(User.employee_id, User.full_name, User.status,
                                        User.department, User.role).where(User.employee_id.in_(ids)))).all()
    result = {}
    for employee_id in ids:
        accounts = [row for row in rows if row.employee_id == employee_id]
        names = {row.full_name.strip() for row in accounts if row.full_name and row.full_name.strip()}
        result[employee_id] = dict(employee_id=employee_id,
                                  name=next(iter(names)) if len(names) == 1 else f'Сотрудник №{employee_id}',
                                  active=any(row.status == 'active' and row.department == 'Продажи'
                                             and row.role in CRM_OWNER_ROLES for row in accounts))
    return result


async def enrich_client_managers(session, rows):
    """Resolve only the visible page; never infer a client owner from deal names."""
    mdm_ids = [row['id'] for row in rows if row['source'] == 'mdm']
    crm_ids = [row['id'] for row in rows if row['source'] == 'crm']
    owners = {}
    if mdm_ids:
        values = (await session.execute(select(Counterparty.id, Counterparty.requisites)
                                        .where(Counterparty.id.in_(mdm_ids)))).all()
        owners.update({('mdm', id_): (requisites or {}).get(MANAGER_FIELD) for id_, requisites in values})
    if crm_ids:
        values = (await session.execute(select(CrmClient.id, CrmClient.owner_id)
                                        .where(CrmClient.id.in_(crm_ids)))).all()
        owners.update({('crm', id_): owner for id_, owner in values})
    labels = await manager_labels(session, owners.values())
    return [{**dict(row), 'responsible_manager': labels.get(owner) if type(owner) is int else None}
            for row in rows for owner in [owners.get((row['source'], row['id']))]]


class ResponsibleInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    employee_id: int | None = Field(gt=0, strict=True)
    expected_revision: int = Field(gt=0, strict=True)


async def responsible_counterparty(session, client_id, access, *, lock=False):
    stmt = scope_counterparties(select(Counterparty).where(
        Counterparty.id == client_id, Counterparty.is_active.is_(True), Counterparty.merged_into_id.is_(None)), access)
    if lock:
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    row = await session.scalar(stmt)
    if row is None:
        raise HTTPException(404, 'Клиент не найден')
    return row


async def responsible_out(session, client, can_edit):
    owner = (client.requisites or {}).get(MANAGER_FIELD)
    labels = await manager_labels(session, [owner])
    return dict(client_id=client.id, source='mdm', revision=client.revision,
                responsible_manager=labels.get(owner) if type(owner) is int else None, can_edit=can_edit)


@router.get('/counterparties/{client_id}/responsible')
async def get_counterparty_responsible(client_id: int, session: AsyncSession = Depends(get_session),
        user: CurrentUser = Depends(require_permission('sales.deal.read')),
        access: DealAccess = Depends(get_deal_access), core=Depends(get_core)):
    client = await responsible_counterparty(session, client_id, access)
    return await responsible_out(session, client, not access.own_only and has_permission(core, user, 'sales.deal.write'))


@router.put('/counterparties/{client_id}/responsible')
async def set_counterparty_responsible(client_id: int, payload: ResponsibleInput,
        session: AsyncSession = Depends(get_session),
        user: CurrentUser = Depends(require_permission('sales.deal.write')),
        access: DealAccess = Depends(get_deal_access)):
    if access.own_only:
        raise HTTPException(403, 'Назначать менеджера клиента может руководитель с доступом ко всем клиентам')
    client = await responsible_counterparty(session, client_id, access, lock=True)
    if client.revision != payload.expected_revision:
        raise HTTPException(409, 'Карточка клиента изменилась. Обновите её перед сохранением менеджера')
    if payload.employee_id is not None:
        await resolve_crm_owner(session, employee_id=payload.employee_id, lock=True)
    before = (client.requisites or {}).get(MANAGER_FIELD)
    if before != payload.employee_id:
        values = dict(client.requisites or {})
        if payload.employee_id is None:
            values.pop(MANAGER_FIELD, None)
        else:
            values[MANAGER_FIELD] = payload.employee_id
        client.requisites = values
        session.add(AuditLog(actor=user.username, action='counterparty.responsible_manager.changed',
                            entity_ref=f'counterparty:{client.id}',
                            detail=dict(previous_employee_id=before, employee_id=payload.employee_id)))
        await session.commit()
    return await responsible_out(session, client, True)


class ClientCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, max_length=255)
    unp: str | None = Field(default=None, pattern=r"^[0-9]{9}$")
    counterparty_id: int | None = Field(default=None, gt=0, strict=True)
    owner_id: int | None = Field(default=None, gt=0)
    request_key: str = Field(min_length=8, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")

    @field_validator("name")
    @classmethod
    def normalized_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = " ".join(value.split())
        if not value:
            raise ValueError("Введите название клиента")
        return value

    @model_validator(mode="after")
    def require_name_or_counterparty(self):
        if self.name is None and self.counterparty_id is None:
            raise ValueError("Введите название клиента или выберите контрагента")
        return self


def scope_clients(stmt, access):
    return stmt.where(CrmClient.owner_id == access.employee_id) if access.own_only else stmt


def client_rows(access):
    deal_id = scope_deals(select(func.min(Deal.id)).where(
        Deal.crm_client_id == CrmClient.id,
    ), access).correlate(CrmClient).scalar_subquery()
    return scope_clients(select(CrmClient.id, CrmClient.name, CrmClient.unp,
                                literal(True).label("is_active"), deal_id.label("deal_id"),
                                literal("crm").label("source")), access)


async def visible_client(session, client_id, access, *, lock=False, shared=False):
    stmt = scope_clients(select(CrmClient).where(CrmClient.id == client_id), access)
    if lock:
        stmt = stmt.with_for_update(read=shared).execution_options(populate_existing=True)
    client = (await session.execute(stmt)).scalar_one_or_none()
    if client is None:
        raise HTTPException(404, "Клиент не найден")
    return client


async def client_out(session, client):
    labels = await manager_labels(session, [client.owner_id])
    return {"id": client.id, "name": client.name, "unp": client.unp,
            "owner_id": client.owner_id, "counterparty_id": client.counterparty_id,
            "source": "crm", "responsible_manager": labels.get(client.owner_id)}


@router.get("/clients/owners")
async def crm_client_owners(
    session: AsyncSession = Depends(get_session),
    _: CurrentUser = Depends(require_permission("sales.deal.write")),
    access: DealAccess = Depends(get_deal_access),
):
    owners = [await resolve_crm_owner(session, employee_id=access.employee_id)] if access.own_only else await list_crm_owners(session)
    return [{"id": owner.employee_id, "name": owner.full_name} for owner in owners]


@router.get("/clients/{client_id}")
async def get_crm_client(
    client_id: int,
    session: AsyncSession = Depends(get_session),
    _: CurrentUser = Depends(require_permission("sales.deal.read")),
    access: DealAccess = Depends(get_deal_access),
):
    return await client_out(session, await visible_client(session, client_id, access))


@router.post("/clients", status_code=201)
async def create_crm_client(
    payload: ClientCreate,
    session: AsyncSession = Depends(get_session),
    user: CurrentUser = Depends(require_permission("sales.deal.write")),
    access: DealAccess = Depends(get_deal_access),
):
    owner_id = require_owner_assignment(access, payload.owner_id)
    owner = await resolve_crm_owner(session, employee_id=owner_id, lock=True)
    if payload.counterparty_id is not None:
        identity = {"counterparty_id": payload.counterparty_id, "owner_id": owner.employee_id}
    else:
        identity = {"name": payload.name, "unp": payload.unp, "owner_id": owner.employee_id}
    digest = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()

    async def replay():
        old = await session.scalar(select(CrmClient).where(
            CrmClient.owner_id == owner.employee_id, CrmClient.request_key == payload.request_key))
        if old and old.request_hash != digest:
            raise HTTPException(409, "Ключ запроса уже использован с другими данными")
        return old

    if old := await replay():
        return await client_out(session, old)
    if payload.counterparty_id is not None:
        counterparty = await session.scalar(scope_counterparties(select(Counterparty).where(
            Counterparty.id == payload.counterparty_id,
            Counterparty.is_active.is_(True),
            Counterparty.merged_into_id.is_(None),
        ), access).with_for_update(read=True))
        if counterparty is None:
            raise HTTPException(422, "Активный контрагент не найден")
        existing = await session.scalar(select(CrmClient).where(
            CrmClient.owner_id == owner.employee_id,
            CrmClient.counterparty_id == counterparty.id,
        ).order_by(CrmClient.id))
        if existing:
            return await client_out(session, existing)
        name, unp = counterparty.name, counterparty.unp
    else:
        name, unp = payload.name, payload.unp
    assert name is not None  # Guaranteed by ClientCreate for unlinked requests.
    client = CrmClient(name=name, unp=unp, owner_id=owner.employee_id,
                       counterparty_id=payload.counterparty_id, name_key=name.casefold(),
                       request_key=payload.request_key, request_hash=digest, created_by=user.username)
    try:
        async with session.begin_nested():
            session.add(client)
            await session.flush()
    except IntegrityError:
        if old := await replay():
            return await client_out(session, old)
        if payload.counterparty_id is not None:
            existing = await session.scalar(select(CrmClient).where(
                CrmClient.owner_id == owner.employee_id,
                CrmClient.counterparty_id == payload.counterparty_id,
            ).order_by(CrmClient.id))
            if existing:
                return await client_out(session, existing)
        raise HTTPException(409, "Клиент с таким названием или УНП уже есть у этого менеджера") from None
    await session.commit()
    return await client_out(session, client)


@router.get("/clients/{client_id}/deals")
async def list_crm_client_deals(client_id: int, session: AsyncSession = Depends(get_session),
    _: CurrentUser = Depends(require_permission("sales.deal.read")), access: DealAccess = Depends(get_deal_access)):
    await visible_client(session, client_id, access)
    rows = (await session.execute(scope_deals(select(Deal.id, Deal.number, Deal.title, Deal.amount).where(
        Deal.crm_client_id == client_id), access).order_by(Deal.id.desc()))).mappings().all()
    return [dict(row) for row in rows]


class ClientDocumentOut(DocumentOut):
    deal_id: int
    deal_number: str


@router.get("/clients/{client_id}/documents", response_model=list[ClientDocumentOut])
async def list_crm_client_documents(client_id: int, session: AsyncSession = Depends(get_session),
    _: CurrentUser = Depends(require_permission("sales.deal.read")), access: DealAccess = Depends(get_deal_access)):
    await visible_client(session, client_id, access)
    rows = (await session.execute(scope_deals(select(DealDocument, Deal.number).join(
        Deal, Deal.id == DealDocument.deal_id).where(Deal.crm_client_id == client_id), access)
        .order_by(DealDocument.id.desc()))).all()
    return [{**DocumentOut.model_validate(doc).model_dump(), "deal_id": doc.deal_id, "deal_number": number}
            for doc, number in rows]
