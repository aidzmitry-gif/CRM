"""Explicit, immutable book ownership of a CRM deal; legacy routes are separate."""
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, event, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, mapped_column

from config.access import is_package_allowed
from core.db.base import Base
from core.runtime.deps import get_core, get_session
from core.services.auth import get_current_user
from modules.sales.access import DealAccess, get_deal_access, scope_deals
from modules.sales.models import Deal, DealDocument


class DealOwnership(Base):
    __tablename__ = "deal_ownership"
    __table_args__ = {"schema": "sales"}
    deal_id: Mapped[int] = mapped_column(ForeignKey("sales.deal.id"), primary_key=True)
    organization_id: Mapped[int] = mapped_column(Integer, index=True)
    snapshot: Mapped[dict] = mapped_column(JSON)
    evidence: Mapped[str] = mapped_column(String(1000))
    actor: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


def immutable(*args):
    raise ValueError("Confirmed sales organization cannot be changed or deleted")


event.listen(DealOwnership, "before_update", immutable)
event.listen(DealOwnership, "before_delete", immutable)


class ClaimInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    expected_snapshot: dict
    evidence: str = Field(min_length=1, max_length=1000)


async def transaction(session=Depends(get_session)):
    try:
        yield session
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(409, "Concurrent sales organization assignment") from exc
    except ValueError as exc:
        await session.rollback()
        raise HTTPException(422, str(exc)) from exc
    except Exception:
        await session.rollback()
        raise


async def context(org_id: int, session=Depends(transaction, scope="function"), core=Depends(get_core), user=Depends(get_current_user)):
    if not is_package_allowed("sales", user.roles):
        raise HTTPException(403, "Sales access required")
    gateway = getattr(core.services, "accounting", None)
    if gateway is None:
        raise HTTPException(503, "Organization service is unavailable")
    actor = await gateway.source_owner_authority(session, org_id, user)
    return session, actor


async def snapshot(session, deal_id, access):
    deal = await session.scalar(scope_deals(select(Deal).where(Deal.id == deal_id), access).with_for_update())
    if deal is None:
        raise HTTPException(404, "Deal not found")
    documents = (await session.scalars(select(DealDocument).where(DealDocument.deal_id == deal_id).order_by(DealDocument.id).with_for_update())).all()
    return {"deal_id": deal.id, "number": deal.number, "counterparty": deal.counterparty,
            "owner_id": deal.owner_id,
            "documents": [{"id": d.id, "version": d.version, "number": d.number,
                           "kind": d.kind, "amount": str(d.amount), "content_sha256": d.content_sha256,
                           "superseded_by_id": d.superseded_by_id} for d in documents]}


router = APIRouter(tags=["Юрлица документов продаж"])


@router.get("/organizations/{org_id}/deals/{deal_id}/ownership-preview")
async def preview(org_id: int, deal_id: int, ctx=Depends(context), access: DealAccess = Depends(get_deal_access)):
    current = await snapshot(ctx[0], deal_id, access)
    owner = await ctx[0].get(DealOwnership, deal_id)
    if owner is not None and owner.organization_id != org_id:
        raise HTTPException(404, "Deal not found in this organization")
    return {"snapshot": current, "assigned": owner is not None}


@router.post("/organizations/{org_id}/deals/{deal_id}/ownership", status_code=201)
async def claim(org_id: int, deal_id: int, data: ClaimInput, ctx=Depends(context), access: DealAccess = Depends(get_deal_access)):
    session, actor = ctx
    current = await snapshot(session, deal_id, access)
    owner = await session.get(DealOwnership, deal_id)
    if owner is not None:
        if owner.organization_id != org_id:
            raise HTTPException(404, "Deal not found in this organization")
        if owner.snapshot != data.expected_snapshot or owner.evidence != data.evidence:
            raise HTTPException(409, "Ownership already confirmed with different evidence")
    else:
        if current != data.expected_snapshot:
            raise HTTPException(409, "Deal or documents changed; review again")
        owner = DealOwnership(deal_id=deal_id, organization_id=org_id, snapshot=current, evidence=data.evidence, actor=actor)
        session.add(owner)
        await session.flush()
    return {"deal_id": owner.deal_id, "organization_id": owner.organization_id,
            "snapshot": owner.snapshot, "evidence": owner.evidence, "actor": owner.actor}
