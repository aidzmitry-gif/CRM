-- Unallocated additive guard. Install only with the invoice notification table.
CREATE OR REPLACE FUNCTION sales.protect_invoice_notification() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'Invoice notification receipts are immutable';
END;
$$;

CREATE TRIGGER invoice_notification_immutable
BEFORE UPDATE OR DELETE ON sales.invoice_notification
FOR EACH ROW EXECUTE FUNCTION sales.protect_invoice_notification();

CREATE TRIGGER invoice_notification_no_truncate
BEFORE TRUNCATE ON sales.invoice_notification
FOR EACH STATEMENT EXECUTE FUNCTION sales.protect_invoice_notification();
