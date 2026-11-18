import time
from dataclasses import dataclass
from unittest.mock import AsyncMock, patch

import pytest

from api.ext.ecommerce import (
    ShopifyAdapter,
    WooCommerceAdapter,
    compute_ipn_signature,
    verify_ipn_signature,
)
from api.invoices import InvoiceStatus
from api.schemas.stores import (
    StorePluginSettings,
    StoreShopifySettings,
    StoreWebhookSettings,
    StoreWooCommerceSettings,
)
from api.services.ipn_sender import IPNSender

# ---------------------------------------------------------------------------
# Schema Tests
# ---------------------------------------------------------------------------


def test_store_plugin_settings_defaults():
    settings = StorePluginSettings()
    assert isinstance(settings.shopify, StoreShopifySettings)
    assert isinstance(settings.woocommerce, StoreWooCommerceSettings)
    assert isinstance(settings.webhook, StoreWebhookSettings)
    assert settings.woocommerce.sync_orders is True
    assert settings.woocommerce.store_url == ""
    assert settings.webhook.enable_hmac is True
    assert settings.webhook.max_retries == 3
    assert settings.webhook.timeout == 10.0


def test_store_plugin_settings_custom_values():
    data = {
        "shopify": {"shop_name": "myshop", "api_key": "k1", "api_secret": "s1"},
        "woocommerce": {
            "store_url": "https://example.com/shop",
            "consumer_key": "ck_123",
            "consumer_secret": "cs_456",
            "webhook_secret": "whsec_789",
            "sync_orders": False,
            "status_mapping": {"complete": "processing"},
        },
        "webhook": {
            "secret": "supersecret",
            "enable_hmac": True,
            "max_retries": 5,
            "backoff_factor": 2.0,
            "timeout": 15.0,
        },
    }
    settings = StorePluginSettings.model_validate(data)
    assert settings.shopify.shop_name == "myshop"
    assert settings.woocommerce.store_url == "https://example.com/shop"
    assert settings.woocommerce.sync_orders is False
    assert settings.woocommerce.status_mapping == {"complete": "processing"}
    assert settings.webhook.secret == "supersecret"
    assert settings.webhook.max_retries == 5
    assert settings.webhook.backoff_factor == 2.0


def test_store_plugin_settings_backward_compatibility():
    # Previous Bitcart format without woocommerce or webhook
    old_data = {"shopify": {"shop_name": "legacy_shop"}}
    settings = StorePluginSettings.model_validate(old_data)
    assert settings.shopify.shop_name == "legacy_shop"
    assert settings.woocommerce.store_url == ""
    assert settings.webhook.max_retries == 3


# ---------------------------------------------------------------------------
# Ecommerce Adapter Tests
# ---------------------------------------------------------------------------


def test_woocommerce_adapter_status_mapping():
    assert WooCommerceAdapter.map_status(InvoiceStatus.COMPLETE) == "completed"
    assert WooCommerceAdapter.map_status(InvoiceStatus.PAID) == "on-hold"
    assert WooCommerceAdapter.map_status(InvoiceStatus.CONFIRMED) == "processing"
    assert WooCommerceAdapter.map_status(InvoiceStatus.PENDING) == "pending"
    assert WooCommerceAdapter.map_status(InvoiceStatus.INVALID) == "failed"
    assert WooCommerceAdapter.map_status(InvoiceStatus.EXPIRED) == "cancelled"
    assert WooCommerceAdapter.map_status("unknown_status") == "on-hold"

    # Custom mapping override
    custom = {InvoiceStatus.COMPLETE: "processing"}
    assert WooCommerceAdapter.map_status(InvoiceStatus.COMPLETE, custom_mapping=custom) == "processing"


def test_woocommerce_adapter_build_order_note():
    note = WooCommerceAdapter.build_order_note(
        invoice_id="inv_123",
        status="complete",
        sent_amount="0.005",
        currency="BTC",
        tx_hashes=["tx_abc", "tx_def"],
    )
    assert "inv_123" in note
    assert "complete" in note
    assert "0.005 BTC" in note
    assert "tx_abc, tx_def" in note


def test_woocommerce_adapter_build_webhook_payload():
    payload = WooCommerceAdapter.build_webhook_payload(
        invoice_id="inv_456",
        status=InvoiceStatus.COMPLETE,
        store_id="store_1",
        price="100.00",
        currency="USD",
        paid_currency="BTC",
        sent_amount="0.0015",
        tx_hashes=["hash1"],
        metadata={"woocommerce_order_id": 9988},
    )
    assert payload["event"] == "invoice_status_changed"
    assert payload["invoice_id"] == "inv_456"
    assert payload["bitcart_status"] == InvoiceStatus.COMPLETE
    assert payload["woocommerce_status"] == "completed"
    assert payload["woocommerce_order_id"] == 9988
    assert payload["price"] == "100.00"
    assert payload["paid_currency"] == "BTC"
    assert "timestamp" in payload


def test_shopify_adapter_status_mapping():
    assert ShopifyAdapter.map_financial_status(InvoiceStatus.COMPLETE) == "paid"
    assert ShopifyAdapter.map_financial_status(InvoiceStatus.PAID) == "pending"
    assert ShopifyAdapter.map_financial_status(InvoiceStatus.INVALID) == "voided"
    assert ShopifyAdapter.map_financial_status(InvoiceStatus.EXPIRED) == "expired"


def test_shopify_adapter_build_transaction():
    tx = ShopifyAdapter.build_transaction_payload("inv_789", InvoiceStatus.COMPLETE, "50.00", "USD")
    assert tx["transaction"]["status"] == "success"
    assert tx["transaction"]["amount"] == "50.00"
    assert tx["transaction"]["gateway"] == "Bitcart"
    assert tx["transaction"]["authorization"] == "inv_789"


# ---------------------------------------------------------------------------
# HMAC Signature Verification Tests
# ---------------------------------------------------------------------------


def test_ipn_signature_computation_and_verification():
    secret = "whsec_test_secret_12345"
    payload = {"id": "inv_test", "status": "complete"}
    ts = int(time.time())

    # Raw signature
    raw_sig = compute_ipn_signature(payload, secret, timestamp=ts)
    assert isinstance(raw_sig, str) and len(raw_sig) == 64
    assert verify_ipn_signature(payload, raw_sig, secret, timestamp=ts)

    # Header format 't=...,v1=...'
    header_sig = f"t={ts},v1={raw_sig}"
    assert verify_ipn_signature(payload, header_sig, secret)

    # Tampered payload fails
    tampered = {"id": "inv_test", "status": "invalid"}
    assert not verify_ipn_signature(tampered, header_sig, secret)

    # Wrong secret fails
    assert not verify_ipn_signature(payload, header_sig, "wrong_secret")

    # Expired timestamp (beyond tolerance window) fails
    old_ts = ts - 600
    old_sig = compute_ipn_signature(payload, secret, timestamp=old_ts)
    old_header = f"t={old_ts},v1={old_sig}"
    assert not verify_ipn_signature(payload, old_header, secret, tolerance_seconds=300)


# ---------------------------------------------------------------------------
# IPNSender Delivery & Retry Tests
# ---------------------------------------------------------------------------


class MockPluginRegistry:
    async def apply_filters(self, name, data, *args):
        return data

    async def run_hook(self, name, *args):
        pass


@dataclass
class MockResponse:
    status: int
    _text: str = ""


class MockStore:
    def __init__(self, secret: str = "", max_retries: int = 3, timeout: float = 5.0):
        self.plugin_settings = StorePluginSettings(
            webhook=StoreWebhookSettings(secret=secret, max_retries=max_retries, timeout=timeout)
        )


@dataclass
class MockInvoice:
    id: str
    notification_url: str
    store: MockStore | None = None


@pytest.mark.asyncio
async def test_ipn_sender_success_first_attempt():
    registry = MockPluginRegistry()
    sender = IPNSender(registry, debounce_window_seconds=1.0)
    store = MockStore(secret="whsec_123", max_retries=3)
    invoice = MockInvoice("inv_001", "https://merchant.example.com/webhook", store=store)

    mock_resp = MockResponse(200, "OK")
    with patch("api.utils.common.send_request", AsyncMock(return_value=(mock_resp, "OK"))) as mock_send:
        result = await sender.send_invoice_ipn(invoice, "complete")

        assert result is not None
        assert result.success is True
        assert result.status_code == 200
        assert result.attempts == 1
        assert result.delivery_id != ""

        # Check headers passed to send_request
        mock_send.assert_called_once()
        call_kwargs = mock_send.call_args[1]
        headers = call_kwargs["headers"]
        assert "X-Bitcart-Event-Id" in headers
        assert "X-Bitcart-Delivery-Id" in headers
        assert "X-Bitcart-Timestamp" in headers
        assert "X-Bitcart-Signature-256" in headers
        assert "X-Bitcart-HMAC-SHA256" in headers


@pytest.mark.asyncio
async def test_ipn_sender_retry_on_server_error_then_success():
    registry = MockPluginRegistry()
    sender = IPNSender(registry, default_initial_delay=0.01, default_backoff_factor=1.0)
    invoice = MockInvoice("inv_002", "https://merchant.example.com/webhook")

    resp_500 = MockResponse(500, "Internal Server Error")
    resp_200 = MockResponse(200, "Success")

    with patch(
        "api.utils.common.send_request",
        AsyncMock(side_effect=[(resp_500, "Internal Server Error"), (resp_200, "Success")]),
    ) as mock_send:
        result = await sender.send_invoice_ipn(invoice, "complete")

        assert result is not None
        assert result.success is True
        assert result.status_code == 200
        assert result.attempts == 2
        assert mock_send.call_count == 2


@pytest.mark.asyncio
async def test_ipn_sender_fast_fail_on_4xx_client_error():
    registry = MockPluginRegistry()
    sender = IPNSender(registry, default_initial_delay=0.01)
    invoice = MockInvoice("inv_003", "https://merchant.example.com/webhook")

    resp_404 = MockResponse(404, "Not Found")
    with patch("api.utils.common.send_request", AsyncMock(return_value=(resp_404, "Not Found"))) as mock_send:
        result = await sender.send_invoice_ipn(invoice, "complete")

        assert result is not None
        assert result.success is False
        assert result.status_code == 404
        assert result.attempts == 1  # Should not retry 404
        assert mock_send.call_count == 1
        assert "client error, non-retryable" in (result.error or "")


@pytest.mark.asyncio
async def test_ipn_sender_debounce_duplicate_notification():
    registry = MockPluginRegistry()
    sender = IPNSender(registry, debounce_window_seconds=10.0)
    invoice = MockInvoice("inv_004", "https://merchant.example.com/webhook")

    mock_resp = MockResponse(200, "OK")
    with patch("api.utils.common.send_request", AsyncMock(return_value=(mock_resp, "OK"))) as mock_send:
        # First send: succeeds
        res1 = await sender.send_invoice_ipn(invoice, "paid")
        assert res1.success is True
        assert res1.attempts == 1
        assert mock_send.call_count == 1

        # Second send within debounce window: debounced
        res2 = await sender.send_invoice_ipn(invoice, "paid")
        assert res2.success is True
        assert res2.attempts == 0
        assert "Debounced" in (res2.error or "")
        assert mock_send.call_count == 1  # No additional HTTP request made!
