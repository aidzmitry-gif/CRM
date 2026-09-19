"""Contacts owned through a numeric CRM client, separate from MDM."""
import hashlib
import json
import re

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from core.runtime.deps import get_session
from core.services.auth import CurrentUser, require_permission
from modules.sales.access import DealAccess, get_deal_access
from modules.sales.clients import visible_client
from modules.sales.models import CrmClientContact

router = APIRouter()


class CrmContactCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    full_name: str = Field(min_length=1, max_length=255)
    phone: str | None = Field(default=None, max_length=64)
    email: str | None = Field(default=None, max_length=255, pattern=r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
    is_primary: bool = False
    request_key: str = Field(min_length=8, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")

    @field_validator("full_name")
    @classmethod
    def name(cls, value):
        value = " ".join(value.split())
        if not value:
            raise ValueError("Введите имя контакта")
        return value

    @field_validator("phone", "email", mode="before")
    @classmethod
    def normalize_channel(cls, value, info):
        if value is None or not isinstance(value, str):
            return value
        value = value.strip()
        if info.field_name == "email":
            return value.casefold() or None
        value = re.sub(r"[\s()\-]", "", value)
        if value and not re.fullmatch(r"\+?[0-9]{3,20}", value):
            raise ValueError("Введите телефон цифрами, с необязательным +")
        return value or None


def contact_out(contact):
    return {"id": contact.id, "crm_client_id": contact.crm_client_id,
            "full_name": contact.full_name, "phone": contact.phone,
            "email": contact.email, "is_primary": contact.is_primary}


async def contact_list(session, client_id, access):
    await visible_client(session, client_id, access)
    rows = (await session.scalars(select(CrmClientContact).where(
        CrmClientContact.crm_client_id == client_id,
    ).order_by(CrmClientContact.is_primary.desc(), CrmClientContact.id))).all()
    return [contact_out(row) for row in rows]


async def create_contact(session, client_id, access, payload):
    await visible_client(session, client_id, access, lock=True)
    data = payload.model_dump(exclude={"request_key"})
    request_hash = hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()
    exact = {k: (v.casefold() if isinstance(v, str) else v) for k, v in data.items() if k != "is_primary"}
    contact_key = hashlib.sha256(json.dumps(exact, sort_keys=True).encode()).hexdigest()

    async def replay():
        old = await session.scalar(select(CrmClientContact).where(
            CrmClientContact.crm_client_id == client_id, CrmClientContact.request_key == payload.request_key))
        if old and old.request_hash != request_hash:
            raise HTTPException(409, "Ключ запроса уже использован с другими данными")
        return old

    if old := await replay():
        return contact_out(old)
    if await session.scalar(select(CrmClientContact.id).where(
        CrmClientContact.crm_client_id == client_id, CrmClientContact.contact_key == contact_key,
    )):
        raise HTTPException(409, "Такой контакт уже есть у клиента")
    contact = CrmClientContact(**data, crm_client_id=client_id, contact_key=contact_key,
                               request_key=payload.request_key, request_hash=request_hash)
    try:
        async with session.begin_nested():
            if payload.is_primary:
                await session.execute(update(CrmClientContact).where(
                    CrmClientContact.crm_client_id == client_id).values(is_primary=False))
            session.add(contact)
            await session.flush()
    except IntegrityError:
        if old := await replay():
            return contact_out(old)
        raise HTTPException(409, "Контакт уже существует или список изменился; обновите его") from None
    await session.commit()
    return contact_out(contact)


@router.get("/clients/{client_id}/contacts")
async def list_crm_client_contacts(client_id: int, session: AsyncSession = Depends(get_session),
    _: CurrentUser = Depends(require_permission("sales.deal.read")), access: DealAccess = Depends(get_deal_access)):
    return await contact_list(session, client_id, access)


@router.post("/clients/{client_id}/contacts", status_code=201)
async def add_crm_client_contact(client_id: int, payload: CrmContactCreate, session: AsyncSession = Depends(get_session),
    _: CurrentUser = Depends(require_permission("sales.deal.write")), access: DealAccess = Depends(get_deal_access)):
    return await create_contact(session, client_id, access, payload)


@router.patch("/clients/{client_id}/contacts/{contact_id}/primary")
async def primary_crm_client_contact(client_id: int, contact_id: int, session: AsyncSession = Depends(get_session),
    _: CurrentUser = Depends(require_permission("sales.deal.write")), access: DealAccess = Depends(get_deal_access)):
    await visible_client(session, client_id, access, lock=True)
    contact = await session.scalar(select(CrmClientContact).where(
        CrmClientContact.id == contact_id, CrmClientContact.crm_client_id == client_id))
    if contact is None:
        raise HTTPException(404, "Контакт не найден")
    await session.execute(update(CrmClientContact).where(
        CrmClientContact.crm_client_id == client_id).values(is_primary=False))
    contact.is_primary = True
    await session.commit()
    return contact_out(contact)
