CREATE OR REPLACE FUNCTION sales.reject_ownership_change() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'Confirmed sales ownership is immutable';
END;
$$;
CREATE TRIGGER sales_ownership_immutable BEFORE UPDATE OR DELETE ON sales.deal_ownership
FOR EACH ROW EXECUTE FUNCTION sales.reject_ownership_change();
CREATE TRIGGER sales_ownership_no_truncate BEFORE TRUNCATE ON sales.deal_ownership
FOR EACH STATEMENT EXECUTE FUNCTION sales.reject_ownership_change();

-- Paid cancellation requires the durable, deferred-validated ERP package.
CREATE OR REPLACE FUNCTION sales.protect_invoice_money() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    IF OLD.status = 'paid' OR EXISTS (
      SELECT 1 FROM sales.invoice_settlement WHERE document_id = OLD.id
    ) THEN
      RAISE EXCEPTION 'Invoice with recorded money cannot be deleted';
    END IF;
    RETURN OLD;
  END IF;
  IF OLD.status = 'cancelled' AND EXISTS (SELECT 1 FROM sales.invoice_cancellation_receipt WHERE document_id=OLD.id)
     AND (NEW.status IS DISTINCT FROM OLD.status OR NEW.reserve_status IS DISTINCT FROM OLD.reserve_status) THEN
    RAISE EXCEPTION 'Confirmed invoice cancellation is terminal';
  END IF;
  IF NEW.status = 'cancelled' AND NEW.status IS DISTINCT FROM OLD.status
     AND EXISTS (SELECT 1 FROM sales.invoice_cancellation_receipt c WHERE c.document_id=NEW.id
       AND c.document_version=NEW.version AND c.content_sha256=NEW.content_sha256) THEN
    RETURN NEW; -- deferred package guard below must pass in the same transaction
  END IF;
  IF NEW.status = 'cancelled' AND NEW.status IS DISTINCT FROM OLD.status
     AND EXISTS (SELECT 1 FROM sales.invoice_issuance_receipt WHERE document_id=NEW.id) THEN
    RAISE EXCEPTION 'ERP invoice requires the atomic cancellation workflow';
  END IF;
  IF OLD.status = 'paid' AND NEW.status IS DISTINCT FROM 'paid' THEN
    RAISE EXCEPTION 'Paid invoice requires confirmed full refund reconciliation';
  END IF;
  IF NEW.status = 'cancelled' AND NEW.status IS DISTINCT FROM OLD.status AND EXISTS (
    SELECT 1 FROM sales.invoice_settlement WHERE document_id = OLD.id AND direction = 'receipt'
  ) THEN
    RAISE EXCEPTION 'Invoice receipt requires full refund reconciliation before cancellation';
  END IF;
  RETURN NEW;
END;
$$;
CREATE TRIGGER sales_invoice_money_guard BEFORE UPDATE OR DELETE ON sales.deal_document
FOR EACH ROW EXECUTE FUNCTION sales.protect_invoice_money();
CREATE TRIGGER sales_invoice_money_no_truncate BEFORE TRUNCATE ON sales.deal_document
FOR EACH STATEMENT EXECUTE FUNCTION sales.reject_ownership_change();
CREATE TRIGGER sales_settlement_immutable BEFORE UPDATE OR DELETE ON sales.invoice_settlement
FOR EACH ROW EXECUTE FUNCTION sales.reject_ownership_change();
CREATE TRIGGER sales_settlement_no_truncate BEFORE TRUNCATE ON sales.invoice_settlement
FOR EACH STATEMENT EXECUTE FUNCTION sales.reject_ownership_change();
CREATE TRIGGER sales_money_reconciliation_immutable BEFORE UPDATE OR DELETE ON sales.invoice_money_reconciliation
FOR EACH ROW EXECUTE FUNCTION sales.reject_ownership_change();
CREATE TRIGGER sales_money_reconciliation_no_truncate BEFORE TRUNCATE ON sales.invoice_money_reconciliation
FOR EACH STATEMENT EXECUTE FUNCTION sales.reject_ownership_change();

CREATE TRIGGER sales_item_request_immutable BEFORE UPDATE OR DELETE ON sales.deal_item_request
FOR EACH ROW EXECUTE FUNCTION sales.reject_ownership_change();
CREATE TRIGGER sales_item_request_no_truncate BEFORE TRUNCATE ON sales.deal_item_request
FOR EACH STATEMENT EXECUTE FUNCTION sales.reject_ownership_change();

CREATE TRIGGER sales_price_request_immutable BEFORE UPDATE OR DELETE ON sales.price_quote_request
FOR EACH ROW EXECUTE FUNCTION sales.reject_ownership_change();
CREATE TRIGGER sales_price_request_no_truncate BEFORE TRUNCATE ON sales.price_quote_request
FOR EACH STATEMENT EXECUTE FUNCTION sales.reject_ownership_change();

CREATE TRIGGER sales_fulfillment_review_immutable BEFORE UPDATE OR DELETE ON sales.invoice_fulfillment_review
FOR EACH ROW EXECUTE FUNCTION sales.reject_ownership_change();
CREATE TRIGGER sales_fulfillment_review_no_truncate BEFORE TRUNCATE ON sales.invoice_fulfillment_review
FOR EACH STATEMENT EXECUTE FUNCTION sales.reject_ownership_change();

CREATE TRIGGER sales_cancellation_receipt_immutable BEFORE UPDATE OR DELETE ON sales.invoice_cancellation_receipt
FOR EACH ROW EXECUTE FUNCTION sales.reject_ownership_change();
CREATE TRIGGER sales_cancellation_receipt_no_truncate BEFORE TRUNCATE ON sales.invoice_cancellation_receipt
FOR EACH STATEMENT EXECUTE FUNCTION sales.reject_ownership_change();

CREATE OR REPLACE FUNCTION sales.check_cancellation_package() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
  c sales.invoice_cancellation_receipt%ROWTYPE;
  d sales.deal_document%ROWTYPE;
  r sales.invoice_fulfillment_review%ROWTYPE;
  m sales.invoice_money_reconciliation%ROWTYPE;
  w wms.invoice_reservation_release%ROWTYPE;
BEGIN
  SELECT * INTO c FROM sales.invoice_cancellation_receipt WHERE id=NEW.id;
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
  RETURN NEW;
END;
$$;
CREATE CONSTRAINT TRIGGER sales_cancellation_package AFTER INSERT ON sales.invoice_cancellation_receipt
DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION sales.check_cancellation_package();
