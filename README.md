<div align="center">

<img src="https://img.shields.io/badge/Python-3.10%2B-3776AB?style=for-the-badge&logo=python&logoColor=white" />
<img src="https://img.shields.io/badge/FastAPI-0.115%2B-009688?style=for-the-badge&logo=fastapi&logoColor=white" />
<img src="https://img.shields.io/badge/PostgreSQL-asyncpg-336791?style=for-the-badge&logo=postgresql&logoColor=white" />
<img src="https://img.shields.io/badge/Redis-Distributed%20Locks-DC382D?style=for-the-badge&logo=redis&logoColor=white" />
<img src="https://img.shields.io/badge/Kafka-AIOKafka-231F20?style=for-the-badge&logo=apachekafka&logoColor=white" />
<img src="https://img.shields.io/badge/structlog-JSON%20Logging-4B8BBE?style=for-the-badge" />
<img src="https://img.shields.io/badge/RFC%207807-Problem%20Details-E34F26?style=for-the-badge" />

<br /><br />

# ⚡ Payment Chassis

### Enterprise-Grade Asynchronous Microservice Boilerplate

*A production-ready FastAPI chassis built on Clean Architecture, Domain-Driven Design, and distributed systems patterns — ready to clone and extend for any payments or financial microservice.*

<br />

</div>

---

## 🎯 What This Is

**Payment Chassis** is not a tutorial app. It is an **engineering-grade infrastructure template** that solves the hardest production concerns *before* you write a single line of business logic.

Clone it. Drop in your domain. Ship in hours, not weeks.

---

## 🏗️ Architecture at a Glance

```
 [ HTTP Request ]
        │
        ▼  ① X-Correlation-ID injected / generated (distributed trace)
 [ Correlation ID Middleware ]
        │
        ▼  ② Redis cache-check + atomic distributed lock (NX + Lua)
 [ Idempotency Middleware ] ──(hit / concurrent)──► Cached 200 / 409 Conflict
        │  (cache miss, lock acquired)
        ▼
 [ FastAPI Router  /api/v1/payments ]  ← Pydantic validation only
        │
        ▼
 [ Application Use Case ]  ← Orchestration only, zero I/O knowledge
        │
        ├──────────────────────────────────┐
        ▼                                  ▼
 [ Payment Gateway Client ]        [ Domain Entity Invariants ]
   tenacity: 3 retries +                   │
   exponential backoff +                   ▼
   primary → fallback         [ SQLAlchemy Unit of Work ]
        │                              │
        └──────────────────────────────┤
                                       ├─► Commit PaymentRecord  ─┐
                                       └─► Commit OutboxEvent    ─┘ (same txn, atomic)
                                                   │
                                                   ▼
 [ Background Outbox Relay ] ─(SKIP LOCKED poll, 2s)─► [ AIOKafka Producer ]
                                                              │
                                                              ▼
                                                   Downstream Services
```

---

## 🛡️ The Six Production Problems This Solves

| Problem | Without This Chassis | With This Chassis |
|---|---|---|
| **Dual-write** | DB commits, Kafka event never fires, data out of sync forever | Transactional Outbox — event and data in one atomic DB transaction |
| **Double charge** | Client retries = customer charged twice | Idempotency Middleware — Redis lock + response cache per key |
| **No traceability** | Bug appears, no way to trace across services | `X-Correlation-ID` bound to every log line across all layers |
| **ORM in routes** | Business logic mixed with HTTP, untestable | Domain layer — zero framework imports, pure Python dataclasses |
| **Non-standard errors** | Every endpoint returns a different error shape | RFC 7807 `application/problem+json` — uniform across all errors |
| **Fragile gateways** | One network blip = 500 with no retry | Tenacity: 3 retries per acquirer + primary/fallback circuit |

---

## 📐 Layer Architecture (Clean Architecture)

```
domain/           ← INNERMOST — zero external dependencies
  entities/       ← Pure Python dataclasses, business invariants
  exceptions/     ← Domain exception taxonomy
  interfaces/     ← Abstract repository ports (ABCs)

application/      ← Orchestration only
  use_cases/      ← One file per business workflow
  uow.py          ← Unit of Work — transaction boundary

infrastructure/   ← ALL external I/O (implements domain ports)
  database/       ← SQLAlchemy models, session, repo adapters
  cache/          ← Redis client (async)
  messaging/      ← Kafka producer (AIOKafka)
  workers/        ← Outbox relay background task
  external/       ← Payment gateway HTTP client (httpx)

presentation/     ← HTTP surface only
  api/            ← FastAPI versioned routers
  middleware/     ← Correlation ID, Idempotency, RFC 7807 handler

core/             ← Cross-cutting (config, logging)
alembic/          ← Schema migrations
```

**Dependency Rule:** Domain ← Application ← Infrastructure. The domain never imports from any outer layer. Ever.

---

## 🚀 Key Features

### ⚛️ Transactional Outbox Pattern
Eliminates the dual-write problem permanently. Payment record and domain event are written in the **same database transaction**. A background worker polls `FOR UPDATE SKIP LOCKED` and publishes to Kafka — supporting horizontal scaling with zero duplication.

### 🔒 Distributed Idempotency
Every `POST /payments` is guarded by:
1. Redis `GET` — instant cache hit returns cached response
2. Redis `SET NX EX 15` — atomic lock prevents concurrent duplicates
3. Lua CAS script — lock release is atomic, no race condition possible

### 🌐 Correlation ID Propagation
`CorrelationIdMiddleware` reads or generates `X-Correlation-ID` on every request and binds it into:
- `request.state` (available to handlers)
- `structlog` ContextVar (auto-injected into every log line)
- Response headers (echoed back to clients)

Query your log aggregator for any correlation ID and see every log line — HTTP handler, Redis lock, gateway call, Kafka publish — from that single request.

### 📋 RFC 7807 Problem Details
**Every error** — validation failure, not found, gateway outage, unhandled exception — returns:
```json
{
  "type": "https://api.example.com/errors/payment-gateway-error",
  "title": "Payment Gateway Unavailable",
  "status": 502,
  "detail": "All payment acquirers are currently unavailable.",
  "instance": "/api/v1/payments",
  "correlation_id": "a3f2-..."
}
```
`Content-Type: application/problem+json` — RFC compliant.

### 📊 Structured JSON Logging (structlog)
```json
{
  "event": "gateway_charge_attempt",
  "acquirer": "primary",
  "correlation_id": "a3f2-...",
  "level": "info",
  "timestamp": "2025-06-09T14:30:00.123456Z",
  "filename": "gateway_client.py",
  "func_name": "charge_with_fallback",
  "lineno": 52
}
```
Every log line is machine-parseable and carries callsite + request context. Ships with `LOG_FORMAT=console` for local dev.

### 🔄 Resilient Gateway Client
```
Primary Acquirer
  ├── Attempt 1 (immediate)
  ├── Attempt 2 (wait 2s)
  └── Attempt 3 (wait 4s) ── FAIL
Fallback Acquirer
  ├── Attempt 1 (immediate)
  ├── Attempt 2 (wait 2s)
  └── Attempt 3 (wait 4s) ── FAIL → PaymentGatewayError → 502
```

---

## 📦 Tech Stack

| Layer | Technology | Purpose |
|---|---|---|
| Web Framework | FastAPI 0.115+ | Async HTTP, OpenAPI docs |
| ORM | SQLAlchemy 2.0 (async) | Database persistence |
| Driver | asyncpg | Fastest async PostgreSQL driver |
| Migrations | Alembic | Schema versioning |
| Config | pydantic-settings | Type-validated env vars |
| Logging | structlog | Structured JSON telemetry |
| Cache / Lock | redis-py (async) | Idempotency + distributed locks |
| Messaging | AIOKafka | Async event publishing |
| HTTP Client | httpx | Async gateway calls |
| Retries | tenacity | Exponential backoff + fallback |
| Python | 3.10+ | `match`, `|` union types, async |

---

## ⚡ Quickstart

### Prerequisites
- Python 3.10+
- PostgreSQL 14+ running
- Redis 6+ running
- Kafka 2.8+ running (optional — app gracefully degrades)
- `uv` package manager

### 1. Clone & Install
```bash
git clone https://github.com/vaibhav2616/enterprise-fastapi-boilerplate.git
cd enterprise-fastapi-boilerplate
uv sync
```

### 2. Configure Environment
```bash
cp .env.example .env
# Edit .env with your connection strings
```

```env
DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5432/payments_db
REDIS_URL=redis://localhost:6379/0
KAFKA_BOOTSTRAP_SERVERS=localhost:9092
LOG_LEVEL=INFO
LOG_FORMAT=console   # use 'json' in production
```

### 3. Run Migrations
```bash
uv run alembic upgrade head
```

### 4. Start the Server
```bash
uv run fastapi dev main.py
```

API docs available at → **http://localhost:8000/docs**

---

## 🔌 API Reference

### `POST /api/v1/payments`

Create and process a payment transaction.

**Request**
```http
POST /api/v1/payments
Content-Type: application/json
X-Idempotency-Key: <unique-client-uuid>
X-Correlation-ID: <optional-trace-id>

{
  "transaction_id": "txn_20250609_001",
  "amount": 2500.00,
  "currency": "INR"
}
```

**Response `201 Created`**
```json
{
  "transaction_id": "txn_20250609_001",
  "amount": 2500.00,
  "currency": "INR",
  "status": "SUCCESS",
  "created_at": "2025-06-09T14:30:00.123456Z"
}
```

**Error Responses (RFC 7807)**

| Status | `type` slug | Cause |
|---|---|---|
| `400` | `domain-validation-error` | Business rule violation |
| `409` | `duplicate-transaction` | Duplicate transaction ID |
| `422` | `request-validation-error` | Invalid request body |
| `502` | `payment-gateway-error` | All acquirers unavailable |
| `503` | `external-service-error` | Downstream service down |
| `500` | `internal-server-error` | Unexpected error (safe message) |

### `GET /health`
```json
{ "status": "healthy", "service": "Payment Chassis" }
```

---

## 📁 Project Structure

```
enterprise-fastapi-boilerplate/
├── core/
│   ├── config.py                        # Pydantic Settings V2
│   └── logging.py                       # structlog bootstrap + correlation ContextVar
│
├── domain/
│   ├── entities/
│   │   └── payment.py                   # Pure dataclass, zero external imports
│   ├── interfaces/
│   │   └── repository.py               # Abstract repo ports (ABCs)
│   └── exceptions.py                    # Domain exception taxonomy (6 types)
│
├── application/
│   ├── uow.py                           # Unit of Work — transaction boundary
│   └── use_cases/
│       └── create_payment.py           # Orchestration: gateway → persist → outbox
│
├── infrastructure/
│   ├── database/
│   │   ├── models.py                    # SQLAlchemy ORM models
│   │   ├── session.py                   # Async engine + session factory
│   │   └── repositories/
│   │       ├── payment.py              # PaymentEntity ↔ PaymentModel adapter
│   │       └── outbox.py              # Outbox event writer
│   ├── cache/
│   │   └── redis.py                     # Redis client + Lua lock scripts
│   ├── messaging/
│   │   └── kafka_producer.py           # AIOKafka producer service
│   ├── workers/
│   │   └── outbox_relay.py            # Background polling + Kafka relay
│   └── external/
│       └── gateway_client.py          # httpx + tenacity retry/fallback
│
├── presentation/
│   ├── api/
│   │   └── payments.py                 # FastAPI router (versioned /api/v1)
│   └── middleware/
│       ├── correlation_id.py           # X-Correlation-ID lifecycle
│       ├── idempotency.py             # Redis idempotency guard
│       └── exception_handler.py       # RFC 7807 global handler
│
├── alembic/
│   └── versions/
│       ├── 001_create_payments_table.py
│       └── 002_add_outbox_events_table.py
│
├── main.py                              # Application factory
├── pyproject.toml                       # Dependencies
├── what_it_is.md                        # Architectural identity document
└── what_it_does.md                      # Operational breakdown document
```

---

## 📊 Architecture Metrics

| Metric | Value |
|---|---|
| **Layers** | 5 (core, domain, application, infrastructure, presentation) |
| **Domain external imports** | **0** — pure Python only |
| **Exception types handled** | 10 distinct handlers |
| **Retry attempts** | Up to 6 (3 primary + 3 fallback) before 502 |
| **Lock TTL** | 15 seconds (distributed idempotency) |
| **Cache TTL** | 300 seconds (idempotent response store) |
| **Outbox batch** | 50 events / 2-second poll cycle |
| **Concurrent workers** | Unlimited (SKIP LOCKED prevents duplication) |
| **Log fields per event** | 8+ (timestamp, level, module, callsite, correlation_id, ...) |

---

## 🧠 Design Patterns Implemented

| Pattern | Location | Solves |
|---|---|---|
| **Clean Architecture** | All layers | Dependency direction, testability |
| **Domain-Driven Design** | `domain/` | Pure business logic isolation |
| **Repository Pattern** | `domain/interfaces/`, `infrastructure/database/repositories/` | Swappable persistence |
| **Unit of Work** | `application/uow.py` | Atomic multi-repo transactions |
| **Transactional Outbox** | `infrastructure/workers/outbox_relay.py` | Dual-write elimination |
| **CQRS-ready** | Use case per workflow | Single responsibility |
| **Circuit Breaker** | `infrastructure/external/gateway_client.py` | Gateway resiliency |
| **Anti-Corruption Layer** | `repositories/payment.py` `_to_entity()/_to_model()` | Domain/ORM isolation |
| **Idempotency** | `presentation/middleware/idempotency.py` | Safe client retries |
| **Correlation ID** | `presentation/middleware/correlation_id.py` | Distributed traceability |

---

## 🔭 Extending This Chassis

### Adding a New Domain Entity
```
1. Create domain/entities/your_entity.py      (pure dataclass)
2. Add domain/interfaces/your_repo.py         (abstract port)
3. Create infrastructure/database/repositories/your_repo.py  (adapter)
4. Write application/use_cases/your_use_case.py
5. Add presentation/api/your_router.py
6. Run alembic revision + upgrade head
```
The middleware, logging, error handling, Kafka, and Redis are already wired. Zero changes needed.

### Swapping PostgreSQL for Another Database
1. Write a new `infrastructure/database/repositories/` adapter implementing `AbstractPaymentRepository`
2. Update `SqlAlchemyUnitOfWork` to inject the new adapter
3. No changes to domain, application, or presentation layers

---

## 📄 Documentation

| Document | Purpose |
|---|---|
| [`what_it_is.md`](./what_it_is.md) | Architectural identity — the 4W1H of what this chassis *is* |
| [`what_it_does.md`](./what_it_does.md) | Operational breakdown — what every file and every request *does* |

---

## 📜 License

MIT — use freely, commercially, and without restriction.

---

<div align="center">

**Built with engineering discipline. Designed to scale.**

*If this helped you — give it a ⭐*

</div>
