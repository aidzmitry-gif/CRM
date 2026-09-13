"""Owned CRM clients are independent of shared master-data counterparties."""
from __future__ import annotations

import hashlib
import json

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import func, literal, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from core.runtime.deps import get_session
from core.services.auth import CurrentUser, require_permission
from core.services.crm_owners import list_crm_owners, resolve_crm_owner
from modules.sales.access import DealAccess, get_deal_access, require_owner_assignment, scope_deals
from modules.sales.models import CrmClient, Deal, DealDocument
from modules.sales.schemas import DocumentOut

router = APIRouter()


class ClientCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=255)
    unp: str | None = Field(default=None, pattern=r"^[0-9]{9}$")
    owner_id: int | None = Field(default=None, gt=0)
    request_key: str = Field(min_length=8, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")

    @field_validator("name")
    @classmethod
    def normalized_name(cls, value: str) -> str:
        value = " ".join(value.split())
        if not value:
            raise ValueError("Введите название клиента")
        return value


def scope_clients(stmt, access):
    return stmt.where(CrmClient.owner_id == access.employee_id) if access.own_only else stmt


def client_rows(access):
    deal_id = scope_deals(select(func.min(Deal.id)).where(
        Deal.crm_client_id == CrmClient.id,
    ), access).correlate(CrmClient).scalar_subquery()
    return scope_clients(select(CrmClient.id, CrmClient.name, CrmClient.unp,
                                literal(True).label("is_active"), deal_id.label("deal_id"),
                                literal("crm").label("source")), access)


async def visible_client(session, client_id, access, *, lock=False):
    stmt = scope_clients(select(CrmClient).where(CrmClient.id == client_id), access)
    if lock:
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    client = (await session.execute(stmt)).scalar_one_or_none()
    if client is None:
        raise HTTPException(404, "Клиент не найден")
    return client


def client_out(client):
    return {"id": client.id, "name": client.name, "unp": client.unp,
            "owner_id": client.owner_id, "counterparty_id": client.counterparty_id,
            "source": "crm"}


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
    return client_out(await visible_client(session, client_id, access))


@router.post("/clients", status_code=201)
async def create_crm_client(
    payload: ClientCreate,
    session: AsyncSession = Depends(get_session),
    user: CurrentUser = Depends(require_permission("sales.deal.write")),
    access: DealAccess = Depends(get_deal_access),
):
    owner_id = require_owner_assignment(access, payload.owner_id)
    owner = await resolve_crm_owner(session, employee_id=owner_id, lock=True)
    normalized = {"name": payload.name, "unp": payload.unp, "owner_id": owner.employee_id}
    digest = hashlib.sha256(json.dumps(normalized, sort_keys=True).encode()).hexdigest()

    async def replay():
        old = await session.scalar(select(CrmClient).where(
            CrmClient.owner_id == owner.employee_id, CrmClient.request_key == payload.request_key))
        if old and old.request_hash != digest:
            raise HTTPException(409, "Ключ запроса уже использован с другими данными")
        return old

    if old := await replay():
        return client_out(old)
    client = CrmClient(**normalized, name_key=payload.name.casefold(),
                       request_key=payload.request_key, request_hash=digest, created_by=user.username)
    try:
        async with session.begin_nested():
            session.add(client)
            await session.flush()
    except IntegrityError:
        if old := await replay():
            return client_out(old)
        raise HTTPException(409, "Клиент с таким названием или УНП уже есть у этого менеджера") from None
    await session.commit()
    return client_out(client)


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
