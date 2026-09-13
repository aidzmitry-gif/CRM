"""Authenticated sender identity and the immutable sales signature."""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.domain.models import User
from core.services.auth import CurrentUser

SIGNATURE_DEPARTMENT = "Отдел продаж microchips.by"
SIGNATURE_EMAIL = "order@microchips.by"


@dataclass(frozen=True)
class MailProfile:
    actor: str
    full_name: str
    signature: str

    @property
    def preview(self) -> str:
        return self.signature


def actor_identity(user: CurrentUser, settings) -> str:
    if user.keycloak_user_id and user.keycloak_user_id.strip():
        return user.keycloak_user_id.strip()
    if getattr(settings, "auth_mode", "dev") == "dev" and user.username.strip():
        return user.username.strip()
    raise HTTPException(503, "Профиль отправителя не настроен")


async def resolve_profile(session: AsyncSession, user: CurrentUser, settings) -> MailProfile:
    actor = actor_identity(user, settings)
    identity_filter = (
        User.keycloak_user_id == actor
        if user.keycloak_user_id and user.keycloak_user_id.strip()
        else User.username == actor
    )
    profile = await session.scalar(select(User).where(identity_filter))
    full_name = (profile.full_name if profile else "").strip() if profile else ""
    if profile is None or profile.status != "active" or not full_name:
        raise HTTPException(503, "Профиль отправителя не настроен")
    signature = f"{full_name}\n{SIGNATURE_DEPARTMENT}\n{SIGNATURE_EMAIL}"
    return MailProfile(actor=actor, full_name=full_name, signature=signature)


def append_signature(body: str, profile: MailProfile) -> str:
    text = body.rstrip()
    return f"{text}\n\n{profile.signature}" if text else profile.signature
