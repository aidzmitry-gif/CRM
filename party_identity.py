"""Resolve stable shared-kernel party IDs without guessing ambiguous legacy names."""
from fastapi import HTTPException
from sqlalchemy import select

from core.domain.models import Counterparty, CounterpartyBranch


async def resolve_new_selection(session, name):
    if not name:
        return None
    rows = (await session.scalars(select(Counterparty).where(
        Counterparty.name == name,
    ).order_by(Counterparty.id).limit(2).with_for_update(read=True)
        .execution_options(populate_existing=True))).all()
    if len(rows) > 1:
        raise HTTPException(409, "Несколько контрагентов с этим названием; выберите по ID")
    if rows:
        return rows[0]
    cp = Counterparty(name=name)
    session.add(cp)
    await session.flush()
    return cp


async def party_for_deal(session, deal, *, create=False):
    if deal.counterparty_id is not None:
        cp = await session.get(Counterparty, deal.counterparty_id)
        if cp is None:
            raise HTTPException(409, "Связанный контрагент не найден; выберите сторону сделки")
    else:
        # Migration already bound the provably unique legacy rows. Repeating name
        # matching later could assign an ambiguous row after another party renames.
        if create:
            raise HTTPException(409, "Контрагент сделки не установлен; выберите или создайте карточку и свяжите её по ID")
        return None
    if cp is not None and create:
        if not cp.is_active or cp.merged_into_id is not None:
            raise HTTPException(409, "Контрагент архивирован или объединён; выберите актуальную сторону")
        deal.counterparty_id = cp.id
    return cp


async def prepare_party_change(session, data, *, deal=None):
    """Validate explicit selection; changing only old text clears any stale binding."""
    if deal is not None and not ({"counterparty", "counterparty_id", "branch_id"} & data.keys()):
        return
    cp_id = data.get("counterparty_id", deal.counterparty_id if deal else None)
    branch_id = data.get("branch_id", deal.branch_id if deal else None)
    name = data.get("counterparty", deal.counterparty if deal else "")
    if name is None or not name.strip():
        raise HTTPException(422, "Название контрагента не может быть пустым")
    if deal is not None and deal.counterparty_id is not None and cp_id is None and name == deal.counterparty:
        raise HTTPException(422, "Нельзя удалить связь с контрагентом без выбора новой стороны")
    if deal is not None and "counterparty_id" not in data and name != deal.counterparty:
        cp_id = None
        branch_id = data.get("branch_id")
    if deal is not None and cp_id != deal.counterparty_id and "branch_id" not in data:
        branch_id = None
    cp = (await session.scalars(select(Counterparty).where(
        Counterparty.id == cp_id,
    ).with_for_update(read=True).execution_options(populate_existing=True))).one_or_none() if cp_id is not None else None
    if cp_id is not None and cp is None:
        raise HTTPException(422, "Выбранный контрагент не найден")
    if cp is None and branch_id is None and (deal is None or name != deal.counterparty):
        cp = await resolve_new_selection(session, name)
    if cp is not None:
        if not cp.is_active or cp.merged_into_id is not None:
            raise HTTPException(409, "Контрагент архивирован или объединён")
        cp_id = cp.id
        # Stable ID is authoritative; untrusted display text cannot identify a party.
        name = cp.display_name or cp.name
    if branch_id is not None:
        branch = (await session.scalars(select(CounterpartyBranch).where(
            CounterpartyBranch.id == branch_id,
        ).with_for_update(read=True).execution_options(populate_existing=True))).one_or_none()
        if cp_id is None or branch is None or branch.legal_entity_id != cp_id or not branch.is_active:
            raise HTTPException(422, "Филиал не принадлежит выбранному активному контрагенту")
    data.update(counterparty=name, counterparty_id=cp_id, branch_id=branch_id)
