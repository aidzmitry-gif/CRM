"""Original invoice evidence for warehouse event processing."""
from datetime import date
from decimal import Decimal, InvalidOperation

from sqlalchemy import select

from modules.sales.accounting_ownership import DealOwnership
from modules.sales.documents import digest, lock_deal
from modules.sales.models import DealDocument


class SalesReservationSource:
    async def authorize_shipping_deal(self, session, deal_id, user):
        from modules.sales.access import get_deal_access_for_user, visible_deal_or_404

        access = await get_deal_access_for_user(session, user)
        await visible_deal_or_404(session, deal_id, access)

    async def invoice_shipping_source(
        self, session, document_id, *, organization_id,
        expected_version, expected_content_sha256, operation="fulfill",
    ):
        if not isinstance(operation, str) or operation not in {"fulfill", "historical_claim", "cancellation"}:
            raise ValueError("Unknown invoice shipping operation")
        if (type(organization_id) is not int or organization_id <= 0
                or type(expected_version) is not int or expected_version <= 0
                or not isinstance(expected_content_sha256, str)
                or len(expected_content_sha256) != 64
                or any(c not in "0123456789abcdef" for c in expected_content_sha256)):
            raise ValueError("Exact invoice organization, version and original hash are required")
        # Discovery is a routing hint only. Avoid taking another organization's
        # source locks when the caller holds the wrong organization lock.
        if await self.invoice_organization(session, document_id) != organization_id:
            raise ValueError("Invoice source identity does not match")
        if operation == "cancellation":
            doc = await session.get(DealDocument, document_id, populate_existing=True)
            if doc is not None and doc.reserve_status == "unreserved":
                # A failure here is unresolved evidence, never a stock fallback.
                return await self._unreserved_cancellation_source(
                    session, document_id, organization_id=organization_id,
                    expected_version=expected_version, expected_content_sha256=expected_content_sha256,
                )
            # Preserve both legacy stock and genuinely later-reserved on-order
            # history, including the exact old source shape and validations.
            operation = "historical_claim"
        facts = await self.invoice_reservation(session, document_id)
        if (facts["organization_id"] != organization_id
                or facts["version"] != expected_version
                or facts["content_sha256"] != expected_content_sha256):
            raise ValueError("Invoice source identity does not match")
        # invoice_reservation just refreshed this row under the deal/doc locks.
        doc = await session.get(DealDocument, document_id)
        can_fulfill = (doc.status in {"posted", "paid"}
                       or (doc.status == "issued" and facts.get("issuance_receipt") is not None))
        can_fulfill = can_fulfill and facts["reserve_status"] == "reserved" and doc.superseded_by_id is None
        if operation == "fulfill":
            from modules.sales.deal_loss import pending
            if await pending(session, doc.deal_id):
                raise ValueError("Deal loss request blocks new physical fulfillment")
        if operation == "fulfill" and not can_fulfill:
            raise ValueError("Invoice is terminal or its reservation is released")
        return {**facts, "document_status": doc.status, "operation": operation,
                "fulfillment_allowed": operation == "fulfill" and can_fulfill}

    async def _unreserved_cancellation_source(
        self, session, document_id, *, organization_id, expected_version, expected_content_sha256,
    ):
        from modules.sales.invoice_issuance import (
            InvoiceLateReservationReceipt,
            unreserved_issuance_anchor,
            verified_receipt,
        )

        facts = await self.invoice_financial_source(
            session, document_id, organization_id=organization_id,
            expected_version=expected_version, expected_content_sha256=expected_content_sha256,
        )
        doc = await session.get(DealDocument, document_id)
        receipt = await verified_receipt(session, doc, organization_id)
        unreserved_issuance_anchor(doc, organization_id, receipt)
        if await session.get(InvoiceLateReservationReceipt, document_id, populate_existing=True) is not None:
            raise ValueError("Cancellation requires a genuine unreserved ERP on-order original")
        if doc.superseded_by_id is not None:
            from modules.sales.invoice_cancellation import orm_cancellation_receipt

            if doc.status != "cancelled" or not await session.run_sync(
                    lambda sync: orm_cancellation_receipt(sync.connection(), doc)):
                raise ValueError("Superseded invoice cancellation requires reconciliation")
        # [] is the consequence of the verified initial on-order mode, not a
        # fabricated persisted reservation. WMS must still inspect its history.
        return {**facts, "reserve_mode": "on_order", "reserve_status": "unreserved",
                "reservation_digest": None, "allocations": [], "operation": "cancellation",
                "fulfillment_allowed": False}

    async def invoice_organization(self, session, document_id):
        if type(document_id) is not int or document_id <= 0:
            raise ValueError("An exact positive invoice ID is required")
        organization_id = await session.scalar(select(DealOwnership.organization_id)
            .join(DealDocument, DealDocument.deal_id == DealOwnership.deal_id)
            .where(DealDocument.id == document_id))
        if type(organization_id) is not int or organization_id <= 0:
            raise ValueError("Invoice organization must be explicitly confirmed")
        return organization_id

    async def invoice_reservation(self, session, document_id):
        if type(document_id) is not int or document_id <= 0:
            raise ValueError("An exact positive invoice ID is required")
        deal_id = await session.scalar(select(DealDocument.deal_id).where(DealDocument.id == document_id))
        if deal_id is None:
            raise ValueError("Reservation invoice does not exist")
        # Same lock order as invoice lifecycle changes; the caller retains locks.
        await lock_deal(session, deal_id)
        doc = await session.scalar(select(DealDocument).where(DealDocument.id == document_id)
                                   .with_for_update().execution_options(populate_existing=True))
        owner = await session.get(DealOwnership, deal_id)
        if owner is None or type(owner.organization_id) is not int or owner.organization_id <= 0:
            raise ValueError("Invoice organization must be explicitly confirmed")
        from modules.sales.invoice_issuance import (
            effective_reservation_digest,
            receipt_anchor,
            verified_receipt,
        )
        receipt = await verified_receipt(session, doc, owner.organization_id) if doc is not None else None
        if (doc is None or doc.deal_id != deal_id or doc.kind != "invoice" or not doc.content_sha256
                or not doc.original_html or not doc.issued_at
                or (doc.status not in {"posted", "paid", "cancelled"} and not (doc.status == "issued" and receipt))
                or doc.reserve_status not in {"reserved", "released"}):
            raise ValueError("Issued invoice with a known reservation state is required")
        if digest(doc.original_html) != doc.content_sha256:
            raise ValueError("Original invoice integrity requires reconciliation")
        snapshot = doc.snapshot_json
        lines = snapshot.get("items") if isinstance(snapshot, dict) else None
        if not isinstance(lines, list) or not lines:
            raise ValueError("Original invoice items are required")
        quantities = {}
        stock_lines = []
        for line_no, line in enumerate(lines, 1):
            if not isinstance(line, dict):
                raise ValueError("Original invoice line is invalid")
            sku = line.get("sku_code")
            # Non-stock lines (e.g. services) cannot authorize stock movements.
            if sku is None:
                continue
            if not isinstance(sku, str) or not sku.strip() or len(sku) > 64:
                raise ValueError("Original invoice SKU is invalid")
            raw = line.get("qty")
            if isinstance(raw, (bool, float)):
                raise ValueError("Original invoice quantity must be exact")
            try:
                qty = Decimal(str(raw))
            except (InvalidOperation, ValueError):
                raise ValueError("Original invoice quantity is invalid") from None
            if (not qty.is_finite() or qty <= 0 or qty >= Decimal("1000000000000")
                    or qty != qty.quantize(Decimal("0.01"))):
                raise ValueError("Original invoice quantity is outside the supported range")
            quantities[sku] = quantities.get(sku, Decimal("0")) + qty
            stock_lines.append({"line_no": line_no, "sku_code": sku, "qty": format(qty, ".2f")})
        if not quantities:
            raise ValueError("Invoice contains no stock items")
        reservation_digest = await effective_reservation_digest(session, doc, receipt) if receipt else None
        if receipt and not reservation_digest:
            raise ValueError("Invoice requires a confirmed reservation before warehouse fulfillment")
        qualified = ({"issuance_receipt": receipt_anchor(receipt),
                      "reservation_digest": reservation_digest} if receipt else {})
        return {"document_id": doc.id, "deal_id": deal_id, "organization_id": owner.organization_id, **qualified,
                "version": doc.version, "content_sha256": doc.content_sha256,
                "reserve_status": doc.reserve_status,
                "lines": stock_lines,
                "quantities": {sku: format(qty, ".2f") for sku, qty in sorted(quantities.items())}}

    async def invoice_financial_source(
        self, session, document_id, *, organization_id,
        expected_version, expected_content_sha256,
    ):
        """Verify receivable evidence; no reservation, shipment or cash assertion.

        Internal outbox caller holds the explicit organization lock and owns the
        transaction. Refresh source under existing deal/document lock order.
        A delayed issuance event receives current terminal state, never demand
        resurrected from its old payload. Unknown legacy ownership stays held.
        """
        if (type(document_id) is not int or document_id <= 0
                or type(organization_id) is not int or organization_id <= 0
                or type(expected_version) is not int or expected_version <= 0
                or not isinstance(expected_content_sha256, str)
                or len(expected_content_sha256) != 64
                or any(c not in "0123456789abcdef" for c in expected_content_sha256)):
            raise ValueError("Exact financial invoice identity is required")
        if await self.invoice_organization(session, document_id) != organization_id:
            raise ValueError("Financial invoice organization does not match")
        deal_id = await session.scalar(select(DealDocument.deal_id).where(DealDocument.id == document_id))
        if deal_id is None:
            raise ValueError("Financial invoice does not exist")
        await lock_deal(session, deal_id)
        doc = await session.scalar(select(DealDocument).where(DealDocument.id == document_id)
                                   .with_for_update().execution_options(populate_existing=True))
        owner = await session.get(DealOwnership, deal_id, populate_existing=True)
        if (doc is None or doc.deal_id != deal_id or owner is None
                or owner.organization_id != organization_id or doc.kind != "invoice"
                or doc.version != expected_version or doc.content_sha256 != expected_content_sha256
                or doc.status not in {"issued", "posted", "paid", "cancelled", "rejected"}):
            raise ValueError("Financial invoice source identity does not match")
        from modules.sales.invoice_issuance import receipt_anchor, verified_receipt

        receipt = await verified_receipt(session, doc, organization_id)
        if receipt is None:
            raise ValueError("Financial invoice requires its confirmed issuance receipt")
        snapshot = doc.snapshot_json
        raw_amount = snapshot.get("amount")
        if not isinstance(raw_amount, str):
            raise ValueError("Financial invoice amount must be exact")
        try:
            amount = Decimal(raw_amount)
        except InvalidOperation:
            raise ValueError("Financial invoice amount is invalid") from None
        if (not amount.is_finite() or amount < 0 or amount >= Decimal("1000000000000")
                or amount != amount.quantize(Decimal("0.01")) or amount != doc.amount):
            raise ValueError("Financial invoice amount does not match")
        currency = snapshot.get("currency")
        if (not isinstance(currency, str) or len(currency) != 3
                or any(c not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ" for c in currency)):
            raise ValueError("Financial invoice currency is not confirmed")
        raw_date = snapshot.get("document_date")
        if not isinstance(raw_date, str):
            raise ValueError("Financial invoice operation date is required")
        try:
            operation_date = date.fromisoformat(raw_date)
        except ValueError:
            raise ValueError("Financial invoice operation date is invalid") from None
        if operation_date.isoformat() != raw_date:
            raise ValueError("Financial invoice operation date must be canonical ISO")
        return {"document_id": doc.id, "deal_id": deal_id, "organization_id": organization_id,
                "version": doc.version, "content_sha256": doc.content_sha256,
                "issuance_receipt": receipt_anchor(receipt), "number": doc.number,
                "amount": format(amount, ".2f"), "currency": currency,
                "document_date": raw_date, "issued_at": doc.issued_at,
                "document_status": doc.status, "supersedes_id": doc.supersedes_id,
                "superseded_by_id": doc.superseded_by_id,
                "collectible": doc.status in {"issued", "posted", "paid"} and doc.superseded_by_id is None}
