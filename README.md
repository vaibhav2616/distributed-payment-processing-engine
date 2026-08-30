<div align="center">

<img src="https://img.shields.io/badge/Python-3.10%2B-3776AB?style=for-the-badge&logo=python&logoColor=white" />
<img src="https://img.shields.io/badge/FastAPI-0.115%2B-009688?style=for-the-badge&logo=fastapi&logoColor=white" />
<img src="https://img.shields.io/badge/PostgreSQL-asyncpg-336791?style=for-the-badge&logo=postgresql&logoColor=white" />
<img src="https://img.shields.io/badge/Redis-Distributed%20Locks-DC382D?style=for-the-badge&logo=redis&logoColor=white" />
<img src="https://img.shields.io/badge/Kafka-AIOKafka-231F20?style=for-the-badge&logo=apachekafka&logoColor=white" />
<img src="https://img.shields.io/badge/structlog-JSON%20Logging-4B8BBE?style=for-the-badge" />

<br /><br />

# ⚡ Distributed Payment Processing Engine

### Enterprise-Grade Asynchronous Payment Gateway

*A production-ready FastAPI distributed payment processing engine built on Clean Architecture, Domain-Driven Design, and distributed systems patterns.*

<br />

</div>

---

## 🎯 What This Is

The **Distributed Payment Processing Engine** is an engineering-grade backend system designed to safely and reliably process financial transactions. It solves the hardest production concerns associated with financial systems, including double-charging, eventual consistency, strict idempotency, and double-entry accounting.

---

## 🏗️ Architecture at a Glance

```
 [ HTTP Request ]
        │
        ▼  ① X-Trace-Id injected / generated (distributed trace)
 [ Trace ID Middleware ]
        │
        ▼  ② Redis cache-check + atomic distributed lock (NX + Lua) + Canonical JSON Hashing
 [ Idempotency Middleware ] ──(hit / concurrent)──► Cached 200 / 409 Conflict
        │  (cache miss, lock acquired)
        ▼
 [ FastAPI Router  /api/v1/payments ]  ← Pydantic validation only
        │
        ▼
 [ Payment Orchestrator ]  ← Orchestration only, zero I/O knowledge
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
                                       ├─► Commit Ledger Lines    │
                                       └─► Commit OutboxEvent    ─┘ (same txn, atomic)
                                                   │
                                                   ▼
 [ Background Outbox Relay ] ─(SKIP LOCKED poll, 2s)─► [ AIOKafka Producer ]
                                                              │
                                                              ▼
                                                   Downstream Services
```

---

## 🛡️ The Hard Production Problems This Solves

| Problem | The Engine's Approach |
|---|---|
| **Dual-write** | **Transactional Outbox** — event and data in one atomic DB transaction, reliably relayed to Kafka |
| **Double charge** | **Idempotency Middleware** — Redis lock + canonical JSON payload hashing + 48hr response cache |
| **No traceability** | **X-Trace-Id** bound to every log line across HTTP, domain, and worker layers |
| **Floating Point Math** | **Strict Decimal Enforcement** — `Decimal` is enforced across domain models, ORM, and API schema |
| **Zombie Payments** | **Background Reconciler** — sweeps DB for stale pending payments and polls gateway for status |
| **Non-standard errors** | **RFC 7807 `application/problem+json`** — uniform across all errors |
| **Compliance** | **PCI-DSS Log Redaction** — unconditionally drops PII and card info from structlog output |

---

## 📐 Layer Architecture (Clean Architecture)

```
domain/           ← INNERMOST — zero external dependencies
  entities/       ← Pure Python dataclasses, business invariants, FSM, Ledger logic
  exceptions/     ← Domain exception taxonomy
  interfaces/     ← Abstract repository ports (ABCs)

application/      ← Orchestration only
  use_cases/      ← Payment Orchestrator (3-phase commit saga)
  workers/        ← Reconciler, Outbox Relay
  uow.py          ← Unit of Work — transaction boundary

infrastructure/   ← ALL external I/O (implements domain ports)
  database/       ← SQLAlchemy models, session, repo adapters
  cache/          ← Redis client (async)
  messaging/      ← Kafka producer (AIOKafka)
  telemetry/      ← Context vars, PCI-DSS redacter
  external/       ← Payment gateway HTTP client (httpx)

presentation/     ← HTTP surface only
  api/            ← FastAPI versioned routers
  middleware/     ← Correlation ID, Idempotency, RFC 7807 handler

alembic/          ← Schema migrations
docs/             ← Architecture docs
```

**Dependency Rule:** Domain ← Application ← Infrastructure. The domain never imports from any outer layer. Ever.

---

## 🚀 Key Features

### ⚛️ Eventual Consistency & Reconciliation
If the payment gateway times out during processing, the engine returns a `202 Accepted` status. The `BackgroundReconciliationEngine` guarantees that this payment will be swept, verified, and correctly captured or failed asynchronously, with no manual intervention required.

### 🔒 Distributed Idempotency (Canonical Hashing)
Every `POST /payments` is guarded by strict idempotency rules:
1. The JSON payload is parsed and canonicalized (sorted keys, no whitespace) and hashed via SHA-256.
2. If a subsequent request arrives with the same `Idempotency-Key` but a mutated payload, the engine rejects it with a `409 Conflict`.
3. Pydantic schema validation failures bypass the cache, allowing clients to fix malformed payloads and retry.

### 📓 Immutable Double-Entry Ledger
Every successful payment immediately computes a zero-sum `LedgerTransaction`. Debits and credits must perfectly balance, and the database schema enforces immutability via `RESTRICT` foreign keys.

### 💊 Poison-Pill Circuit Breaker
The Transactional Outbox relay uses a circuit breaker that detects transient vs. terminal Kafka errors. If an event is too large or permanently fails serialization, it is sent to a Dead Letter Queue (`DLQ`) and bypassed, preventing the queue from stalling.

---

## ⚙️ Quickstart (Local Development)

### 1. Requirements
- Docker & Docker Compose
- Python 3.10+
- `uv` (or pip/poetry)

### 2. Boot Infrastructure

The engine requires PostgreSQL, Redis, and Kafka to run. A `docker-compose.yml` is included.

```bash
docker-compose up -d
```

### 3. Install & Migrate

```bash
uv venv
source .venv/bin/activate
uv pip sync requirements.txt
make migrate-up
```

### 4. Run the API

```bash
make dev
```
*API runs on `http://localhost:8000`*

### 5. Run the Background Workers (in separate terminals)

```bash
# Terminal 1: Outbox Relay (sends events to Kafka)
python -m application.workers.outbox_relay

# Terminal 2: Background Reconciler (sweeps timed-out requests)
python -m application.workers.reconciler
```

---

## 📖 Further Documentation

| File | Description |
|---|---|
| [`what_it_is.md`](./what_it_is.md) | The architectural decisions and why they were chosen over alternatives |
| [`what_it_does.md`](./what_it_does.md) | A complete flow breakdown of the engine's phases |
| [`docs/architecture.md`](./docs/architecture.md) | In-depth technical guide to the engine's guarantees |

---

## 🧪 Testing

The engine includes a full test suite with ~75 tests validating domain invariants, integration flows, and idempotency states.

```bash
pytest -v
```
