"""Объявляемые модулем Sales роли и разрешения RBAC.

Реальная проверка прав появится в части 5 (Keycloak). Здесь модуль лишь
декларирует, какие права и роли он вводит.
"""
from __future__ import annotations

from core.runtime.contract import Permission, Role

PERMISSIONS = [
    Permission("sales.deal.read", "Просмотр сделок"),
    Permission("sales.deal.write", "Создание и изменение сделок"),
    Permission("sales.deal.approve", "Согласование документов сделки"),
]

ROLES = [
    Role("Менеджер", ("sales.deal.read", "sales.deal.write")),
    Role("РОП", ("sales.deal.read", "sales.deal.write", "sales.deal.approve")),
]
