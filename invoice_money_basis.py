"""Read-only money basis. Caller holds organization -> deal -> invoice locks.

This evaluator neither authorizes cancellation nor changes paid/reserve state.
Its digest covers observed ledger facts, not completeness of external history.
"""
from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation

from fastapi import HTTPException
from sqlalchemy import select

from modules.sales.accounting_ownership import DealOwnership
from modules.sales.documents import original
from modules.sales.invoice_settlements import InvoiceSettlement, settlement_ready
from modules.sales.models import DealDocument


def canonical(value) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
                      allow_nan=False)


def amount(value, *, positive=True) -> Decimal:
    if isinstance(value, (bool, float)):
        raise ValueError("Exact money is required")
    result = Decimal(value)
    if (not result.is_finite() or result != result.quantize(Decimal("0.01"))
            or result < 0 or (positive and result == 0)):
        raise ValueError("Invalid money")
    return result


def bank_fact(value, entry_id, document_id):
    if (value["entry_id"] != entry_id or value["direction"] not in {"receipt", "refund"}
            or value["currency"] != "BYN" or not value["digest"]
            or not value["statement_reference"]
            or value["settlement_dimensions"].get("settlement_document")
            != f"sales:document:{document_id}"):
        raise ValueError("Bank identity mismatch")
    date.fromisoformat(value["operation_date"])
    return {**value, "amount": format(amount(value["amount"]), ".2f")}


@dataclass(frozen=True)
class ReceiptRemaining:
    receipt_id: int
    received: Decimal
    refunded: Decimal
    remaining: Decimal


@dataclass(frozen=True)
class InvoiceMoneyBasis:
    money_state: str
    received: Decimal
    refunded: Decimal
    per_receipt_remaining: tuple[ReceiptRemaining, ...]
    blockers: tuple[str, ...]
    facts_json: str
    digest: str
    external_requirements: tuple[str, ...] = (
        "history_scope_required", "reconciliation_required", "fulfillment_required",
    )

    @property
    def money_conditions_met(self) -> bool:
        """Necessary observed-money condition, never cancellation authorization."""
        return self.money_state in {"no_receipts", "fully_refunded"}


async def evaluate_invoice_money_basis(session, organization_id, user, invoice, gateway):
    """Evaluate under caller's existing locks, without commit/rollback or ledger writes.

    Require an active transaction; the caller must acquire org/deal/invoice locks
    BEFORE entry, including for empty sets. SQLAlchemy cannot prove that contract.
    Refresh the exact invoice and allocations to avoid stale identity-map values.
    Gateway.invoice_bank_basis discovers even known but unallocated bank evidence.
    Authorization/network failures propagate; only invalid bank evidence is a blocker.
    """
    if not session.in_transaction():
        raise RuntimeError("Existing organization/deal/invoice transaction required")
    doc = await session.scalar(select(DealDocument).join(
        DealOwnership, DealOwnership.deal_id == DealDocument.deal_id,
    ).where(DealDocument.id == invoice.id, DealOwnership.organization_id == organization_id)
        .with_for_update(of=DealDocument).execution_options(populate_existing=True))
    if doc is None:
        raise HTTPException(404, "Invoice not found in this organization")
    if not await settlement_ready(session, organization_id, doc):
        raise HTTPException(409, "An issued BYN invoice is required")
    try:
        original(doc, issued_only=True)
    except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
        raise HTTPException(409, "Invoice original requires reconciliation") from exc
    identity = {"version": doc.version, "content_sha256": doc.content_sha256,
                "number": doc.number, "amount": format(amount(doc.amount, positive=False), ".2f"),
                "currency": "BYN"}
    # Read all rows on this exact document, so corrupt cross-org facts cannot hide.
    rows = (await session.scalars(select(InvoiceSettlement).where(
        InvoiceSettlement.document_id == doc.id,
    ).order_by(InvoiceSettlement.id).with_for_update()
        .execution_options(populate_existing=True))).all()
    known = await gateway.invoice_bank_basis(session, organization_id, user, doc.id)
    if known.get("organization_id") != organization_id or known.get("document_id") != doc.id:
        raise ValueError("Invoice bank basis gateway scope mismatch")
    errors = set()
    facts = {}
    observed_banks = []
    descriptors = sorted(known["entries"], key=lambda e: e["entry_id"])
    known_ids = [e["entry_id"] for e in descriptors]
    if len(set(known_ids)) != len(known_ids):
        raise ValueError("Duplicate entry in bank basis gateway")
    entries = sorted(set(known_ids) | {r.bank_entry_id for r in rows})
    for entry_id in entries:
        try:
            facts[entry_id] = bank_fact(await gateway.bank_settlement(
                session, organization_id, user, entry_id), entry_id, doc.id)
        except HTTPException as exc:
            if exc.status_code not in {404, 409}:
                raise
            errors.add("bank_evidence_invalid")
            observed_banks.append({"entry_id": entry_id, "invalid_status": exc.status_code})
        except (KeyError, TypeError, ValueError, InvalidOperation):
            errors.add("bank_evidence_invalid")
            observed_banks.append({"entry_id": entry_id, "invalid_shape": True})
        else:
            observed_banks.append(facts[entry_id])
    for descriptor in descriptors:
        if descriptor.get("blockers"):
            errors.add("bank_evidence_invalid")
        discovered = descriptor.get("fact")
        if discovered is not None and descriptor["entry_id"] in facts:
            try:
                match = bank_fact(discovered, descriptor["entry_id"], doc.id)
                if canonical(match) != canonical(facts[descriptor["entry_id"]]):
                    errors.add("bank_basis_changed")
            except (KeyError, TypeError, ValueError, InvalidOperation):
                errors.add("bank_evidence_invalid")

    receipts, refunds, used = {}, [], defaultdict(lambda: Decimal("0"))
    frozen_rows = []
    for row in sorted(rows, key=lambda r: r.id):
        frozen_rows.append({"id": row.id, "organization_id": row.organization_id,
                            "document_id": row.document_id, "bank_entry_id": row.bank_entry_id,
                            "direction": row.direction, "amount": str(row.amount),
                            "refund_of": row.refund_of, "snapshot": row.snapshot,
                            "source_key": row.source_key, "evidence": row.evidence,
                            "actor": row.actor})
        try:
            qty = amount(row.amount)
            snap = dict(row.snapshot["invoice"])
            snap["amount"] = format(amount(snap["amount"], positive=False), ".2f")
            if row.organization_id != organization_id or row.document_id != doc.id:
                errors.add("settlement_scope_mismatch")
                continue
            if snap != identity:
                errors.add("invoice_version_mismatch")
            current = facts.get(row.bank_entry_id)
            if current is not None:
                prior = bank_fact(row.snapshot["bank"], row.bank_entry_id, doc.id)
                if canonical(prior) != canonical(current):
                    errors.add("bank_snapshot_mismatch")
                if current["direction"] != row.direction:
                    errors.add("settlement_direction_mismatch")
            used[row.bank_entry_id] += qty
            if row.bank_entry_id not in known_ids:
                errors.add("allocated_bank_missing_from_basis")
            if row.direction == "receipt" and row.refund_of is None:
                receipts[row.id] = (row, qty)
            elif row.direction == "refund" and row.refund_of is not None:
                refunds.append((row, qty))
            else:
                errors.add("settlement_direction_mismatch")
        except (KeyError, TypeError, ValueError, InvalidOperation):
            errors.add("settlement_snapshot_invalid")

    refunded = defaultdict(lambda: Decimal("0"))
    for row, qty in refunds:
        receipt = receipts.get(row.refund_of)
        if receipt is None:
            errors.add("refund_receipt_mismatch")
            continue
        refunded[row.refund_of] += qty
        current, incoming = facts.get(row.bank_entry_id), facts.get(receipt[0].bank_entry_id)
        if current and incoming and current["operation_date"] < incoming["operation_date"]:
            errors.add("refund_predates_receipt")
    remaining = tuple(ReceiptRemaining(key, qty, refunded[key], qty - refunded[key])
                      for key, (_, qty) in sorted(receipts.items()))
    if any(r.remaining < 0 for r in remaining):
        errors.add("refund_exceeds_receipt")
    # The same bank entry may contain inconsistent legacy allocations outside
    # this invoice. Detect them without exposing another document's identity.
    bank_rows = (await session.scalars(select(InvoiceSettlement).where(
        InvoiceSettlement.bank_entry_id.in_(entries),
    ).order_by(InvoiceSettlement.id).execution_options(populate_existing=True))).all()
    foreign_fingerprints = []
    for row in bank_rows:
        if row.document_id == doc.id and row.organization_id == organization_id:
            continue
        errors.add("bank_allocation_scope_mismatch")
        foreign = {"id": row.id, "organization_id": row.organization_id,
                   "document_id": row.document_id, "bank_entry_id": row.bank_entry_id,
                   "amount": str(row.amount), "direction": row.direction,
                   "refund_of": row.refund_of, "snapshot": row.snapshot}
        foreign_fingerprints.append(hashlib.sha256(canonical(foreign).encode()).hexdigest())
        # Rows on this document were already included (or rejected) above.
        if row.document_id != doc.id:
            try:
                used[row.bank_entry_id] += amount(row.amount)
            except (TypeError, ValueError, InvalidOperation):
                errors.add("settlement_snapshot_invalid")
    for entry_id, fact in facts.items():
        available = amount(fact["amount"])
        if used[entry_id] > available:
            errors.add("bank_overallocated")
        elif used[entry_id] < available:
            errors.add("known_bank_unallocated")
    received_total = sum((r.received for r in remaining), Decimal("0"))
    refund_total = sum((qty for _, qty in refunds), Decimal("0"))
    if doc.status == "paid" and not receipts:
        errors.add("legacy_paid_without_receipts")
    if errors:
        state = "history_unknown"
    elif not receipts:
        state = "no_receipts"
    elif any(r.remaining > 0 for r in remaining):
        state = "funds_held"
    else:
        state = "fully_refunded"
    blockers = set(errors)
    if state == "funds_held":
        blockers.add("funds_not_fully_refunded")
    if doc.status == "cancelled" and (rows or descriptors):
        # No cancellation timestamp/basis exists in A1: do not label facts "late"
        # without evidence. The transition service must compare its saved basis.
        blockers.add("cancelled_invoice_money_review_required")
    payload = {"schema": "invoice-money-basis-v1", "organization_id": organization_id,
               "document_id": doc.id, "deal_id": doc.deal_id, "invoice": identity,
               "invoice_status": doc.status, "settlements": frozen_rows,
               "known_bank_entries": descriptors, "revalidated_banks": observed_banks,
               "foreign_allocation_fingerprints": sorted(foreign_fingerprints),
               "money_state": state, "blockers": sorted(blockers),
               "per_receipt_remaining": [
                   {"receipt_id": r.receipt_id, "received": format(r.received, ".2f"),
                    "refunded": format(r.refunded, ".2f"), "remaining": format(r.remaining, ".2f")}
                   for r in remaining]}
    frozen = canonical(payload)
    return InvoiceMoneyBasis(state, received_total, refund_total, remaining,
                             tuple(sorted(blockers)), frozen,
                             hashlib.sha256(frozen.encode("utf-8")).hexdigest())
