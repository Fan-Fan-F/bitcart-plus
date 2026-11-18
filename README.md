# Bitcart Plus (bitcart-plus)

> 高可用、带电商扩展与可靠 Webhook 回调引擎的自托管加密货币支付处理器。  
> High-availability, self-hosted cryptocurrency payment processor with robust webhook callback engine and e-commerce integrations.

---

## 简体中文

### 项目介绍
**Bitcart Plus** 是基于知名自托管加密货币支付网关 Bitcart 进行二次开发（A 路线）的增强版本。针对跨境电商独立站与多商户场景的实际需求，本项目重点强化了支付状态回调（IPN / Webhook）的交付可靠性（支持指数退避重试与失败快速拦截）、数字防伪装与防重放机制（HMAC-SHA256 签名与时间戳校验）、商户端幂等防抖，并补齐了 WooCommerce 与 Shopify 等主流电商平台的标准化对接插件与状态映射适配器。

### 功能与改进点
本项目在原项目基础上实现了 4 项实质性改进（详见 [CHANGES.md](CHANGES.md)）：

1. **支付状态回调（IPN / Webhook）高可用重试与交付引擎** (`api/services/ipn_sender.py`)：
   - 彻底解决原项目单次 HTTP 调用易因网络抖动、商户端重启导致漏单卡单的问题；
   - 支持商户级与全局最大重试次数（默认 3 次）、退避因子（默认 1.5）与指数退避（Exponential Backoff）；
   - 智能识别 429 限流与 5xx 服务端错误自动重试；对不可重试的 4xx 客户端错误快速失败；
   - 引入 `IPNDeliveryResult`，详尽记录每次回调的全局交付追踪 ID（`delivery_id`）、尝试次数、HTTP 状态码及耗时。
2. **Webhook 安全签名防伪装与防重放机制** (`api/services/ipn_sender.py`, `api/ext/ecommerce.py`)：
   - 支持配置 Webhook 密钥（`secret`）与 HMAC-SHA256 签名计算；
   - 回调请求头中注入 `X-Bitcart-Signature-256`（格式 `t=<timestamp>,v1=<signature>`）、`X-Bitcart-Timestamp` 与 `X-Bitcart-Delivery-Id`；
   - 提供开箱即用的商户端验签工具 `verify_ipn_signature`，通过时序安全比较（`hmac.compare_digest`）与容差时间窗口防御仿冒请求与重放攻击。
3. **主流电商平台对接插件与状态适配器** (`api/schemas/stores.py`, `api/ext/ecommerce.py`)：
   - 在 `StorePluginSettings` 中新增 `StoreWooCommerceSettings` 与 `StoreWebhookSettings` 结构化配置模型；
   - 提供 `WooCommerceAdapter`：支持 Bitcart 账单状态（`pending`、`paid`、`confirmed`、`complete`、`invalid`、`expired` 等）到 WooCommerce 订单状态的标准双向映射、订单备注格式化与标准化 Webhook 报文生成；
   - 提供 `ShopifyAdapter`：支持 Shopify 财务状态映射与交易报文构造。
4. **支付回调幂等性防抖与重复通知抑制** (`api/services/ipn_sender.py`)：
   - 引入内存状态指纹 `(target_url, object_id, status)` 缓存；
   - 在设定的防抖窗口内（默认 15s）抑制重复状态的高频重推，避免下游商户系统并发错乱或重复扣减库存；
   - 请求头注入全局事件唯一标识 `X-Bitcart-Event-Id`，方便商户端实现幂等消费。

### 快速开始

#### 1. 环境要求
- Python >= 3.12
- 依赖项管理器：`pip` 或 `uv`
- 外部基础服务（完整运行时）：PostgreSQL、Redis、对应币种全节点/Electrum 守护进程

#### 2. 安装与配置
克隆仓库并安装 Python 依赖：
```bash
git clone https://github.com/Fan-Fan-F/bitcart-plus.git
cd bitcart-plus

# 创建虚拟环境
python3 -m venv .venv
source .venv/bin/activate

# 安装核心依赖
pip install -e .
# 或使用 pyproject.toml 声明依赖
pip install "fastapi>=0.115.0" "pydantic>=2.10.3" "aiohttp<4.0" "sqlalchemy>=2.0.36" "dishka>=1.4.2"
```

配置环境变量（或创建 `.env` 文件）：
```bash
BITCART_ADMIN_USER=admin@example.com
BITCART_ADMIN_PASS=changeme
BITCART_DB_URL=postgresql+asyncpg://postgres:postgres@localhost:5432/bitcart
BITCART_REDIS_URL=redis://localhost:6379/0
```

#### 3. 运行服务与自测
```bash
# 启动 API 服务
uvicorn main:app --host 0.0.0.0 --port 8000

# 运行代码规范检查
ruff check api/schemas/stores.py api/services/ipn_sender.py api/ext/ecommerce.py tests/test_ecommerce_and_ipn.py

# 运行电商与回调增强模块自测
pytest -o addopts="" -o filterwarnings="default" -o asyncio_default_fixture_loop_scope="function" --noconftest tests/test_ecommerce_and_ipn.py
```

### 来源与致谢
本项目基于 [bitcart](https://github.com/bitcart/bitcart) 二次开发，原项目由 bitcart 以 MIT 许可证开源。

### 许可证
本项目沿用原项目的开源许可证，采用 MIT 许可证。完整授权声明请参阅根目录 [LICENSE](LICENSE) 文件。

---

## English

### Overview
**Bitcart Plus** is an enhanced fork (Route A secondary development) of [Bitcart](https://github.com/bitcart/bitcart), the popular self-hosted cryptocurrency payment processor. Designed to meet the stringent reliability requirements of e-commerce independent stores and multi-merchant platforms, this project enhances payment status callback (IPN / Webhook) reliability with exponential backoff retries, cryptographic anti-tampering (HMAC-SHA256 signatures and timestamp verification), idempotency debouncing, and plug-and-play adapters for mainstream e-commerce platforms such as WooCommerce and Shopify.

### Key Features & Improvements
Based on the upstream project, 4 substantive enhancements have been implemented (see [CHANGES.md](CHANGES.md) for details):

1. **High-Availability IPN Delivery Engine with Exponential Backoff** (`api/services/ipn_sender.py`):
   - Solves missed or lost notifications caused by transient network glitches or merchant server restarts;
   - Configurable retry count (`max_retries`, default 3), backoff multiplier (`backoff_factor`, default 1.5), and request timeout;
   - Intelligent status code handling: retries on HTTP 429 rate limits and 5xx server errors; fast-fails on non-retryable 4xx client errors;
   - Returns structured `IPNDeliveryResult` logging delivery attempts, HTTP status codes, latency in ms, and unique delivery IDs.
2. **HMAC-SHA256 Webhook Security & Anti-Replay Verification** (`api/services/ipn_sender.py`, `api/ext/ecommerce.py`):
   - Supports merchant webhook secrets and HMAC-SHA256 signature generation;
   - Automatically injects `X-Bitcart-Signature-256` (format: `t=<timestamp>,v1=<signature>`), `X-Bitcart-Timestamp`, and `X-Bitcart-Delivery-Id` headers;
   - Provides ready-to-use merchant verification utilities `verify_ipn_signature` with timing-attack prevention (`hmac.compare_digest`) and configurable timestamp tolerance windows.
3. **E-Commerce Adapters for WooCommerce and Shopify** (`api/schemas/stores.py`, `api/ext/ecommerce.py`):
   - Adds `StoreWooCommerceSettings` and `StoreWebhookSettings` schemas to `StorePluginSettings`;
   - Implements `WooCommerceAdapter` with complete bidirectional status mappings (`pending`, `paid`, `confirmed`, `complete`, `invalid`, `expired`), order note generation, and standardized webhook payloads;
   - Implements `ShopifyAdapter` for financial status translation and checkout transaction responses.
4. **Notification Idempotency & Debouncing** (`api/services/ipn_sender.py`):
   - Implements state fingerprint caching `(target_url, object_id, status)`;
   - Suppresses duplicate rapid status notifications within a configurable debounce window (default 15s);
   - Injects global unique `X-Bitcart-Event-Id` for downstream idempotency management.

### Quick Start

#### Requirements
- Python >= 3.12
- Package manager: `pip` or `uv`
- External services (for full runtime): PostgreSQL, Redis, coin daemons / Electrum nodes

#### Installation
```bash
git clone https://github.com/Fan-Fan-F/bitcart-plus.git
cd bitcart-plus

python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

#### Running & Testing
```bash
# Start API server
uvicorn main:app --host 0.0.0.0 --port 8000

# Run linter
ruff check api/schemas/stores.py api/services/ipn_sender.py api/ext/ecommerce.py tests/test_ecommerce_and_ipn.py

# Run unit tests
pytest -o addopts="" -o filterwarnings="default" -o asyncio_default_fixture_loop_scope="function" --noconftest tests/test_ecommerce_and_ipn.py
```

### Source & Acknowledgments
This project is a secondary development based on [bitcart](https://github.com/bitcart/bitcart), originally developed by [bitcart](https://github.com/bitcart) and open-sourced under the MIT License.

### License
This project inherits the original open source license and is released under the terms of the MIT License. See [LICENSE](LICENSE) for details.
