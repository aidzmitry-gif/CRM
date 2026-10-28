"""Payment projection for visible Sales rows; never changes money or invoice state.

Current invoices use validated, allocated book evidence. Historical document flags
remain explicitly legacy. Neither projection authorizes cancellation or certifies
the completeness of bank statements outside the local ledger.
"""
from dataclasses import dataclass

from fastapi import HTTPException
from sqlalchemy import select

from modules.sales.accounting_ownership import DealOwnership
from modules.sales.models import Deal, DealDocument


@dataclass(frozen=True)
class JournalPayment:
    payment: str
    invoice_number: str | None = None
    source: str = "ledger"
    reason: str | None = None


def unknown(number, reason="unverified_evidence"):
    return JournalPayment("unknown", number, "unknown", reason)


def observed_payment(document, basis, *, cancellation_verified=False):
    """Classify one exact invoice; an overpayment never pays a different invoice."""
    from modules.sales.invoice_money_basis import amount

    number = document.number
    allowed = {"funds_not_fully_refunded"} if basis.money_state == "funds_held" else set()
    if set(basis.blockers) - allowed:
        return unknown(number)
    try:
        due = amount(document.amount, positive=False)
        received = amount(basis.received, positive=False)
        refunded = amount(basis.refunded, positive=False)
    except (ArithmeticError, TypeError, ValueError):
        return unknown(number)
    net = received - refunded
    if net < 0:
        return unknown(number)
    if document.status == "cancelled":
        if cancellation_verified and basis.money_state == "no_receipts" and received == refunded == 0:
            return JournalPayment("cancelled", number)
        return unknown(number, "cancellation_review_required")
    if basis.money_state == "no_receipts" and received == refunded == 0:
        return JournalPayment("invoiced", number)
    if basis.money_state == "fully_refunded" and received > 0 and net == 0:
        return JournalPayment("refunded", number)
    if basis.money_state == "funds_held" and net > 0 and due > 0:
        return JournalPayment("paid" if net >= due else "partial", number)
    return unknown(number)


def aggregate_payments(payments):
    """Every active invoice must be covered; unknown evidence dominates."""
    number = payments[-1].invoice_number
    uncertain = next((p for p in payments if p.payment == "unknown"), None)
    if uncertain is not None:
        return unknown(number, uncertain.reason)
    active = [p for p in payments if p.payment != "cancelled"]
    if not active:
        return JournalPayment("cancelled", number)
    number = active[-1].invoice_number
    if all(p.payment == "paid" for p in active):
        return JournalPayment("paid", number)
    if any(p.payment in {"paid", "partial"} for p in active):
        return JournalPayment("partial", number)
    if any(p.payment == "refunded" for p in active):
        return JournalPayment("refunded", number)
    return JournalPayment("invoiced", number)


def legacy_payment(documents):
    paid = next((d for d in documents if d.status == "paid"), None)
    posted = next((d for d in reversed(documents) if d.status == "posted"), None)
    if paid is not None:
        return JournalPayment("paid", paid.number, "legacy", "legacy_document_status")
    if posted is not None:
        return JournalPayment("invoiced", posted.number, "legacy", "legacy_document_status")
    cancelled = next((d for d in reversed(documents) if d.status == "cancelled"), None)
    if cancelled is not None:
        return JournalPayment("cancelled", cancelled.number, "legacy", "legacy_document_status")
    if any(d.status != "draft" for d in documents):
        return unknown(documents[-1].number)
    return JournalPayment("none", source="none")


async def cancellation_verified(session, org_id, document):
    from modules.sales.invoice_cancellation import (
        InvoiceCancellationReceipt,
        orm_cancellation_receipt,
    )

    owner = await session.scalar(select(InvoiceCancellationReceipt.organization_id).where(
        InvoiceCancellationReceipt.document_id == document.id))
    if owner != org_id:
        return False
    return await (await session.connection()).run_sync(lambda conn: orm_cancellation_receipt(conn, document))


async def project_journal_payments(session, services, user, deals):
    """Caller owns the transaction. Acquire all org locks before deal/invoice locks."""
    from modules.sales.invoice_issuance import InvoiceIssuanceReceipt
    from modules.sales.invoice_money_basis import evaluate_invoice_money_basis

    if not deals:
        return {}
    ids = [d.id for d in deals]
    discovered_owners = dict((await session.execute(select(
        DealOwnership.deal_id, DealOwnership.organization_id,
    ).where(DealOwnership.deal_id.in_(ids)))).all())
    gateway = getattr(services, "accounting", None)
    available = all(callable(getattr(gateway, method, None))
                    for method in ("source_member", "invoice_bank_basis", "bank_settlement"))
    authorized = set()
    denied = {}
    if available:
        for org_id in sorted(set(discovered_owners.values())):
            try:
                await gateway.source_member(session, org_id, user)
            except HTTPException as exc:
                if exc.status_code == 401:
                    raise
                denied[org_id] = "organization_access_unavailable" if exc.status_code in {403, 404} else "ledger_unavailable"
            else:
                authorized.add(org_id)
    # Include empty/draft-only deals: a first issuer can already hold the book
    # lock while its new receipt is still invisible. Classifying before these
    # locks would turn that pending issuance into a confident "none" result.
    await session.execute(select(Deal).where(Deal.id.in_(ids)).order_by(Deal.id)
                          .with_for_update().execution_options(populate_existing=True))
    owners = dict((await session.execute(select(
        DealOwnership.deal_id, DealOwnership.organization_id,
    ).where(DealOwnership.deal_id.in_(ids)))).all())
    documents = (await session.scalars(select(DealDocument).where(
        DealDocument.deal_id.in_(ids), DealDocument.kind == "invoice",
    ).order_by(DealDocument.id).with_for_update().execution_options(populate_existing=True))).all()
    issued_ids = set((await session.scalars(select(InvoiceIssuanceReceipt.document_id).where(
        InvoiceIssuanceReceipt.document_id.in_([d.id for d in documents]),
    ))).all()) if documents else set()
    grouped = {key: [] for key in ids}
    for doc in documents:
        grouped[doc.deal_id].append(doc)
    current = {key for key, docs in grouped.items()
               if any(d.id in issued_ids or d.status == "issued" for d in docs)}
    result = {key: legacy_payment(docs) for key, docs in grouped.items() if key not in current}
    for deal_id in sorted(current, key=lambda key: (owners.get(key, 0), key)):
        number = grouped[deal_id][-1].number
        org_id = owners.get(deal_id)
        if org_id != discovered_owners.get(deal_id):
            # Never acquire a newly discovered book after a deal lock.
            result[deal_id] = unknown(number, "organization_binding_changed")
            continue
        if not available or org_id not in authorized:
            result[deal_id] = unknown(number, denied.get(org_id, "ledger_unavailable" if not available else "unverified_evidence"))
            continue
        # Refresh after the book/deal locks, including invoices issued since discovery.
        docs = (await session.scalars(select(DealDocument).where(
            DealDocument.deal_id == deal_id, DealDocument.kind == "invoice",
        ).order_by(DealDocument.id).with_for_update().execution_options(populate_existing=True))).all()
        current_ids = set((await session.scalars(select(InvoiceIssuanceReceipt.document_id).where(
            InvoiceIssuanceReceipt.document_id.in_([d.id for d in docs]),
        ))).all())
        if not docs or any(d.status in {"paid", "posted"} and d.id not in current_ids for d in docs):
            result[deal_id] = unknown(number, "mixed_invoice_sources")
            continue
        payments = []
        for doc in docs:
            if doc.status == "draft" and doc.id not in current_ids:
                continue
            if doc.id not in current_ids:
                payments.append(unknown(doc.number))
                continue
            try:
                basis = await evaluate_invoice_money_basis(session, org_id, user, doc, gateway)
                cancelled = (await cancellation_verified(session, org_id, doc)) if doc.status == "cancelled" else False
                payments.append(observed_payment(doc, basis, cancellation_verified=cancelled))
            except HTTPException as exc:
                if exc.status_code == 401:
                    raise
                reason = "organization_access_unavailable" if exc.status_code == 403 else "ledger_unavailable" if exc.status_code >= 500 else "unverified_evidence"
                payments.append(unknown(doc.number, reason))
            except (ArithmeticError, AttributeError, KeyError, TypeError, ValueError):
                # Unusable facade evidence cannot establish either payment or debt.
                payments.append(unknown(doc.number))
        result[deal_id] = aggregate_payments(payments) if payments else unknown(number)
    return result
