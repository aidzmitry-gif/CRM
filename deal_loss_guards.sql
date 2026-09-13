-- ADDITIVE REVIEW ARTIFACT. No migration or installation is performed here.
-- Install after both DealLoss ORM tables and existing invoice cancellation,
-- issuance, reservation-release, logistics and accounting guards. PostgreSQL
-- concurrency/commit-time tests are a required integration gate.
-- DML privileges remain application-only: these guards prove package consistency,
-- not actor authentication. Canonical SHA-256 is verified by the application.

ALTER TABLE sales.deal_loss_resolution ADD COLUMN born_root_transaction bigint NOT NULL DEFAULT txid_current();
CREATE OR REPLACE FUNCTION sales.stamp_loss_resolution_root() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 NEW.born_root_transaction := txid_current();
 RETURN NEW;
END $$;
CREATE TRIGGER stamp_loss_resolution_root BEFORE INSERT ON sales.deal_loss_resolution
FOR EACH ROW EXECUTE FUNCTION sales.stamp_loss_resolution_root();

CREATE OR REPLACE FUNCTION sales.loss_composition(deal_key integer) RETURNS jsonb
LANGUAGE sql STABLE AS $$
 SELECT COALESCE(jsonb_agg(jsonb_build_object('id',id,'deal_id',deal_id,'kind',kind,
   'version',version,'content_sha256',content_sha256,'supersedes_id',supersedes_id,
   'superseded_by_id',superseded_by_id) ORDER BY id),'[]'::jsonb)
 FROM sales.deal_document WHERE deal_id=deal_key AND kind='invoice'
$$;

CREATE OR REPLACE FUNCTION sales.loss_kind(funnel_key text, stage_key text) RETURNS boolean
LANGUAGE sql STABLE AS $$
 SELECT CASE WHEN EXISTS(SELECT 1 FROM sales.stage WHERE funnel=funnel_key AND is_active)
   THEN EXISTS(SELECT 1 FROM sales.stage WHERE funnel=funnel_key AND code=stage_key AND is_active AND kind='lost')
   ELSE (funnel_key,stage_key) IN (('new_clients','lost'),('repeat_clients','rp_lost'),('tenders','tn_lost')) END
$$;

CREATE OR REPLACE FUNCTION sales.guard_loss_request() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE d sales.deal%ROWTYPE;
BEGIN
 IF TG_OP='DELETE' THEN RAISE EXCEPTION 'Loss requests are durable'; END IF;
 IF TG_OP='UPDATE' THEN
   IF (to_jsonb(NEW)-'state') IS DISTINCT FROM (to_jsonb(OLD)-'state')
     OR OLD.state<>'pending' OR NEW.state NOT IN ('finalized','withdrawn')
     OR NOT EXISTS(SELECT 1 FROM sales.deal_loss_resolution r WHERE r.request_id=NEW.id AND r.action=NEW.state)
   THEN RAISE EXCEPTION 'Immutable command; resolution required'; END IF;
   RETURN NEW;
 END IF;
 PERFORM 1 FROM accounting.organization WHERE id=NEW.organization_id FOR UPDATE;
 SELECT * INTO STRICT d FROM sales.deal WHERE id=NEW.deal_id FOR UPDATE;
 PERFORM id FROM sales.deal_document WHERE deal_id=d.id AND kind='invoice' ORDER BY id FOR UPDATE;
 IF NEW.state IS DISTINCT FROM 'pending'
   OR NOT EXISTS(SELECT 1 FROM sales.deal_ownership WHERE deal_id=d.id AND organization_id=NEW.organization_id)
   OR NEW.command->>'request_key' IS DISTINCT FROM NEW.id
   OR (NEW.command->>'organization_id')::integer IS DISTINCT FROM NEW.organization_id
   OR COALESCE(btrim(NEW.command->>'reason_code'),'')=''
   OR NEW.snapshot->>'funnel' IS DISTINCT FROM d.funnel
   OR NEW.snapshot->>'stage' IS DISTINCT FROM d.stage
   OR NEW.snapshot::jsonb->'invoices' IS DISTINCT FROM sales.loss_composition(d.id)
   OR sales.loss_kind(d.funnel,d.stage)
   OR sales.loss_kind(d.funnel,NEW.snapshot->>'lost_stage') IS NOT TRUE
 THEN RAISE EXCEPTION 'Invalid loss request scope or composition'; END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER deal_loss_request_guard BEFORE INSERT OR UPDATE OR DELETE ON sales.deal_loss_request
FOR EACH ROW EXECUTE FUNCTION sales.guard_loss_request();
CREATE TRIGGER deal_loss_request_no_truncate BEFORE TRUNCATE ON sales.deal_loss_request
EXECUTE FUNCTION sales.reject_ownership_change();
CREATE TRIGGER deal_loss_resolution_immutable BEFORE UPDATE OR DELETE ON sales.deal_loss_resolution
FOR EACH ROW EXECUTE FUNCTION sales.reject_ownership_change();
CREATE TRIGGER deal_loss_resolution_no_truncate BEFORE TRUNCATE ON sales.deal_loss_resolution
EXECUTE FUNCTION sales.reject_ownership_change();

CREATE OR REPLACE FUNCTION sales.guard_loss_composition() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE old_deal integer; new_deal integer; old_kind text; new_kind text;
BEGIN
 IF TG_OP<>'INSERT' THEN old_deal:=OLD.deal_id; old_kind:=OLD.kind; END IF;
 IF TG_OP<>'DELETE' THEN new_deal:=NEW.deal_id; new_kind:=NEW.kind; END IF;
 IF TG_OP='UPDATE' AND ROW(OLD.id,OLD.deal_id,OLD.kind,OLD.version,OLD.content_sha256,OLD.supersedes_id,OLD.superseded_by_id)
   IS NOT DISTINCT FROM ROW(NEW.id,NEW.deal_id,NEW.kind,NEW.version,NEW.content_sha256,NEW.supersedes_id,NEW.superseded_by_id)
 THEN RETURN NEW; END IF;
 IF old_kind='invoice' OR new_kind='invoice' THEN
   -- Serialize phantom inserts/moves with request creation and finalization.
   PERFORM id FROM sales.deal WHERE id IN (old_deal,new_deal) ORDER BY id FOR UPDATE;
   IF EXISTS(SELECT 1 FROM sales.deal_loss_request WHERE deal_id IN (old_deal,new_deal) AND state='pending')
   THEN RAISE EXCEPTION 'Pending loss request freezes every invoice identity'; END IF;
 END IF;
 IF TG_OP='DELETE' THEN RETURN OLD; END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER deal_loss_invoice_composition BEFORE INSERT OR UPDATE OR DELETE ON sales.deal_document
FOR EACH ROW EXECUTE FUNCTION sales.guard_loss_composition();
CREATE TRIGGER deal_loss_invoice_no_truncate BEFORE TRUNCATE ON sales.deal_document
EXECUTE FUNCTION sales.reject_ownership_change();

CREATE OR REPLACE FUNCTION sales.guard_loss_stage() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF TG_OP='UPDATE' AND ROW(NEW.funnel,NEW.stage) IS NOT DISTINCT FROM ROW(OLD.funnel,OLD.stage)
 THEN RETURN NEW; END IF;
 -- Coordinate rare semantic edits with entering a populated stage, without
 -- serializing invoice/document operations that only lock the unchanged deal.
 PERFORM pg_advisory_xact_lock(1935764588, 1);
 IF EXISTS(SELECT 1 FROM sales.deal_loss_request WHERE deal_id=NEW.id AND state='pending')
 THEN RAISE EXCEPTION 'Pending loss request blocks stage/funnel transitions'; END IF;
 IF sales.loss_kind(NEW.funnel,NEW.stage) THEN
   IF TG_OP='INSERT' THEN RAISE EXCEPTION 'New deal cannot start lost'; END IF;
   IF NOT EXISTS(SELECT 1 FROM sales.deal_loss_request q JOIN sales.deal_loss_resolution r ON r.request_id=q.id
     WHERE q.deal_id=NEW.id AND q.state='finalized' AND r.action='finalized'
       AND r.born_root_transaction = txid_current()
       AND q.snapshot->>'funnel'=NEW.funnel AND q.snapshot->>'stage'=OLD.stage
       AND q.snapshot->>'lost_stage'=NEW.stage AND OLD.funnel=NEW.funnel)
   THEN RAISE EXCEPTION 'Lost stage requires exact durable resolution'; END IF;
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER deal_loss_stage_guard BEFORE INSERT OR UPDATE ON sales.deal
FOR EACH ROW EXECUTE FUNCTION sales.guard_loss_stage();

CREATE OR REPLACE FUNCTION sales.guard_loss_stage_definition() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 PERFORM pg_advisory_xact_lock(1935764588, 1);
 IF TG_OP='UPDATE' AND ROW(OLD.funnel,OLD.code,OLD.kind,OLD.is_active)
    IS DISTINCT FROM ROW(NEW.funnel,NEW.code,NEW.kind,NEW.is_active)
    AND EXISTS(SELECT 1 FROM sales.deal WHERE funnel IN (OLD.funnel,NEW.funnel))
 THEN RAISE EXCEPTION 'Populated funnel semantics require explicit migration'; END IF;
 IF TG_OP='INSERT' AND NEW.is_active AND NEW.kind='lost'
    AND EXISTS(SELECT 1 FROM sales.deal WHERE funnel=NEW.funnel AND stage=NEW.code)
 THEN RAISE EXCEPTION 'Cannot reclassify populated stage as lost'; END IF;
 IF TG_OP='DELETE' THEN
   IF EXISTS(SELECT 1 FROM sales.deal WHERE funnel=OLD.funnel)
   THEN RAISE EXCEPTION 'Populated funnel semantics require explicit migration'; END IF;
   RETURN OLD;
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER deal_loss_stage_definition BEFORE INSERT OR UPDATE OR DELETE ON sales.stage
FOR EACH ROW EXECUTE FUNCTION sales.guard_loss_stage_definition();

CREATE UNIQUE INDEX uq_loss_request_event ON public.outbox_event ((payload->>'request_id'))
WHERE event_type='sales.deal.loss_requested';
CREATE UNIQUE INDEX uq_loss_resolution_event ON public.outbox_event ((payload->>'resolution_id'))
WHERE event_type IN ('sales.deal.loss_finalized','sales.deal.loss_withdrawn');

-- Source-owned cancellation package predicate copied verbatim from accounting_ownership_guards.sql,
-- with an explicit receipt key and current reserve/pick checks added for later finalization.
CREATE OR REPLACE FUNCTION sales.check_loss_cancelled_invoice(cancellation_key text) RETURNS void
LANGUAGE plpgsql AS $$
DECLARE
  c sales.invoice_cancellation_receipt%ROWTYPE;
  d sales.deal_document%ROWTYPE;
  r sales.invoice_fulfillment_review%ROWTYPE;
  m sales.invoice_money_reconciliation%ROWTYPE;
  w wms.invoice_reservation_release%ROWTYPE;
BEGIN
  SELECT * INTO c FROM sales.invoice_cancellation_receipt WHERE id=cancellation_key;
  SELECT * INTO d FROM sales.deal_document WHERE id=c.document_id;
  SELECT * INTO r FROM sales.invoice_fulfillment_review WHERE id=c.fulfillment_review_id;
  SELECT * INTO m FROM sales.invoice_money_reconciliation WHERE id=r.money_reconciliation_id;
  SELECT * INTO w FROM wms.invoice_reservation_release WHERE id=c.release_id;
  IF jsonb_typeof(c.snapshot::jsonb) IS DISTINCT FROM 'object'
    OR jsonb_typeof(r.request::jsonb->'external_sources') IS DISTINCT FROM 'array'
    OR jsonb_typeof(c.snapshot::jsonb->'money'->'facts') IS DISTINCT FROM 'object'
    OR COALESCE(c.snapshot->'money'->>'state','') NOT IN ('no_receipts','fully_refunded')
    OR COALESCE(c.request->>'acknowledge_invoice_invalidation','') <> 'true'
    OR COALESCE(r.snapshot->>'basis_digest','') !~ '^[a-f0-9]{64}$'
    OR jsonb_typeof(r.snapshot::jsonb->'sections') IS DISTINCT FROM 'object'
    OR EXISTS (SELECT 1 FROM jsonb_each(r.snapshot::jsonb->'sections') section
      WHERE jsonb_typeof(section.value) IS DISTINCT FROM 'object'
        OR jsonb_typeof(section.value->'facts') IS DISTINCT FROM 'object'
        OR COALESCE(section.value->>'sha256','') !~ '^[a-f0-9]{64}$')
    OR COALESCE(c.snapshot->'withdrawal'->>'before_snapshot_sha256','') !~ '^[a-f0-9]{64}$'
    OR COALESCE(r.snapshot->'sections'->'logistics_shipment'->'facts'->>'sha256','') !~ '^[a-f0-9]{64}$'
    OR COALESCE(r.snapshot::jsonb->'sections' ?& ARRAY['wms_issue','wms_pick','logistics_shipment','accounting_issue','legacy_fulfillment'],false) IS NOT TRUE
    OR jsonb_typeof(m.facts::jsonb->'revalidated_banks') IS DISTINCT FROM 'array'
    OR jsonb_typeof(m.facts::jsonb->'settlements') IS DISTINCT FROM 'array'
    OR EXISTS (SELECT 1 FROM jsonb_array_elements(m.facts::jsonb->'revalidated_banks') b
      WHERE NOT EXISTS (SELECT 1 FROM accounting.entry e WHERE e.id=(b->>'entry_id')::integer
        AND e.organization_id=c.organization_id AND e.digest=b->>'digest'
        AND e.operation='bank_settlement' AND e.rule_version='bank-byn-v1'
        AND e.opening=false AND e.correction_of IS NULL)
      OR EXISTS (SELECT 1 FROM accounting.entry e WHERE e.correction_of=(b->>'entry_id')::integer))
    OR EXISTS (SELECT 1 FROM accounting.entry e JOIN accounting.line l ON l.entry_id=e.id
      WHERE e.organization_id=c.organization_id AND l.dimensions->>'settlement_document'='sales:document:'||d.id
        AND (e.operation='bank_settlement' OR EXISTS (SELECT 1 FROM accounting.line cash WHERE cash.entry_id=e.id AND cash.cash))
        AND NOT EXISTS (SELECT 1 FROM jsonb_array_elements(m.facts::jsonb->'revalidated_banks') b
           WHERE (b->>'entry_id')::integer=e.id AND b->>'digest'=e.digest))
    OR (SELECT count(*) FROM sales.invoice_settlement s WHERE s.document_id=d.id)
       IS DISTINCT FROM jsonb_array_length(m.facts::jsonb->'settlements')::bigint
    OR EXISTS (SELECT 1 FROM sales.invoice_settlement s WHERE s.document_id=d.id
      AND NOT EXISTS (SELECT 1 FROM jsonb_array_elements(m.facts::jsonb->'settlements') b
        WHERE (b->>'id')::integer=s.id AND (b->>'organization_id')::integer=s.organization_id
          AND (b->>'bank_entry_id')::integer=s.bank_entry_id AND b->>'direction'=s.direction
          AND (b->>'amount')::numeric=s.amount AND (b->>'refund_of')::integer IS NOT DISTINCT FROM s.refund_of
          AND b->'snapshot'=s.snapshot::jsonb))
    OR d.status IS DISTINCT FROM 'cancelled' OR d.reserve_status IS DISTINCT FROM 'released'
    OR c.document_version IS DISTINCT FROM d.version OR c.content_sha256 IS DISTINCT FROM d.content_sha256
    OR r.document_id IS DISTINCT FROM d.id OR r.organization_id IS DISTINCT FROM c.organization_id
    OR m.document_id IS DISTINCT FROM d.id OR m.organization_id IS DISTINCT FROM c.organization_id
    OR w.document_id IS DISTINCT FROM d.id OR w.organization_id IS DISTINCT FROM c.organization_id
    OR w.source_key IS DISTINCT FROM c.id
    OR c.snapshot->>'basis_digest' IS DISTINCT FROM r.snapshot->>'basis_digest'
    OR c.snapshot->>'review_digest' IS DISTINCT FROM r.digest
    OR c.snapshot->'money'->>'digest' IS DISTINCT FROM m.basis_digest
    OR (c.snapshot->'money'->'facts')::jsonb IS DISTINCT FROM (r.snapshot->'money'->'facts')::jsonb
    OR (c.snapshot->'money'->'facts')::jsonb IS DISTINCT FROM m.facts::jsonb
    OR c.snapshot->'release'->>'digest' IS DISTINCT FROM w.digest
    OR (w.snapshot->'fulfillment'->>'review_id') IS DISTINCT FROM r.id
    OR (r.request->>'all_fulfillment_sources_identified') IS DISTINCT FROM 'true'
    OR (m.request->>'all_money_sources_checked') IS DISTINCT FROM 'true'
    OR m.history_through < c.created_at::date
    OR m.history_from > d.issued_at::date
    OR jsonb_array_length(r.request::jsonb->'external_sources') < 1
    OR EXISTS (SELECT 1 FROM jsonb_array_elements(r.request::jsonb->'external_sources') e
        WHERE e->>'confirmed_no_fulfillment' IS DISTINCT FROM 'true'
          OR e->>'history_from' IS NULL OR e->>'history_through' IS NULL
          OR COALESCE(btrim(e->>'system'),'')='' OR COALESCE(btrim(e->>'reference'),'')=''
          OR (e->>'history_from')::date > d.issued_at::date
          OR (e->>'history_through')::date < c.created_at::date)
    OR jsonb_typeof(c.snapshot::jsonb->'withdrawal') IS DISTINCT FROM 'object'
    OR c.snapshot->'withdrawal'->'cancel_receipt_identity'->>'cancellation_receipt_id' IS DISTINCT FROM c.id
    OR c.snapshot->'withdrawal'->'cancel_receipt_identity'->>'cancellation_request_sha256' IS DISTINCT FROM c.request_hash
    OR c.snapshot->'withdrawal'->>'before_snapshot_sha256' IS DISTINCT FROM
       r.snapshot->'sections'->'logistics_shipment'->'facts'->>'sha256'
    OR EXISTS (SELECT 1 FROM logistics.shipment_invoice_binding b JOIN logistics.shipment s ON s.id=b.shipment_id
       WHERE b.organization_id=c.organization_id AND b.document_id=d.id AND s.status IS DISTINCT FROM 'withdrawn')
    OR EXISTS (SELECT 1 FROM logistics.rfq_invoice_binding b JOIN logistics.carrier_rfq q ON q.id=b.rfq_id
       WHERE b.organization_id=c.organization_id AND b.document_id=d.id AND q.status IS DISTINCT FROM 'withdrawn')
    OR EXISTS (SELECT 1 FROM jsonb_array_elements(r.snapshot::jsonb->'sections'->'logistics_shipment'->'facts'->'shipments') e
       WHERE NOT EXISTS (SELECT 1 FROM logistics.shipment s WHERE s.id=(e->'row'->>'id')::integer AND s.status='withdrawn'))
    OR EXISTS (SELECT 1 FROM jsonb_array_elements(r.snapshot::jsonb->'sections'->'logistics_shipment'->'facts'->'rfqs') e
       WHERE NOT EXISTS (SELECT 1 FROM logistics.carrier_rfq q WHERE q.id=(e->'row'->>'id')::integer AND q.status='withdrawn'))
    OR EXISTS (SELECT 1 FROM jsonb_array_elements(r.snapshot::jsonb->'sections'->'logistics_shipment'->'facts'->'intakes') e
       WHERE NOT EXISTS (SELECT 1 FROM logistics.shipment_intake i WHERE i.id=(e->'row'->>'id')::integer
         AND ((e->'row'->>'state'='resolved' AND i.state='resolved')
           OR (e->'row'->>'state'='pending' AND i.state='withdrawn' AND i.pending_reason='invoice_cancelled:'||c.id))))
    OR EXISTS (SELECT 1 FROM wms.physical_shipment_act WHERE document_id=d.id)
    OR NOT EXISTS (SELECT 1 FROM sales.deal_ownership WHERE deal_id=d.deal_id AND organization_id=c.organization_id)
    OR EXISTS (SELECT 1 FROM sales.invoice_settlement receipt WHERE receipt.document_id=d.id
      AND receipt.direction='receipt' AND receipt.amount IS DISTINCT FROM
      (SELECT COALESCE(SUM(refund.amount),0) FROM sales.invoice_settlement refund
       WHERE refund.refund_of=receipt.id AND refund.direction='refund'
         AND refund.document_id=d.id AND refund.organization_id=c.organization_id))
    OR NOT EXISTS (SELECT 1 FROM public.outbox_event e WHERE e.event_type='sales.invoice.cancelled'
      AND e.payload->>'cancellation_id'=c.id AND e.payload->>'cancellation_digest'=c.digest
      AND (e.payload->>'document_id')::integer=d.id)
  THEN RAISE EXCEPTION 'Incomplete or inconsistent invoice cancellation package';
  END IF;
  IF EXISTS (SELECT 1 FROM wms.invoice_reservation_release_line l
    LEFT JOIN wms.reservation_version v ON v.id=l.after_id
    WHERE l.release_id=w.id AND (v.id IS NULL OR v.qty<>0 OR EXISTS(
      SELECT 1 FROM wms.reservation_version newer WHERE newer.organization_id=w.organization_id
      AND newer.source=l.source AND newer.version>v.version)))
    OR NOT EXISTS(SELECT 1 FROM wms.reservation_event_state WHERE document_id=d.id AND state='released')
    OR EXISTS(SELECT 1 FROM wms.task t JOIN wms.reservation_pick p ON p.task_id=t.id
      WHERE p.document_id=d.id AND (t.status<>'canceled' OR t.done_at IS NOT NULL))
  THEN RAISE EXCEPTION 'Loss requires current released reserve and canceled picks'; END IF;
  RETURN;
END;
$$;

-- The final package checker is DEFERRED because receipt insertion, request state,
-- deal history/stage and outbox are one transaction, in that order.
CREATE OR REPLACE FUNCTION sales.check_loss_package() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE q sales.deal_loss_request%ROWTYPE; r sales.deal_loss_resolution%ROWTYPE;
  d sales.deal%ROWTYPE; invoice jsonb; c sales.invoice_cancellation_receipt%ROWTYPE;
BEGIN
 IF TG_TABLE_NAME='deal_loss_request' THEN SELECT * INTO STRICT q FROM sales.deal_loss_request WHERE id=NEW.id;
 ELSE SELECT * INTO STRICT q FROM sales.deal_loss_request WHERE id=NEW.request_id; END IF;
 SELECT * INTO STRICT d FROM sales.deal WHERE id=q.deal_id;
 IF NOT EXISTS(SELECT 1 FROM public.outbox_event e WHERE e.event_type='sales.deal.loss_requested'
   AND e.payload->>'request_id'=q.id AND e.payload->>'digest'=q.digest
   AND (e.payload->>'deal_id')::integer=q.deal_id AND (e.payload->>'organization_id')::integer=q.organization_id)
 THEN RAISE EXCEPTION 'Loss request requires unique outbox evidence'; END IF;
 IF q.state='pending' THEN
   IF EXISTS(SELECT 1 FROM sales.deal_loss_resolution WHERE request_id=q.id)
   THEN RAISE EXCEPTION 'Resolution requires terminal request projection'; END IF;
   RETURN NEW;
 END IF;
 SELECT * INTO STRICT r FROM sales.deal_loss_resolution WHERE request_id=q.id;
 IF r.action IS DISTINCT FROM q.state OR r.command->>'request_key' IS DISTINCT FROM r.id
   OR r.command->>'expected_request_digest' IS DISTINCT FROM q.digest
   OR r.snapshot->>'request_digest' IS DISTINCT FROM q.digest
   OR COALESCE(btrim(r.command->>'evidence'),'')=''
   OR r.snapshot::jsonb->'composition' IS DISTINCT FROM q.snapshot::jsonb->'invoices'
   OR q.snapshot::jsonb->'invoices' IS DISTINCT FROM sales.loss_composition(q.deal_id)
   OR NOT EXISTS(SELECT 1 FROM public.outbox_event e WHERE e.event_type='sales.deal.loss_'||r.action
     AND e.payload->>'resolution_id'=r.id AND e.payload->>'request_id'=q.id AND e.payload->>'digest'=r.digest)
 THEN RAISE EXCEPTION 'Incomplete loss resolution package'; END IF;
 IF r.action='withdrawn' THEN RETURN NEW; END IF;
 IF r.action<>'finalized' OR d.funnel IS DISTINCT FROM q.snapshot->>'funnel'
   OR d.stage IS DISTINCT FROM q.snapshot->>'lost_stage' OR NOT sales.loss_kind(d.funnel,d.stage)
   OR d.lost_reason_code IS DISTINCT FROM q.command->>'reason_code'
   OR d.lost_comment IS DISTINCT FROM q.command->>'comment' OR d.closed_date IS NULL
   OR jsonb_array_length(r.snapshot::jsonb->'invoices') IS DISTINCT FROM jsonb_array_length(q.snapshot::jsonb->'invoices')
 THEN RAISE EXCEPTION 'Loss finalization stage or invoice set mismatch'; END IF;
 FOR invoice IN SELECT * FROM jsonb_array_elements(q.snapshot::jsonb->'invoices') LOOP
   SELECT * INTO STRICT c FROM sales.invoice_cancellation_receipt WHERE document_id=(invoice->>'id')::integer;
   IF c.organization_id<>q.organization_id OR c.document_version IS DISTINCT FROM (invoice->>'version')::integer
     OR c.content_sha256 IS DISTINCT FROM invoice->>'content_sha256'
     OR NOT EXISTS(SELECT 1 FROM jsonb_array_elements(r.snapshot::jsonb->'invoices') e
       WHERE (e->>'document_id')::integer=c.document_id AND e->>'ready'='true'
         AND e->'blockers'='[]'::jsonb AND e->'cancellation_receipt'->>'cancellation_id'=c.id
         AND e->'cancellation_receipt'->>'digest'=c.digest)
   THEN RAISE EXCEPTION 'Loss finalization missing exact cancellation evidence'; END IF;
   PERFORM sales.check_loss_cancelled_invoice(c.id);
 END LOOP;
 RETURN NEW;
END $$;
CREATE CONSTRAINT TRIGGER deal_loss_request_package AFTER INSERT OR UPDATE ON sales.deal_loss_request
DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION sales.check_loss_package();
CREATE CONSTRAINT TRIGGER deal_loss_resolution_package AFTER INSERT ON sales.deal_loss_resolution
DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION sales.check_loss_package();
