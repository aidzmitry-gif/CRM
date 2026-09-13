"""Scoped, transactional client/contact lookup without MDM writes."""
from fastapi import HTTPException
from sqlalchemy import select

from core.services.crm_clients import CrmClientLink
from modules.sales.clients import visible_client
from modules.sales.models import CrmClientContact


class SalesCrmClientLookup:
    async def resolve_link(self, session, *, actor, client_id, contact_id,
                           expected_owner_id, lock=False):
        client = await visible_client(session, client_id, actor, lock=bool(lock), shared=lock == "shared")
        if client.owner_id != expected_owner_id:
            raise HTTPException(422, "Владелец клиента не совпадает с владельцем лида")
        contact = None
        if contact_id is not None:
            stmt = select(CrmClientContact).where(
                CrmClientContact.id == contact_id, CrmClientContact.crm_client_id == client.id)
            if lock:
                stmt = stmt.with_for_update(read=lock == "shared").execution_options(populate_existing=True)
            contact = (await session.execute(stmt)).scalar_one_or_none()
            if contact is None:
                raise HTTPException(404, "Контакт клиента не найден")
        return CrmClientLink(client.id, contact_id, client.owner_id, client.name, client.unp,
                             contact.full_name if contact else None,
                             contact.phone if contact else None, contact.email if contact else None)
