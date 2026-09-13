"""Объявляемые модулем Sales роли и разрешения RBAC.

Права навешаны на роуты (``routes.py``, ``require_permission``, fail-closed через
``core.services.auth``). ``has_permission`` сопоставляет ``role.name`` с ролями
пользователя, а те приходят РЕАЛЬНЫМИ слагами (``config/access.py``: ``sales_head``/
``sales``/``sales_cli``, не «Менеджер»/«РОП») — поэтому роли объявляем под слаги, иначе
право (``sales.deal.read`` и др.) получал бы только суперюзер (Директор/Коммерческий —
минуют проверку), а продавец/РОП ловили бы 403 на журнале и марже. Тот же приём — в
модуле ``leads`` (``modules/leads/permissions.py``). Полноценный role-mapping — с
Keycloak (часть 5, SECURITY.md).
"""
from __future__ import annotations

from core.runtime.contract import Permission, Role

PERMISSIONS = [
    Permission("sales.deal.read", "Просмотр сделок"),
    Permission("sales.deal.write", "Создание и изменение сделок"),
    Permission("sales.deal.approve", "Согласование документов сделки"),
    Permission("sales.shipping.associate", "Подготовка и подтверждение связи заказа со счётом"),
]

# Слаги ролей — из config/access.py. РОП (sales_head) ведёт сделку целиком, включая
# согласование документов; продавцы (sales) и клиентская работа (sales_cli) — читают и
# правят сделки без права согласования. Директор/Коммерческий — суперроли (минуют это).
ROLES = [
    Role("sales_head", ("sales.deal.read", "sales.deal.write", "sales.deal.approve", "sales.shipping.associate")),
    Role("sales", ("sales.deal.read", "sales.deal.write", "sales.shipping.associate")),
    # Keycloak realm role (go-live): те же права, что у sales — иначе 403 на доске.
    Role("sales_manager", ("sales.deal.read", "sales.deal.write", "sales.shipping.associate")),
    Role("sales_cli", ("sales.deal.read", "sales.deal.write", "sales.shipping.associate")),
]
