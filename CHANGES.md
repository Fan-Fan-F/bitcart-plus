# CHANGES — bitcart-plus 改进计划与变更记录

本项目基于 [bitcart/bitcart](https://github.com/bitcart/bitcart)（自托管加密货币支付处理器）进行二次开发（路线 A），重点增强支付状态回调可靠性、补全主流电商平台对接插件与模型，并实现支付回调防伪装与幂等防抖机制。

---

## 改进计划与变更详情

### 1. 支付状态回调（IPN / Webhook）高可用重试与交付引擎 (IPN Delivery Engine with Exponential Backoff)
- **涉及文件**：`api/services/ipn_sender.py`, `api/schemas/stores.py`
- **原项目问题**：原 `IPNSender.send_invoice_ipn` 为单次无状态 HTTP 请求（`send_request("POST", ...)`），无重试机制，无失败容错。若商户服务短暂网络波动、重启或限流（429/5xx/超时），回调立即丢弃且仅打一条 info 日志，导致商户电商系统无法收到支付成功通知，引发订单卡单、漏单等严重问题；且原逻辑未校验 5xx 状态码，即使商户端返回 500 也会误判为 success 记入日志。
- **改进做法**：
  1. 重构 `IPNSender` 为支持指数退避重试（Exponential Backoff）的异步交付引擎；
  2. 支持商户级或全局配置最大重试次数（`max_retries`，默认 3 次）、退避因子（`backoff_factor`，默认 1.5）、初始延时（`default_initial_delay`）与单次超时限制（`timeout`）；
  3. 智能区分错误类型：对 429 限流和 5xx 服务器瞬时故障自动指数退避重试；对不可重试的 4xx 客户端错误（如 400 Bad Request、404 Not Found）快速失败，避免无谓重试浪费资源；
  4. 引入 `IPNDeliveryResult` 数据结构，详细记录每次交付序列的尝试次数（`attempts`）、最终 HTTP 状态码（`status_code`）、耗时（`elapsed_ms`）、全局交付 ID（`delivery_id`）与错误信息。

### 2. Webhook 安全签名防伪装与防重放机制 (HMAC-SHA256 Signature & Anti-Replay Verification)
- **涉及文件**：`api/services/ipn_sender.py`, `api/schemas/stores.py`, `api/ext/ecommerce.py`
- **原项目问题**：原项目 IPN 仅发送裸 JSON 报文（`{"id": ..., "status": ...}`），没有任何数字签名或身份验证机制。任何知道商户通知 URL 的攻击者均可伪造支付完成回调，绕过支付直接骗取商品；同时缺少时间戳，无法防御重放攻击。
- **改进做法**：
  1. 在 `StoreWebhookSettings` 中增加 `secret`（密钥）与 `enable_hmac` 配置项；
  2. 在 `IPNSender` 发送回调时计算基于密钥的 HMAC-SHA256 签名，并通过标准请求头 `X-Bitcart-Signature-256`（格式 `t=<timestamp>,v1=<signature>`）与 `X-Bitcart-HMAC-SHA256` 发送；
  3. 增加请求时间戳 `X-Bitcart-Timestamp`（Unix 时间戳）与交付追踪 ID `X-Bitcart-Delivery-Id`；
  4. 在 `api/ext/ecommerce.py` 中提供开箱即用的商户端验签工具函数 `compute_ipn_signature` 与 `verify_ipn_signature`，基于 `hmac.compare_digest` 防御时序攻击，并支持配置容差时间窗口（`tolerance_seconds`，默认 300s）防御重放攻击。

### 3. 主流电商平台对接插件与状态适配器 (WooCommerce & Shopify E-Commerce Adapters)
- **涉及文件**：`api/schemas/stores.py`, `api/ext/ecommerce.py`
- **原项目问题**：原项目仅在 `StorePluginSettings` 中留有一个空的 `StoreShopifySettings` 存根，完全缺失全球使用最广泛的开源独立站系统 WooCommerce 的配置模型，且缺少统一的电商平台状态映射工具与适配器。
- **改进做法**：
  1. 在 `api/schemas/stores.py` 的 `StorePluginSettings` 中新增 `StoreWooCommerceSettings` 配置模型（包含 `store_url`、`consumer_key`、`consumer_secret`、`webhook_secret`、`sync_orders`、`status_mapping` 等字段）；
  2. 新增 `StoreWebhookSettings` 模型（包含 `secret`、`enable_hmac`、`max_retries`、`backoff_factor`、`timeout` 等独立回调策略字段）；
  3. 在 `api/ext/ecommerce.py` 中实现统一的电商适配器：
     - `WooCommerceAdapter`：提供 Bitcart 账单状态（`pending`、`paid`、`confirmed`、`complete`、`invalid`、`expired`、`refunded`）到 WooCommerce 订单状态（`pending`、`on-hold`、`processing`、`completed`、`failed`、`cancelled`、`refunded`）的标准映射与自定义映射覆盖机制；
     - 提供 `build_order_note` 生成商户友好的带交易哈希与金额的后台订单备注；
     - 提供 `build_webhook_payload` 构建符合电商网关标准的完整 Webhook 事件报文；
     - `ShopifyAdapter`：提供 `map_financial_status` 与 `build_transaction_payload`，支持 Shopify 格式交易回执与状态同步。

### 4. 支付回调幂等性防抖与重复通知抑制 (Payment Notification Idempotency & Debouncing)
- **涉及文件**：`api/services/ipn_sender.py`
- **原项目问题**：区块链网络在确认数推进（0-conf -> 1-conf -> N-conf）或连续产生新块时，支付处理模块可能会因交易哈希或确认数更新而多次触发通知。若无幂等防抖，商户服务器会短时间内收到大量重复状态的 IPN 请求，容易导致下游业务逻辑并发混乱或库存重复扣减。
- **改进做法**：
  1. 在 `IPNSender` 中引入基于内存时间戳的幂等缓存机制，以 `(target_url, object_id, status)` 作为状态指纹；
  2. 在设定的防抖抑制窗口（`debounce_window_seconds`，默认 15s）内，对相同目标地址和状态的重复通知进行智能抑制，防止短时间连续重推；
  3. 在 Webhook Header 中注入全局事件唯一标识 `X-Bitcart-Event-Id`，方便商户接收端实现自身业务层幂等消费。

---

## 自测记录

### 测试环境与工具
- 运行环境：Python 3.12.3 (Linux x86_64)
- 测试框架：pytest 9.2.0, pytest-mock 3.17.0, pytest-asyncio 1.5.0
- 代码规范：ruff 0.17.5（格式检查与 import 排序全部通过）

### 外部依赖与环境限制说明
原项目全量功能测试包含 functional 目录与 views 目录，需要本地启动 Fulcrum/Electrum 节点守护进程、PostgreSQL 数据库、Redis 以及 Rust 模板渲染器等一系列微服务。在纯净开发容器环境下未配置这些外部基础服务，因此按本次任务指令及防超时纪律，聚焦核心改进模块的单元测试，不跑依赖未启动外部微服务的全量测试。

### 执行测试命令与实际输出
1. **代码风格与规范检查**：
   ```bash
   ruff check api/schemas/stores.py api/services/ipn_sender.py api/ext/ecommerce.py tests/test_ecommerce_and_ipn.py
   ```
   **结果**：`All checks passed!`（0 errors, 0 warnings）

2. **核心改进点与相关模块自测**：
   ```bash
   pytest -o addopts="" -o filterwarnings="default" -o asyncio_default_fixture_loop_scope="function" --noconftest tests/test_ecommerce_and_ipn.py tests/test_ext/test_moneyformat.py tests/test_ext/test_fxrate.py
   ```
   **结果**：36 项全部通过（其中本次新增 `tests/test_ecommerce_and_ipn.py` 占 13 项，其余 23 项为原项目自带的 ext 模块测试）：
   ```text
   ============================= test session starts ==============================
   platform linux -- Python 3.12.3, pytest-9.2.0, pluggy-1.7.0
   rootdir: . (bitcart-plus)
   configfile: pyproject.toml
   plugins: mock-3.17.0, asyncio-1.5.0, anyio-4.16.0
   asyncio: mode=Mode.STRICT, debug=False, asyncio_default_fixture_loop_scope=function, asyncio_default_test_loop_scope=function
   collected 36 items

   tests/test_ecommerce_and_ipn.py .............                            [ 36%]
   tests/test_ext/test_moneyformat.py .....................                 [ 94%]
   tests/test_ext/test_fxrate.py ..                                         [100%]

   ============================== 36 passed in 2.15s ==============================
   ```
   共 36 项测试，36 全部通过（通过率 100%）。测试覆盖了 WooCommerce/Shopify 适配器、HMAC-SHA256 防篡改与防重放验签、IPN 指数退避重试、4xx 客户端错误快速失败、幂等防抖机制及配置序列化向后兼容性。
