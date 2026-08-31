<div align="center">

<img src="https://img.shields.io/badge/Python-3.12%2B-3776AB?style=for-the-badge&logo=python&logoColor=white" />
<img src="https://img.shields.io/badge/FastAPI-0.115%2B-009688?style=for-the-badge&logo=fastapi&logoColor=white" />
<img src="https://img.shields.io/badge/PostgreSQL-16%20(asyncpg)-336791?style=for-the-badge&logo=postgresql&logoColor=white" />
<img src="https://img.shields.io/badge/Redis-7%20(Locks%20%26%20Breaker)-DC382D?style=for-the-badge&logo=redis&logoColor=white" />
<img src="https://img.shields.io/badge/Redpanda-Kafka%20Drop--in-FA2D46?style=for-the-badge&logo=apachekafka&logoColor=white" />
<img src="https://img.shields.io/badge/Docker-Compose%20Topology-2496ED?style=for-the-badge&logo=docker&logoColor=white" />
<img src="https://img.shields.io/badge/Locust-Chaos%20Harness-00C853?style=for-the-badge" />

<br /><br />

# ⚡ Distributed Payment Processing Engine

### Enterprise-Grade Asynchronous Financial Infrastructure

*A production-grade, distributed payment processing engine built with Python 3.12, FastAPI, PostgreSQL, Redis, and Redpanda. Built on Clean Architecture, Domain-Driven Design (DDD), and resilient distributed systems patterns.*

<br />

</div>

---

## 🎯 What This Is

The **Distributed Payment Processing Engine** is an industrial backend system designed to reliably process financial transactions at scale. It solves the hardest distributed systems and concurrency challenges in financial software:

* **Eliminating Double-Charging**: Edge-level canonical JSON hashing and Redis distributed locks (`SET NX EX`) that intercept concurrent duplicate requests.
* **Non-Blocking 3-Phase Sagas**: Decoupling database connection lifetimes from unpredictable third-party payment gateway latency, eliminating connection pool starvation.
* **Distributed Circuit Breaking**: Multi-pod Redis circuit breaker (`CLOSED`, `OPEN`, `HALF-OPEN`) with canary probes, protecting external acquirer endpoints.
* **Asynchronous Webhook Ingestion**: Dynamic HMAC authentication with 300-second replay attack protection and `SELECT FOR UPDATE NOWAIT` locking to resolve split-brain state collisions.
* **Compensating Refund Sagas**: Safe partial and full reversals with automated compensating transactions to restore balances if the external gateway declines.
* **Immutable Double-Entry Ledger**: Mathematical verification that every state mutation is zero-sum ($\sum \text{Debits} + \sum \text{Credits} = 0$) prior to persistence.
* **Transactional Outbox & Dead Letter Queue**: Reliable, at-least-once message streaming to Kafka/Redpanda without distributed transactions (2PC), paired with poison-pill quarantine.

---

## 🏗️ Architecture at a Glance

```
                         [ HTTP Client Requests ]
                                    │
                                    ▼  ① X-Trace-Id injected across context
                         [ Trace ID Middleware ]
                                    │
                                    ▼  ② Redis Cache + Atomic Lock (SET NX EX 15)
                         [ Idempotency Middleware ] ──(hit/lock)──► Cached 200 / 409 Conflict
                                    │  (cache miss, lock acquired)
                                    ▼
                         [ FastAPI Routers /api/v1 ]
                          ├── /payments  (Payment Ingress)
                          ├── /webhooks  (Gateway Callbacks)
                          └── /refunds   (Refund Operations)
                                    │
                                    ▼
       ┌────────────────────────────┴────────────────────────────┐
       ▼                                                         ▼
[ Payment & Refund Orchestrators ]               [ Asynchronous Webhook Receiver ]
 ├─ Phase 1: Local Init (Short Txn 1)             ├─ Verify Dynamic HMAC & Timestamp
 ├─ Phase 2: Lock-Free Network Call               ├─ SELECT FOR UPDATE NOWAIT
 └─ Phase 3: Finalize / Compensate (Txn 2)        └─ Resolve Stale-State / Split-Brain
       │                                                         │
       ├────────────────────────────┬────────────────────────────┘
       ▼                            ▼
[ Redis Circuit Breaker ]   [ Domain Entity Invariants ]
  CLOSED / OPEN (60s TTL)     ├── Strict Decimal Arithmetic (No Floats)
  HALF-OPEN Canary Probe      ├── Zero-Sum Double-Entry Ledger Balance
       │                      └── Strict Finite State Machine (FSM)
       │                                    │
       └────────────────────────────┐       │
                                    ▼       ▼
                          [ SQLAlchemy Unit of Work ]
                                    │
                  ┌─────────────────┼─────────────────┐
                  ▼                 ▼                 ▼
          [ payments table ] [ ledger tables ] [ outbox_events ]
                  │                 │                 │ (Same Local ACID Txn)
                  └─────────────────┴─────────────────┘
                                    │
                                    ▼
           [ Outbox Relay Worker ] (SELECT ... FOR UPDATE SKIP LOCKED)
                                    │
                                    ├─► Relayed ──► [ Redpanda (Kafka) ]
                                    └─► Poison Pill ──► [ Dead Letter Queue (DLQ) ]
```

---

## 🛡️ The Hard Production Problems This Solves

| Challenge | Failure Mode Prevented | Architectural Implementation |
|---|---|---|
| **Dual-Write Problem** | Data saved to DB but lost by Kafka on broker disconnect | **Transactional Outbox**: DB mutation and event saved atomically in one local ACID transaction. |
| **Double Charging** | Duplicate clicks or network retries triggering duplicate debits | **Idempotency Middleware**: Canonical JSON SHA-256 hash + Redis distributed lock (`SET NX EX 15`). |
| **DB Pool Starvation** | Slow third-party gateway holding DB connections hostage | **3-Phase Lock-Free Saga**: Zero DB connections or row locks held during external network I/O. |
| **Cascading Gateway Collapse** | Repeated timeouts during upstream outages swamping gateway | **Redis Distributed Circuit Breaker**: Trips `OPEN` after 5 failures; fast-fails with `503 Service Unavailable`. |
| **Split-Brain Webhooks** | Out-of-order gateway callbacks colliding with active sweeps | **`SELECT FOR UPDATE NOWAIT`**: Rejects concurrent updates with `409 Conflict`; logs split-brain collisions. |
| **Replay Attacks** | Intercepted webhook payloads replayed to trigger duplicate credit | **Dynamic HMAC**: Cryptographic binding across `timestamp.body` with strict 300-second tolerance window. |
| **Balance Leakage** | Binary floating-point rounding errors (`0.1 + 0.2 != 0.3`) | **Strict Decimal Enforcement**: `Decimal` in Python, `NUMERIC(18, 4)` in PostgreSQL. Floats raise `TypeError`. |
| **Audit Imbalance** | Unilateral balance changes with no accounting trail | **Double-Entry Ledger**: `LedgerTransaction.build()` enforces $\sum \Delta = 0.0000$ prior to DB write. |
| **Worker Deadlocks** | Multi-pod background workers contending on the same database rows | **`SELECT FOR UPDATE SKIP LOCKED`**: Pods process non-overlapping queues in parallel without deadlocks. |
| **Poison-Pill Queue Halting** | Malformed payloads halting asynchronous event relaying | **Dead Letter Queue (DLQ)**: Quarantines un-publishable events after 5 retries; pipeline continues unimpeded. |

---

## 📐 Clean Architecture Layer Map

```
domain/                 ← INNERMOST LAYER: Zero external framework imports
  entities/             ← Pure Python dataclasses: PaymentAggregate, LedgerTransaction, Money
  exceptions/           ← Domain exception taxonomy (e.g., LedgerImbalanceError, InvalidRefundAmountError)
  interfaces/           ← Abstract repository ports (PaymentRepositoryInterface, OutboxRepositoryInterface)

application/            ← ORCHESTRATION LAYER: Pure business workflow logic
  use_cases/            ← PaymentOrchestrator, RefundOrchestrator, WebhookOrchestrator
  workers/              ← BackgroundReconciliationEngine, OutboxRelayWorker
  uow.py                ← Abstract and SQLAlchemy Unit of Work (transaction boundary)

infrastructure/         ← ADAPTER LAYER: All external I/O implementations
  database/             ← PostgreSQL models, asyncpg session factory, repository implementations
  cache/                ← Redis client with Lua-scripted distributed locking
  messaging/            ← AIOKafka producer service configured for Redpanda/Kafka
  external/             ← PaymentGatewayClient, RedisCircuitBreaker
  telemetry/            ← OpenTelemetry context propagation, PCI-DSS log redacter

presentation/           ← DELIVERY LAYER: HTTP delivery and edge security
  api/v1/               ← Versioned FastAPI routers: /payments, /webhooks, /refunds
  middleware/           ← TraceIdMiddleware, IdempotencyMiddleware, RFC 7807 Exception Handlers
  dependencies.py       ← Webhook HMAC signature verification and replay tolerance checks

alembic/                ← Database migrations (001 through 007)
docs/                   ← Architecture documentation & System Design RFC
tests/                  ← Complete unit, integration, and chaos load test harness
```

---

## ⚙️ Quickstart (Local Production Topology)

The system deploys as a multi-container topology running PostgreSQL 16, Redis 7, Redpanda Kafka, the FastAPI API server, and independent worker containers.

### 1. Requirements
* Docker & Docker Compose
* Python 3.12+ (managed via `uv`)

### 2. Boot the Cluster

```bash
# Build unified image and start all 6 containers in detached mode
docker compose up --build -d
```

Verify that all services report healthy:
```bash
docker compose ps
```

Expected healthy services:
* `dppe-postgres` (PostgreSQL 16 on port 5432)
* `dppe-redis` (Redis 7 on port 6379)
* `dppe-redpanda` (Redpanda Kafka on port 9092)
* `dppe-api` (FastAPI / Uvicorn on port 8000)
* `dppe-worker-reconciler` (Background reconciliation loop)
* `dppe-worker-outbox` (Transactional outbox relay loop)

### 3. Verify System Health

```bash
curl -i http://localhost:8000/health
```

---

## 🧪 Testing & Empirical Verification

### Run Unit & Integration Test Suite (107 Tests)

The engine includes 107 unit and integration tests covering domain invariants, ledger balance checks, circuit breaker states, and webhook security:

```bash
uv run pytest -v
```

### Run the Chaos Load Test (The Idempotency Stampede)

To empirically prove resilience against concurrency stampedes, execute the headless Locust harness:

```bash
uv run locust -f tests/load/locustfile.py --headless -u 1 -r 1 -t 10s --host http://localhost:8000
```

#### Empirical Invariants Proven by the Test:
* **0.00% Failure Rate**: Zero HTTP 500 errors. PostgreSQL connection pool limits are never exhausted.
* **Exact 1-to-9 Ratio**: For every 10 concurrent requests fired simultaneously with the same `Idempotency-Key`, **exactly 1 executes the orchestrator** (`201` or `202`) and **exactly 9 are intercepted** by the Redis distributed lock (`409 Conflict`) or cache.
* **Median Latency**: $\approx 23\text{ms}$.

---

## 📖 System Documentation

| Document | Focus & Content |
|---|---|
| [docs/architecture/system_design.md](./docs/architecture/system_design.md) | **System Design RFC (RFC-001)**: Mathematical proofs, distributed saga patterns, and deep-dive technical specifications |
| [docs/architecture.md](./docs/architecture.md) | **Architecture Documentation**: Component-level reference, FSM diagrams, and migration details |
| [what_it_is.md](./what_it_is.md) | **Architectural Rationale**: Detailed comparison explaining why each technology was chosen over alternatives |
| [what_it_does.md](./what_it_does.md) | **Runtime Lifecycle**: Step-by-step breakdown of each execution phase |
| [CONTRIBUTING.md](./CONTRIBUTING.md) | **Engineering Standards**: Layer isolation rules, commit conventions, and development workflow |
