from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any

from api.invoices import InvoiceStatus

DEFAULT_WOOCOMMERCE_STATUS_MAPPING: dict[str, str] = {
    InvoiceStatus.COMPLETE: "completed",
    InvoiceStatus.PAID: "on-hold",
    InvoiceStatus.CONFIRMED: "processing",
    InvoiceStatus.PENDING: "pending",
    InvoiceStatus.INVALID: "failed",
    InvoiceStatus.EXPIRED: "cancelled",
    InvoiceStatus.REFUNDED: "refunded",
}

DEFAULT_SHOPIFY_STATUS_MAPPING: dict[str, str] = {
    InvoiceStatus.COMPLETE: "paid",
    InvoiceStatus.PAID: "pending",
    InvoiceStatus.CONFIRMED: "pending",
    InvoiceStatus.PENDING: "pending",
    InvoiceStatus.INVALID: "voided",
    InvoiceStatus.EXPIRED: "expired",
    InvoiceStatus.REFUNDED: "refunded",
}


def compute_ipn_signature(
    payload: str | bytes | dict[str, Any],
    secret: str,
    timestamp: int | str | None = None,
) -> str:
    """Compute HMAC-SHA256 signature for IPN payload with optional timestamp."""
    if isinstance(payload, dict):
        payload_str = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    elif isinstance(payload, bytes):
        payload_str = payload.decode("utf-8")
    else:
        payload_str = str(payload)

    to_sign = f"{timestamp}.{payload_str}" if timestamp is not None else payload_str
    return hmac.new(
        secret.encode("utf-8"),
        to_sign.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def verify_ipn_signature(
    payload: str | bytes | dict[str, Any],
    signature: str,
    secret: str,
    timestamp: int | str | None = None,
    tolerance_seconds: int = 300,
) -> bool:
    """Verify HMAC-SHA256 signature for incoming/outgoing IPN payloads.

    Prevents timing attacks via hmac.compare_digest and replay attacks via timestamp tolerance window.
    Supports either raw hex signatures or Stripe/Bitcart format 't=<ts>,v1=<sig>'.
    """
    if not signature or not secret:
        return False

    clean_signature = signature.strip()
    extracted_timestamp = timestamp

    # Parse 't=...,v1=...' header format if present
    if "t=" in clean_signature and "v1=" in clean_signature:
        parts = dict(part.split("=", 1) for part in clean_signature.split(",") if "=" in part)
        extracted_timestamp = parts.get("t")
        clean_signature = parts.get("v1", "")

    # Check replay window if timestamp is available
    if extracted_timestamp is not None:
        try:
            ts_float = float(extracted_timestamp)
            current_time = time.time()
            if abs(current_time - ts_float) > tolerance_seconds:
                return False
        except (ValueError, TypeError):
            return False

    expected_signature = compute_ipn_signature(payload, secret, timestamp=extracted_timestamp)
    return hmac.compare_digest(clean_signature.lower(), expected_signature.lower())


class WooCommerceAdapter:
    """Adapter for WooCommerce e-commerce integration and status synchronization."""

    @classmethod
    def map_status(cls, status: str, custom_mapping: dict[str, str] | None = None) -> str:
        """Map Bitcart invoice status to WooCommerce order status."""
        mapping = {**DEFAULT_WOOCOMMERCE_STATUS_MAPPING, **(custom_mapping or {})}
        return mapping.get(status, "on-hold")

    @classmethod
    def build_order_note(
        cls,
        invoice_id: str,
        status: str,
        sent_amount: Any = 0,
        currency: str = "",
        tx_hashes: list[str] | None = None,
    ) -> str:
        """Generate human-readable order note for WooCommerce store admin."""
        tx_info = f" (TX: {', '.join(tx_hashes)})" if tx_hashes else ""
        amount_info = f" of {sent_amount} {currency}" if currency and sent_amount else ""
        return f"[Bitcart] Invoice {invoice_id} status updated to '{status}'{amount_info}{tx_info}."

    @classmethod
    def build_webhook_payload(
        cls,
        invoice_id: str,
        status: str,
        store_id: str | None = None,
        price: Any = None,
        currency: str | None = None,
        paid_currency: str | None = None,
        sent_amount: Any = None,
        tx_hashes: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Construct a standardized webhook payload for WooCommerce stores."""
        wc_status = cls.map_status(status)
        order_id = (metadata or {}).get("order_id") or (metadata or {}).get("woocommerce_order_id")
        return {
            "event": "invoice_status_changed",
            "invoice_id": invoice_id,
            "bitcart_status": status,
            "woocommerce_status": wc_status,
            "woocommerce_order_id": order_id,
            "store_id": store_id,
            "price": str(price) if price is not None else None,
            "currency": currency,
            "paid_currency": paid_currency,
            "sent_amount": str(sent_amount) if sent_amount is not None else None,
            "tx_hashes": tx_hashes or [],
            "timestamp": int(time.time()),
        }


class ShopifyAdapter:
    """Adapter for Shopify e-commerce integration."""

    @classmethod
    def map_financial_status(cls, status: str) -> str:
        """Map Bitcart invoice status to Shopify financial_status."""
        return DEFAULT_SHOPIFY_STATUS_MAPPING.get(status, "pending")

    @classmethod
    def build_transaction_payload(
        cls,
        invoice_id: str,
        status: str,
        amount: Any,
        currency: str,
        test_mode: bool = False,
    ) -> dict[str, Any]:
        """Build transaction response payload compatible with Shopify checkout/webhook."""
        financial_status = cls.map_financial_status(status)
        is_success = financial_status == "paid"
        return {
            "transaction": {
                "kind": "sale",
                "status": "success" if is_success else "pending" if financial_status == "pending" else "failure",
                "amount": str(amount),
                "currency": currency,
                "gateway": "Bitcart",
                "test": test_mode,
                "authorization": invoice_id,
                "message": f"Bitcart payment {status}",
            }
        }
