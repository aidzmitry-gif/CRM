"""Процессы модуля Sales (Temporal). На этапе каркаса — пустой workflow.

В части 9 здесь появится согласование документа сделки (договор уходит юристу
через Temporal из части 4). Сейчас — заглушка, демонстрирующая регистрацию
процесса в ядре.
"""
from __future__ import annotations

from core.services.temporal import Workflow


class DealApprovalWorkflow(Workflow):
    """Согласование документа сделки (счёт/договор). Тело — в части 9."""

    name = "DealApproval"

    async def run(self, deal_id: int) -> None:
        # Заглушка: реальная логика согласования — часть 9 поверх Temporal (часть 4)
        raise NotImplementedError
