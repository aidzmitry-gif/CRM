"""Canonical order producer. No source inference and no commits inside services."""
import hashlib
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException
from pydantic import Field, ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from core.domain.models import User
from core.runtime.deps import get_core, get_session
from core.services.auth import get_current_user, has_permission, resolve_effective_oidc_user
from core.services.logistics import (
    ExactInvoiceIdentity,
    SourceDiscoveryV1,
    producer_sources_snapshot,
    snapshot_hash,
    snapshot_row,
)
from core.services.shipping_payload import (
    ShippingIntent,
    StrictShippingModel,
    canonical_hash,
    canonical_shipping_payload,
    shipping_intent_digest,
)
from modules.sales.access import get_deal_access_for_user, visible_deal_or_404
from modules.sales.accounting_ownership import DealOwnership
from modules.sales.documents import lock_deal
from modules.sales.models import DealDocument
from modules.sales.shipping_associations import OrderInvoiceAssociation, ShippingEnvelope


class EnvelopeInput(StrictShippingModel):
    request_key: str = Field(min_length=1, max_length=64)
    expected_source_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    intent: ShippingIntent


class Evidence(StrictShippingModel):
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    invoice_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    explanation: str = Field(min_length=1, max_length=1000)


class ConfirmInput(StrictShippingModel):
    request_key: str = Field(min_length=1, max_length=64)
    expected_source_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_assignment_revision: int = Field(ge=0)
    exact_invoice: ExactInvoiceIdentity
    envelope_id: str = Field(min_length=36, max_length=36)
    expected_payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_intent_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    evidence_refs: Evidence


def conflict(message):
    raise HTTPException(409, message)


async def current_actor(core, session, user, permission):
    if user is None or (not user.keycloak_user_id and (
        core.services.config.auth_mode != "dev" or user.username in {"", "anonymous"}
    )):
        raise HTTPException(403, "Identified actor required")
    # Refresh the identity map before canonical effective-identity resolution.
    identity = User.keycloak_user_id == user.keycloak_user_id if user.keycloak_user_id else User.username == user.username
    linked = await session.scalar(select(User).where(identity).execution_options(populate_existing=True))
    if linked is not None and linked.status != "active":
        raise HTTPException(403, "Inactive actor")
    effective = await resolve_effective_oidc_user(user, session)
    if not has_permission(core, effective, permission):
        raise HTTPException(403, f"Permission required: {permission}")
    return effective, effective.keycloak_user_id or effective.username


def order_integrity(doc):
    if (doc is None or doc.kind != "order" or not doc.issued_at or not doc.original_html
            or hashlib.sha256(doc.original_html.encode("utf-8")).hexdigest() != doc.content_sha256):
        conflict("Issued order original integrity requires reconciliation")
    return doc.content_sha256


def envelope_result(row):
    return {"envelope_id": row.id, "payload": row.payload, "payload_sha256": row.payload_sha256,
            "source_sha256": row.source_sha256}


class SalesShippingProducer:
    def __init__(self, core):
        self.core, self.services = core, core.services

    async def _invoice_snapshot_records(self, session, exact, user):
        user, _ = await current_actor(self.core, session, user, "sales.deal.read")
        orgs = await self.services.accounting.source_organizations(session, user)
        if exact["organization_id"] not in {row["id"] for row in orgs}:
            raise HTTPException(403, "producer_scope_unavailable")
        doc = await session.scalar(select(DealDocument).where(DealDocument.id == exact["document_id"])
            .execution_options(populate_existing=True))
        if doc is None:
            raise HTTPException(404, "Invoice source not found")
        await visible_deal_or_404(session, doc.deal_id, await get_deal_access_for_user(session, user))
        owner = await session.scalar(select(DealOwnership).where(DealOwnership.deal_id == doc.deal_id)
            .execution_options(populate_existing=True))
        if (owner is None or owner.organization_id != exact["organization_id"] or doc.kind != "invoice"
                or doc.version != exact["expected_version"] or doc.content_sha256 != exact["expected_content_sha256"]):
            conflict("Exact invoice source changed")
        associations = list(await session.scalars(select(OrderInvoiceAssociation).where(
            OrderInvoiceAssociation.organization_id == exact["organization_id"],
            OrderInvoiceAssociation.exact_invoice["document_id"].as_integer() == exact["document_id"],
            OrderInvoiceAssociation.exact_invoice["expected_version"].as_integer() == exact["expected_version"],
            OrderInvoiceAssociation.exact_invoice["expected_content_sha256"].as_string() == exact["expected_content_sha256"],
        ).order_by(OrderInvoiceAssociation.id).execution_options(populate_existing=True)))
        records = []
        for association in associations:
            envelope = await session.scalar(select(ShippingEnvelope).where(ShippingEnvelope.id == association.envelope_id)
                .execution_options(populate_existing=True))
            if envelope is None or association.exact_invoice != exact or association.deal_id != doc.deal_id:
                conflict("Order snapshot association changed")
            records.append((association, envelope))
        return doc, records

    async def discover_invoice_shipping_sources(self, session, *, exact_invoice, user):
        exact = ExactInvoiceIdentity.model_validate(exact_invoice).model_dump()
        doc, records = await self._invoice_snapshot_records(session, exact, user)
        return SourceDiscoveryV1(source_kind="order", exact_invoice=exact,
            source_keys=sorted({f"sales:order:{envelope.order_document_id}" for _, envelope in records}),
            source_document_ids=sorted({doc.id, *(envelope.order_document_id for _, envelope in records)})).model_dump()

    async def lock_invoice_shipping_sources_snapshot(self, session, *, exact_invoice, user, discovery):
        exact = ExactInvoiceIdentity.model_validate(exact_invoice).model_dump()
        expected = SourceDiscoveryV1.model_validate(discovery).model_dump()
        doc, _ = await self._invoice_snapshot_records(session, exact, user)
        await lock_deal(session, doc.deal_id)
        current = await self.discover_invoice_shipping_sources(session, exact_invoice=exact, user=user)
        if current != expected:
            conflict("Order snapshot discovery changed")
        documents = list(await session.scalars(select(DealDocument).where(DealDocument.id.in_(current["source_document_ids"]))
            .order_by(DealDocument.id).with_for_update().execution_options(populate_existing=True)))
        if len(documents) != len(current["source_document_ids"]) or any(row.deal_id != doc.deal_id for row in documents):
            conflict("Order snapshot source document set changed")
        try:
            await self.services.sales_source.invoice_shipping_source(session, **exact, operation="historical_claim")
        except ValueError as exc:
            conflict(str(exc))
        # The caller's org lock prevents a new association while these rows are read.
        doc, records = await self._invoice_snapshot_records(session, exact, user)
        by_id = {row.id: row for row in documents}
        sources = []
        for association, envelope in records:
            model, hashed = canonical_shipping_payload(envelope.payload)
            verified = await self.verify_shipping_source(session, source_kind="order",
                source_key=f"sales:order:{envelope.order_document_id}", source_revision=str(envelope.order_version),
                actual_payload_hash=hashed)
            # Read permission and current DealAccess were checked above; do not
            # require the association mutation privilege for a read-only review.
            if not verified or verified["exact_invoice"] != exact or verified["association_id"] != association.id:
                conflict("Order snapshot verification changed")
            sources.append({"source": model.source.model_dump(),
                "envelope": {"record_id": envelope.id, "row_sha256": snapshot_hash(snapshot_row(envelope)),
                    "payload_sha256": hashed, "payload": model.model_dump()},
                "association": {"record_id": association.id, "revision": "1", "row_sha256": snapshot_hash(snapshot_row(association))},
                "execution_id": verified["execution_id"], "intent_digest": verified["intent_digest"],
                "source_state_sha256": snapshot_hash(snapshot_row(by_id[envelope.order_document_id])),
                "fulfillment_allowed": verified["fulfillment_allowed"]})
        return producer_sources_snapshot(exact, sources)

    async def _source(self, session, kind, key, revision=None, lock=False):
        if kind != "order" or not isinstance(key, str) or not key.startswith("sales:order:"):
            return None
        raw = key.removeprefix("sales:order:")
        if not raw.isascii() or not raw.isdecimal() or str(int(raw)) != raw or int(raw) <= 0:
            return None
        stmt = select(ShippingEnvelope).where(ShippingEnvelope.order_document_id == int(raw))
        if revision is not None:
            if not isinstance(revision, str) or not revision.isascii() or not revision.isdecimal() or str(int(revision)) != revision or int(revision) <= 0:
                return None
            stmt = stmt.where(ShippingEnvelope.order_version == int(revision))
        if lock:
            stmt = stmt.with_for_update()
        return await session.scalar(stmt.order_by(ShippingEnvelope.order_version.desc())
            .execution_options(populate_existing=True))

    async def authorize_shipping_intake(self, session, *, source_kind, source_key, user):
        user, _ = await current_actor(self.core, session, user, "sales.shipping.associate")
        envelope = await self._source(session, source_kind, source_key)
        if envelope is None:
            raise HTTPException(404, "Order shipping source not found")
        doc = await session.get(DealDocument, envelope.order_document_id)
        access = await get_deal_access_for_user(session, user)
        await visible_deal_or_404(session, doc.deal_id, access)

    async def resolve_shipping_source(self, session, *, source_kind, source_key, source_revision):
        envelope = await self._source(session, source_kind, source_key, source_revision)
        if envelope is None:
            return None
        assoc = await session.scalar(select(OrderInvoiceAssociation).where(OrderInvoiceAssociation.envelope_id == envelope.id))
        return dict(assoc.exact_invoice) if assoc else None

    async def verify_shipping_source(self, session, *, source_kind, source_key, source_revision, actual_payload_hash, user=None):
        try:
            return await self._verify_shipping_source(session, source_kind=source_kind, source_key=source_key,
                source_revision=source_revision, actual_payload_hash=actual_payload_hash, user=user)
        except (ValidationError, ValueError, TypeError, KeyError) as exc:
            raise HTTPException(409, "Persisted shipping source is invalid") from exc

    async def _verify_shipping_source(self, session, *, source_kind, source_key, source_revision, actual_payload_hash, user=None):
        # Caller already holds org/deal/Sales document locks in canonical order.
        envelope = await self._source(session, source_kind, source_key, source_revision, lock=True)
        if envelope is None:
            return None
        if user is not None:
            await self.authorize_shipping_intake(session, source_kind=source_kind, source_key=source_key, user=user)
        doc = await session.scalar(select(DealDocument).where(DealDocument.id == envelope.order_document_id)
            .execution_options(populate_existing=True))
        model, computed = canonical_shipping_payload(envelope.payload)
        if (order_integrity(doc) != envelope.source_sha256 or doc.version != envelope.order_version
                or computed != envelope.payload_sha256 or actual_payload_hash != computed
                or model.source.key != source_key or model.source.revision != source_revision
                or model.source_refs.document_id != doc.id or model.source_refs.document_number != doc.number):
            conflict("Order source or actual payload changed")
        assoc = await session.scalar(select(OrderInvoiceAssociation).where(OrderInvoiceAssociation.envelope_id == envelope.id)
            .execution_options(populate_existing=True))
        if assoc is None:
            return None
        owner = await session.get(DealOwnership, doc.deal_id)
        exact = ExactInvoiceIdentity.model_validate(assoc.exact_invoice).model_dump()
        evidence = Evidence.model_validate(assoc.evidence_refs)
        if (owner is None or owner.organization_id != exact["organization_id"] or assoc.organization_id != owner.organization_id
                or assoc.deal_id != doc.deal_id or evidence.source_sha256 != envelope.source_sha256
                or evidence.invoice_sha256 != exact["expected_content_sha256"]
                or shipping_intent_digest(exact, model.intent) != assoc.intent_digest
                or str(UUID(assoc.execution_id)) != assoc.execution_id):
            conflict("Order association integrity changed")
        return {"exact_invoice": exact, "execution_id": assoc.execution_id, "intent_digest": assoc.intent_digest,
                "fulfillment_allowed": bool(doc.status in {"posted", "issued", "paid"} and doc.superseded_by_id is None),
                "payload_sha256": computed, "association_id": assoc.id, "association_revision": "1"}

    async def prepare(self, session, deal_id, order_id, data, user):
        user, _ = await current_actor(self.core, session, user, "sales.shipping.associate")
        await visible_deal_or_404(session, deal_id, await get_deal_access_for_user(session, user))
        owner = await session.get(DealOwnership, deal_id)
        if owner is None:
            conflict("Order organization must be explicitly assigned")
        await self.services.accounting.source_member(session, owner.organization_id, user)
        await lock_deal(session, deal_id)
        doc = await session.scalar(select(DealDocument).where(DealDocument.id == order_id)
            .with_for_update().execution_options(populate_existing=True))
        user, actor = await current_actor(self.core, session, user, "sales.shipping.associate")
        await visible_deal_or_404(session, deal_id, await get_deal_access_for_user(session, user))
        if doc is None or doc.deal_id != deal_id:
            raise HTTPException(404, "Order not found")
        if order_integrity(doc) != data.expected_source_hash:
            conflict("Order original changed")
        existing = await session.scalar(select(ShippingEnvelope).where(ShippingEnvelope.order_document_id == order_id,
            ShippingEnvelope.order_version == doc.version))
        if existing:
            model, hashed = canonical_shipping_payload(existing.payload)
            if (existing.request_key != data.request_key or existing.source_sha256 != data.expected_source_hash
                    or model.intent != data.intent or hashed != existing.payload_sha256):
                conflict("Order shipping envelope already differs")
            return envelope_result(existing)
        if doc.status not in {"posted", "issued", "paid"} or doc.superseded_by_id is not None:
            conflict("Terminal order cannot create a shipping envelope")
        payload = {"schema_version": 1, "source": {"kind": "order", "key": f"sales:order:{doc.id}", "revision": str(doc.version)},
                   "source_refs": {"document_id": doc.id, "document_number": doc.number, "log_ref": ""},
                   "intent": data.intent.model_dump()}
        model, hashed = canonical_shipping_payload(payload)
        row = ShippingEnvelope(id=str(uuid4()), order_document_id=doc.id, order_version=doc.version,
            source_sha256=doc.content_sha256, request_key=data.request_key, payload=model.model_dump(), payload_sha256=hashed, actor=actor)
        session.add(row)
        await session.flush()
        self.services.event_bus.emit(session, "sales.document.posted", {**row.payload, "payload_sha256": hashed})
        return envelope_result(row)

    async def association(self, session, deal_id, order_id, data, user, *, confirm):
        user, _ = await current_actor(self.core, session, user, "sales.shipping.associate")
        await visible_deal_or_404(session, deal_id, await get_deal_access_for_user(session, user))
        exact = data.exact_invoice.model_dump()
        # No source locks before organization, then one deal and docs in ID order.
        await self.services.accounting.source_owner_authority(session, exact["organization_id"], user)
        await lock_deal(session, deal_id)
        docs = list(await session.scalars(select(DealDocument).where(DealDocument.id.in_([order_id, exact["document_id"]]))
            .order_by(DealDocument.id).with_for_update().execution_options(populate_existing=True)))
        by_id = {d.id: d for d in docs}
        if len(by_id) != 2 or any(d.deal_id != deal_id for d in docs):
            conflict("Order and invoice must be separate originals of the same deal")
        user, actor = await current_actor(self.core, session, user, "sales.shipping.associate")
        await visible_deal_or_404(session, deal_id, await get_deal_access_for_user(session, user))
        envelope = await self._source(session, "order", f"sales:order:{order_id}", str(by_id[order_id].version), lock=True)
        if envelope is None or envelope.id != data.envelope_id:
            conflict("Prepare and review the order envelope first")
        old = await session.scalar(select(OrderInvoiceAssociation).where(OrderInvoiceAssociation.envelope_id == envelope.id))
        if old is None and (by_id[order_id].status not in {"posted", "issued", "paid"}
                            or by_id[order_id].superseded_by_id is not None):
            conflict("Terminal order cannot create a new association")
        # Only an existing immutable association can select historical attribution.
        operation = "historical_claim" if old else "fulfill"
        try:
            await self.services.sales_source.invoice_shipping_source(session, exact["document_id"],
                organization_id=exact["organization_id"], expected_version=exact["expected_version"],
                expected_content_sha256=exact["expected_content_sha256"], operation=operation)
        except ValueError as exc:
            conflict(str(exc))
        model, hashed = canonical_shipping_payload(envelope.payload)
        expected = shipping_intent_digest(exact, model.intent)
        if (order_integrity(by_id[order_id]) != data.expected_source_hash or envelope.source_sha256 != data.expected_source_hash
                or envelope.payload_sha256 != hashed or hashed != data.expected_payload_hash or expected != data.expected_intent_digest
                or data.expected_assignment_revision != 0 or data.evidence_refs.source_sha256 != envelope.source_sha256
                or data.evidence_refs.invoice_sha256 != exact["expected_content_sha256"]):
            conflict("Source, intent or documentary evidence changed; review again")
        if not data.evidence_refs.explanation.strip():
            conflict("Documentary explanation is required")
        confirmation = canonical_hash(data.model_dump())
        if old:
            if old.confirmation_sha256 != confirmation:
                conflict("Order association already differs")
            return {"association_id": old.id, "execution_id": old.execution_id, "intent_digest": old.intent_digest, "replayed": True}
        result = {"exact_invoice": exact, "source_original": by_id[order_id].original_html,
            "invoice_original": by_id[exact["document_id"]].original_html, **envelope_result(envelope), "intent_digest": expected}
        if not confirm:
            return result
        execution = await self.services.logistics.claim_execution(session, exact_invoice=exact,
            intent=model.intent.model_dump(), expected_digest=expected)
        if (execution.get("exact_invoice") != exact or execution.get("intent") != model.intent.model_dump()
                or execution.get("intent_digest") != expected or str(UUID(execution["execution_id"])) != execution["execution_id"]):
            conflict("Execution registry returned inconsistent evidence")
        row = OrderInvoiceAssociation(id=str(uuid4()), envelope_id=envelope.id, organization_id=exact["organization_id"],
            deal_id=deal_id, exact_invoice=exact, execution_id=execution["execution_id"], intent_digest=expected,
            request_key=data.request_key, confirmation_sha256=confirmation, evidence_refs=data.evidence_refs.model_dump(), actor=actor)
        session.add(row)
        await session.flush()
        return {"association_id": row.id, "execution_id": row.execution_id, "intent_digest": expected, "replayed": False}


async def transaction(session=Depends(get_session)):
    try:
        yield session
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(409, "Shipping request key or association already exists") from exc
    except Exception:
        await session.rollback()
        raise


router = APIRouter()


@router.post("/deals/{deal_id}/documents/{order_id}/shipping-envelope")
async def shipping_envelope_prepare(deal_id: int, order_id: int, data: EnvelopeInput,
    session=Depends(transaction), core=Depends(get_core), user=Depends(get_current_user)):
    result = await SalesShippingProducer(core).prepare(session, deal_id, order_id, data, user)
    await session.commit()
    return result


@router.post("/deals/{deal_id}/documents/{order_id}/shipping-association-preview")
async def shipping_association_preview(deal_id: int, order_id: int, data: ConfirmInput,
    session=Depends(transaction), core=Depends(get_core), user=Depends(get_current_user)):
    return await SalesShippingProducer(core).association(session, deal_id, order_id, data, user, confirm=False)


@router.post("/deals/{deal_id}/documents/{order_id}/shipping-association-confirm")
async def shipping_association_confirm(deal_id: int, order_id: int, data: ConfirmInput,
    session=Depends(transaction), core=Depends(get_core), user=Depends(get_current_user)):
    result = await SalesShippingProducer(core).association(session, deal_id, order_id, data, user, confirm=True)
    await session.commit()
    return result
