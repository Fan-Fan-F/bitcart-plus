from __future__ import annotations

import asyncio
import time
import traceback
import uuid
from dataclasses import dataclass
from typing import Any

from api import utils
from api.ext.ecommerce import compute_ipn_signature
from api.logging import get_logger
from api.services.plugin_registry import PluginRegistry

logger = get_logger(__name__)


@dataclass
class IPNDeliveryResult:
    """Detailed result of an IPN delivery attempt sequence."""

    success: bool
    status_code: int | None = None
    attempts: int = 1
    error: str | None = None
    delivery_id: str = ""
    elapsed_ms: float = 0.0


class IPNSender:
    """Robust IPN sender with exponential backoff retries, HMAC signatures, and deduplication."""

    def __init__(
        self,
        plugin_registry: PluginRegistry,
        *,
        default_max_retries: int = 3,
        default_backoff_factor: float = 1.5,
        default_initial_delay: float = 0.5,
        default_timeout: float = 10.0,
        debounce_window_seconds: float = 15.0,
    ) -> None:
        self.plugin_registry = plugin_registry
        self.default_max_retries = default_max_retries
        self.default_backoff_factor = default_backoff_factor
        self.default_initial_delay = default_initial_delay
        self.default_timeout = default_timeout
        self.debounce_window_seconds = debounce_window_seconds
        # In-memory deduplication cache: (url, obj_id, status) -> timestamp
        self._sent_cache: dict[tuple[str, str, str], float] = {}

    def _cleanup_cache(self, now: float) -> None:
        cutoff = now - (self.debounce_window_seconds * 4)
        expired = [k for k, ts in self._sent_cache.items() if ts < cutoff]
        for k in expired:
            self._sent_cache.pop(k, None)

    def _should_debounce(self, url: str, obj_id: str, status: str, now: float) -> bool:
        self._cleanup_cache(now)
        key = (url, obj_id, status)
        last_sent = self._sent_cache.get(key)
        return last_sent is not None and (now - last_sent) < self.debounce_window_seconds

    def _record_sent(self, url: str, obj_id: str, status: str, now: float) -> None:
        self._sent_cache[(url, obj_id, status)] = now

    def _extract_store_settings(self, obj: Any) -> tuple[str | None, int, float, float]:
        """Extract webhook secret, max_retries, backoff_factor, and timeout from object store if present."""
        secret: str | None = None
        max_retries = self.default_max_retries
        backoff_factor = self.default_backoff_factor
        timeout = self.default_timeout

        store = getattr(obj, "store", None)
        if store is not None:
            plugin_settings = getattr(store, "plugin_settings", None)
            if plugin_settings is not None:
                webhook_cfg = getattr(plugin_settings, "webhook", None)
                if webhook_cfg is not None:
                    if getattr(webhook_cfg, "secret", ""):
                        secret = str(webhook_cfg.secret)
                    max_retries = int(getattr(webhook_cfg, "max_retries", max_retries))
                    backoff_factor = float(getattr(webhook_cfg, "backoff_factor", backoff_factor))
                    timeout = float(getattr(webhook_cfg, "timeout", timeout))

                wc_cfg = getattr(plugin_settings, "woocommerce", None)
                if wc_cfg is not None and not secret and getattr(wc_cfg, "webhook_secret", ""):
                    secret = str(wc_cfg.webhook_secret)

        return secret, max_retries, backoff_factor, timeout

    async def send_invoice_ipn(self, obj: Any, status: str) -> IPNDeliveryResult | None:
        """Send IPN notification to the configured notification_url with retries and signature."""
        if not getattr(obj, "notification_url", None):
            return None

        url = obj.notification_url
        obj_id = str(getattr(obj, "id", ""))
        now = time.time()

        delivery_id = str(uuid.uuid4())
        event_id = f"evt_{obj_id}_{int(now)}"

        # Check idempotency debouncing for rapid duplicate events
        if self._should_debounce(url, obj_id, status, now):
            logger.info(f"Skipping duplicate IPN for object {obj_id} (status: {status}) to {url} (debounced)")
            return IPNDeliveryResult(
                success=True,
                status_code=200,
                attempts=0,
                error="Debounced duplicate notification",
                delivery_id=delivery_id,
                elapsed_ms=0.0,
            )

        data = await self.plugin_registry.apply_filters("ipn_data", {"id": obj.id, "status": status}, obj, status)
        secret, max_retries, backoff_factor, timeout = self._extract_store_settings(obj)

        timestamp_str = str(int(now))
        headers: dict[str, str] = {
            "Content-Type": "application/json",
            "X-Bitcart-Event-Id": event_id,
            "X-Bitcart-Delivery-Id": delivery_id,
            "X-Bitcart-Timestamp": timestamp_str,
            "User-Agent": "Bitcart-IPN-Sender/2.0",
        }

        if secret:
            sig = compute_ipn_signature(data, secret, timestamp=timestamp_str)
            headers["X-Bitcart-Signature-256"] = f"t={timestamp_str},v1={sig}"
            headers["X-Bitcart-HMAC-SHA256"] = sig

        base_log_message = f"Sending IPN with data {data} to {url} [Delivery: {delivery_id}]"

        try:
            await self.plugin_registry.run_hook("send_ipn", obj, status, data)
        except Exception:
            logger.warning(f"Plugin hook 'send_ipn' failed: {traceback.format_exc()}")

        last_error: str | None = None
        last_status_code: int | None = None
        total_start = time.monotonic()

        for attempt in range(1, max_retries + 1):
            attempt_start = time.monotonic()
            try:
                resp, text = await utils.common.send_request(
                    "POST",
                    url,
                    headers=headers,
                    json=data,
                    return_json=False,
                )
                last_status_code = resp.status
                attempt_elapsed = (time.monotonic() - attempt_start) * 1000

                # 2xx Success
                if 200 <= resp.status < 300:
                    total_elapsed = (time.monotonic() - total_start) * 1000
                    logger.info(
                        f"{base_log_message}: success on attempt {attempt}/{max_retries} "
                        f"(status {resp.status}, {attempt_elapsed:.1f}ms)"
                    )
                    self._record_sent(url, obj_id, status, time.time())
                    return IPNDeliveryResult(
                        success=True,
                        status_code=resp.status,
                        attempts=attempt,
                        delivery_id=delivery_id,
                        elapsed_ms=total_elapsed,
                    )

                # Non-retryable 4xx client errors (except 429 rate limit)
                if 400 <= resp.status < 500 and resp.status != 429:
                    total_elapsed = (time.monotonic() - total_start) * 1000
                    last_error = f"HTTP {resp.status} (client error, non-retryable): {text[:150]}"
                    logger.warning(f"{base_log_message}: attempt {attempt}/{max_retries} failed with {last_error}")
                    return IPNDeliveryResult(
                        success=False,
                        status_code=resp.status,
                        attempts=attempt,
                        error=last_error,
                        delivery_id=delivery_id,
                        elapsed_ms=total_elapsed,
                    )

                # 429 or 5xx server errors: retryable
                last_error = f"HTTP {resp.status}: {text[:150]}"
                logger.warning(f"{base_log_message}: attempt {attempt}/{max_retries} received {last_error}")

            except Exception as exc:
                attempt_elapsed = (time.monotonic() - attempt_start) * 1000
                last_error = f"{type(exc).__name__}: {str(exc)}"
                logger.warning(f"{base_log_message}: attempt {attempt}/{max_retries} raised exception: {last_error}")

            if attempt < max_retries:
                delay = self.default_initial_delay * (backoff_factor ** (attempt - 1))
                await asyncio.sleep(delay)

        total_elapsed = (time.monotonic() - total_start) * 1000
        logger.error(f"{base_log_message}: all {max_retries} attempts failed. Last error: {last_error}")
        return IPNDeliveryResult(
            success=False,
            status_code=last_status_code,
            attempts=max_retries,
            error=last_error,
            delivery_id=delivery_id,
            elapsed_ms=total_elapsed,
        )
