"""Read-only shipment projection for deal and client document registers.

Sales owns the register view; WMS remains the source of shipment evidence.
The projection never calls an internal WMS ORM model directly and never labels
an internal act as a statutory TN/TTN.
"""
from fastapi import HTTPException


async def project(core, session, organization_id: int, document_ids: list[int]) -> dict:
    gateway = getattr(getattr(core, "services", None), "wms_reservations", None)
    method = getattr(gateway, "invoice_shipments_register", None)
    if not callable(method):
        return {"status": "unavailable", "tn_ttn_status": "not_connected", "items": []}
    result = await method(session, organization_id, document_ids)
    if not isinstance(result, dict) or result.get("status") != "internal_acts_only" \
            or result.get("tn_ttn_status") != "not_certified" or not isinstance(result.get("items"), list):
        raise HTTPException(503, "Shipment register returned an invalid source projection")
    return result
