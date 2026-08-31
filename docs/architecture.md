# Architecture Documentation — Distributed Payment Processing Engine (V1 & V2 Core)

## Table of Contents

1. [System Overview](#system-overview)
2. [Layer Map](#layer-map)
3. [Domain Invariants](#domain-invariants)
4. [Eventual Consistency & Reconciliation](#eventual-consistency--reconciliation)
5. [Distributed Circuit Breaker](#distributed-circuit-breaker-infrastructureexternalcircuit_breakerpy)
6. [Asynchronous Webhook Receiver](#asynchronous-webhook-receiver-presentationapiv1webhookspy)
7. [Refund Orchestration & Compensating Sagas](#refund-orchestration--compensating-sagas-presentationapiv1refundspy)
8. [Idempotency Contract](#idempotency-contract)
9. [Outbox Relay & Poison-Pill Circuit Breaker](#outbox-relay--poison-pill-circuit-breaker)
10. [PCI-DSS Compliance](#pci-dss-compliance)
11. [Observability & Telemetry](#observability--telemetry)
12. [Containerized Production Topology](#containerized-production-topology-docker-composeyml)
13. [Chaos Concurrency Harness & Load Testing](#chaos-concurrency-harness--load-testing-testsloadlocustfilepy)
14. [Database Migrations](#database-migrations)
15. [System Design RFC Reference](#system-design-rfc-reference)

---

## System Overview

A **Clean Architecture**, **event-driven** payment processing engine built on FastAPI + PostgreSQL + Kafka. The system is designed around three non-negotiable guarantees:

1. **Exactly-once processing** — every charge attempt is idempotent at the HTTP, application, and gateway layer.
2. **Auditability** — every state transition is recorded in an immutable double-entry ledger.
3. **Resilience** — the system recovers from any partial failure (network timeout, pod crash, broker unavailability) via the Transactional Outbox and Background Reconciler.

---

## Layer Map

```
presentation/           ← FastAPI routers, Pydantic schemas, middleware
application/            ← Orchestrators, use cases, background workers (no ORM imports)
domain/                 ← Pure Python entities, value objects, domain exceptions
infrastructure/         ← SQLAlchemy ORM, Redis, Kafka, external gateway client
```

**Zero framework leakage into the domain**: `domain/` has zero imports from FastAPI, SQLAlchemy, or any infrastructure library.

---

## Domain Invariants

### 1. Zero-Sum Ledger (Double-Entry Accounting)

Every `CAPTURED` payment produces exactly one `LedgerTransaction` containing two `LedgerEntry` rows:

| Side   | Account         | Sign   |
|--------|-----------------|--------|
| Debit  | accounts_receivable | `+amount` |
| Credit | gateway_payable     | `+amount` |

**Invariant enforced by**: `LedgerTransaction.build()` raises `LedgerBalanceError` if `sum(debits) != sum(credits)`. This is checked at construction time, before any database write.

### 2. Strict Decimal Arithmetic — No Floats

All monetary values are typed as `Decimal` throughout the system:

- **Domain**: `PaymentAggregate.amount: Decimal`
- **Database**: `Numeric(precision=18, scale=4, asdecimal=True)` — the ORM returns `Decimal`, never `float`
- **API Input**: Pydantic `condecimal(gt=0)` on `CreatePaymentRequest.amount`
- **Outbox Payload**: serialised as `str(amount)` — JSON has no `Decimal` type

`PaymentAggregate.create()` raises `TypeError` if passed a `float`. Any pathway that introduces a `float` is a bug.

### 3. Immutable Ledger Entries

The `ledger_entries.transaction_id` FK is configured with the default `RESTRICT` behaviour. The database **will raise an `IntegrityError`** if any code attempts to delete a `ledger_transactions` row while entries exist. There are no cascade deletes in the ledger schema.

### 4. Payment Status Transitions (Strict FSM)

```
PENDING ──► CAPTURED
PENDING ──► FAILED
```

Any transition not listed above raises `InvalidStateTransitionError`. There is no path back to `PENDING` once a terminal state is reached.

---

## Eventual Consistency & Reconciliation

### The Happy Path (Synchronous, ~99% of requests)

```
Client POST /api/v1/payments
    │
    ▼
[IdempotencyMiddleware] ── cache hit? ──► return cached 201/202/422
    │
    ▼
[PaymentOrchestrator]
 ├─ Phase 1: persist PENDING payment + outbox event  [TX 1 — committed]
 ├─ Phase 2: call gateway.charge()
 └─ Phase 3: persist CAPTURED/FAILED + ledger + outbox event  [TX 2 — committed]
    │
    ▼
HTTP 201 Created  (payment fully CAPTURED in this request)
```

### The Timeout Path (Asynchronous, ~1% of requests)

```
Client POST /api/v1/payments
    │
    ▼
[PaymentOrchestrator]
 ├─ Phase 1: persist PENDING  [committed]
 └─ Phase 2: gateway.charge() → TimeoutError / 5xx
    │  CRITICAL: we do NOT know if the gateway processed the charge.
    │
    ▼
HTTP 202 Accepted  (PENDING — reconciler will resolve)
    │
    ▼ (async, within 5 minutes)
[BackgroundReconciliationEngine — runs every 60 seconds]
 ├─ Step 1: SELECT id FROM payments WHERE status=PENDING AND created_at < now()-5min
 ├─ Step 2: gateway.verify_status(id)  [no DB locks held during network I/O]
 └─ Step 3: FOR UPDATE NOWAIT → update to CAPTURED or FAILED + commit
```

**The 5-minute buffer** prevents the Reconciler from stepping on an active Orchestrator request. If the Reconciler cannot acquire the lock (`NOWAIT`), it skips that row and retries on the next sweep.

**Concurrency control**: Multiple Reconciler pods use `SELECT FOR UPDATE SKIP LOCKED` on the batch fetch. Each pod independently works on a non-overlapping set of stale payments.

---

## Distributed Circuit Breaker (`infrastructure/external/circuit_breaker.py`)

To prevent cascading network failures across distributed pods, external calls to `PaymentGatewayInterface` (`charge`, `verify_status`) are protected by `RedisCircuitBreaker`.

### Circuit States

```
         ┌─────────────────────────┐
         │         CLOSED          │ (Normal Operation)
         │  consecutive_failures < 5│
         └───────────┬─────────────┘
                     │ 5 consecutive network failures (5xx / timeouts)
                     ▼
         ┌─────────────────────────┐
         │          OPEN           │ (Fast Failure Mode)
         │     Redis TTL = 60s     │ Immediate CircuitBreakerOpenException
         └───────────┬─────────────┘
                     │ TTL expires (60s cool-off)
                     ▼
         ┌─────────────────────────┐
         │        HALF-OPEN        │ (Canary / Recovery Testing)
         │  Single probe request   │
         └───┬─────────────────┬───┘
             │ Success         │ Failure (5xx / timeout)
             ▼                 ▼
          CLOSED              OPEN (fresh 60s TTL)
```

### Invariants & Guarantees

1. **Failure Threshold**: 5 consecutive network timeouts or 5xx server errors transition state to `OPEN`.
2. **Cool-off Window**: 60 seconds Redis TTL on `OPEN`.
3. **Half-Open Single Probe**: Exactly one request is allowed through to test upstream health. Concurrent requests fast-fail with `CircuitBreakerOpenException`.
4. **4xx Domain Safety**: HTTP 4xx responses (card declined, insufficient funds) are valid business outcomes and are **never** counted as failures.
5. **No Zombie PENDING Records**: The `PaymentOrchestrator` pre-flights the circuit breaker before opening database transactions. If the breaker is `OPEN`, the request is rejected immediately with `503 Service Unavailable` (`Retry-After: 60`), preventing database exhaustion.

---

## Asynchronous Webhook Receiver (`presentation/api/v1/webhooks.py`)

Handles asynchronous payment notifications from external gateways (e.g. Stripe, Adyen).

### Security & Invariants

1. **Replay Attack Mitigation & Dynamic HMAC (`verify_webhook_signature`)**:
   - Parses timestamp from `X-Gateway-Timestamp` or `t=` signature parameter.
   - Enforces a **5-minute (300 seconds)** tolerance window against current server time; older timestamps are rejected with `401 Unauthorized` (`Replay Attack detected`).
   - Dynamic HMAC: signature is verified against `f"{timestamp}.{raw_body.decode()}"` using constant-time comparison.
2. **Concurrency Control (`SELECT FOR UPDATE NOWAIT`)**:
   - Locks the target payment row immediately inside `uow.begin()`.
   - If locked by another worker (e.g., Reconciler), raises `ConcurrentUpdateException` and returns `409 Conflict`, signaling the gateway to retry with exponential backoff.
3. **Distributed Split-Brain Guard & True Idempotency**:
   - If the payment was already resolved (no longer `PENDING`), compares `payment.status` to incoming `webhook_status`.
   - **True Idempotency**: If `payment.status == webhook_status`, safely rolls back and returns `200 OK`.
   - **Distributed Split-Brain**: If `payment.status != webhook_status`, rolls back without applying the transition, logs a **CRITICAL** security alert detailing `payment_id`, `db_status`, and `webhook_status`, and returns `200 OK` (to prevent gateway retry loops) while flagging for immediate manual engineering review.
4. **Zero-Sum Ledger & Outbox Execution**:
   - On `CAPTURED`: creates and verifies a balanced `LedgerTransaction` and emits `payment.captured` event.
   - On `FAILED`: marks payment failed and emits `payment.failed` event.

---

## Refund Orchestration & Compensating Sagas (`presentation/api/v1/refunds.py`)

Handles safe reversals of captured funds without violating immutable accounting or over-refunding.

### Strict Invariants & Domain FSM

1. **State Machine Validity**: Refunds can only be executed against payments in state `CAPTURED` or `PARTIALLY_REFUNDED`.
2. **Cap on Refunds**: If `amount_refunded + refund_amount > amount_captured`, the aggregate rejects the attempt with `InvalidRefundAmountError` (HTTP 422).
3. **State Transitions**:
   * If `amount_refunded == amount_captured`: state transitions to `REFUNDED`.
   * If `amount_refunded < amount_captured`: state transitions to `PARTIALLY_REFUNDED`.

### The 3-Phase Refund Saga & Compensating Transaction

```
Phase 1: Local Reservation (Short DB Transaction 1)
  - uow.begin() -> SELECT FOR UPDATE NOWAIT on payment
  - payment.process_refund(amount) -> update amount_refunded
  - Enqueue refund.initiated outbox event -> Commit

Phase 2: Gateway Network Call (No DB resources held)
  - gateway.refund(gateway_ref, amount, refund_idempotency_key)

Phase 3: Finalization or Compensating Rollback
  - SUCCESS: Enqueue refund.succeeded outbox event -> Commit -> HTTP 200
  - DECLINE: GatewayDeclineException caught
      - uow.begin() -> SELECT FOR UPDATE NOWAIT on payment
      - payment.fail_refund(amount)  [Compensating Transaction]
      - Enqueue refund.failed outbox event -> Commit -> HTTP 422
```

---

## Idempotency Contract

### Rules

| Rule | Value |
|------|-------|
| Header | `X-Idempotency-Key` (required on all `POST` requests to `/api/v1/payments`) |
| TTL | **172,800 seconds (48 hours)** |
| Scope | Per `(Idempotency-Key, endpoint)` — keys are not global |
| Cached statuses | `200`, `201`, `202`, `422` (domain declines only) |
| Not cached | `422` from Pydantic schema validation (client must be allowed to fix and retry) |

### Payload Hashing (Canonicalization)

To prevent false-positive `409 Conflict` responses caused by harmless differences in whitespace or JSON key ordering from upstream clients, the middleware **canonicalizes** the body before hashing:

```python
parsed = json.loads(req_body)
canonical = json.dumps(parsed, sort_keys=True, separators=(",", ":"))
req_hash = hashlib.sha256(canonical.encode()).hexdigest()
```

If the body is not valid JSON (e.g., empty body), the middleware falls back to hashing the raw bytes.

### Conflict Rejection

If a subsequent request arrives with the **same `Idempotency-Key` but a different payload hash**, the middleware immediately returns:

```http
HTTP/1.1 409 Conflict
{"error": "Idempotency-Key already used with a different payload."}
```

### Safe 422 Caching

The `RequestValidationError` exception handler (Pydantic schema failures) sets `request.state.skip_idempotency_cache = True`. The middleware checks this flag and skips the Redis `SET` call. Domain-originated 422 responses (e.g., gateway hard decline) do not set this flag and are cached normally.

---

## Outbox Relay & Poison-Pill Circuit Breaker

The **Transactional Outbox** decouples database writes from Kafka publishes. Every state transition (PENDING, CAPTURED, FAILED) is written atomically to `outbox_events` in the same database transaction as the payment row.

### Outbox Event Schema

All outbox payloads contain only **non-sensitive domain data**:

```json
{
  "payment_id": "uuid",
  "amount": "150.0000",
  "currency": "USD",
  "status": "CAPTURED",
  "reference_id": "merchant-transaction-id",
  "trace_id": "opentelemetry-trace-id"
}
```

`source_token`, `card_number`, and all PCI-DSS sensitive fields are **strictly excluded** from outbox payloads.

### Relay Worker (`application/workers/outbox_relay.py`)

The relay polls `outbox_events` using `SELECT FOR UPDATE SKIP LOCKED` to safely distribute work across multiple relay pods.

### Poison-Pill Circuit Breaker

| Condition | Action |
|-----------|--------|
| Transient error (broker disconnect, network timeout) | Leave `status = PENDING`, increment `retry_count`, retry on next poll |
| Terminal error (`RecordTooLargeError`, serialization failure) | Set `status = DLQ`, log `CRITICAL`, skip to next record |
| `retry_count > 5` (max retries exhausted) | Set `status = DLQ`, log `CRITICAL`, skip to next record |

A poison-pill event **never blocks the batch**. The relay continues processing the remaining records in the same sweep.

### `outbox_events` Status Lifecycle

```
PENDING ──► PROCESSED   (happy path)
PENDING ──► DLQ         (terminal error or max retries)
```

---

## PCI-DSS Compliance

### Log Redaction (`infrastructure/telemetry/redacter.py`)

A structlog processor `mask_sensitive_data` is injected into the logging pipeline **before** the JSON renderer. It deeply scans every log event dictionary (including nested dicts and lists) and replaces any value whose key matches the blocklist with `[REDACTED]`:

**Blocklist**: `source_token`, `card_number`, `cvv`, `pan`, `password`, `authorization`

This processor is unconditional — it runs on every log line, in every environment.

### Outbox PII Exclusion

The `PaymentOrchestrator` constructs outbox payloads by **explicit allow-listing** the fields to include. It never passes the full command object or any model dict to the outbox.

---

## Observability & Telemetry

### Trace Propagation

- **Ingress**: `TraceIdMiddleware` reads `X-Trace-Id` from the request header (or generates a UUID4). It binds the value to `structlog.contextvars` for the lifecycle of the request.
- **Outbox**: The `PaymentOrchestrator` calls `get_current_trace_id()` from contextvars and stamps it into the outbox event payload at enqueue time.
- **Workers**: Background workers (`outbox_relay`, `reconciler`) extract `trace_id` from event payloads and bind it to their local structlog context, creating an unbroken trace chain across process boundaries.

### Structured Logging

All logs are emitted as structured JSON via structlog. Every log line contains:
- `trace_id` (propagated from HTTP layer or outbox payload)
- `timestamp` (ISO 8601, UTC)
- `level`, `logger`, `filename`, `func_name`, `lineno`

---

## Containerized Production Topology (`docker-compose.yml`)

The system deploys as a multi-service production topology separating stateless web processes, asynchronous worker loops, and stateful infrastructure:

```
                    ┌────────────────────────────┐
                    │      HTTP Clients          │
                    └──────────────┬─────────────┘
                                   │ :8000
                                   ▼
                    ┌────────────────────────────┐
                    │     api (FastAPI/Uvicorn)  │
                    └──────┬───────────────┬─────┘
                           │               │
       ┌───────────────────┘               └──────────────────┐
       ▼                                                      ▼
┌──────────────┐                                       ┌──────────────┐
│   postgres   │                                       │    redis     │
│  Postgres 16 │                                       │   Redis 7    │
│  (Port 5432) │                                       │  (Port 6379) │
└──────▲───────┘                                       └──────▲───────┘
       │                                                      │
       ├─────────────────────────────────┐                    │
       │                                 │                    │
┌──────┴───────────────┐          ┌──────┴───────────────┐    │
│  worker-reconciler   │          │    worker-outbox     │    │
│ (Reconciliation Loop)│          │  (Outbox Relay Loop) │    │
└──────────────────────┘          └──────────────┬───────┘    │
                                                 │            │
                                                 ▼            │
                                          ┌──────────────┐    │
                                          │   redpanda   │    │
                                          │(Kafka Drop-in│    │
                                          │  Port 9092)  │    │
                                          └──────────────┘    │
                                                              │
                                          (Locks & Breaker) ──┘
```

* **`postgres`** (`postgres:16-alpine`): Stores payment records, double-entry ledger entries, and transactional outbox events.
* **`redis`** (`redis:7-alpine`): Distributed lock manager (`SET NX EX 15`), idempotency response cache (48hr TTL), and distributed circuit breaker state coordinator.
* **`redpanda`** (`docker.redpanda.com/redpandadata/redpanda:v23.3.11`): Lightweight Kafka drop-in running in single-node mode.
* **`api`**: Boots from `Dockerfile`, executes `alembic upgrade head`, and serves REST endpoints via Uvicorn.
* **`worker-reconciler`**: Independent container running [BackgroundReconciliationEngine.run_forever()](file:///home/nitrov/distributed-payment-processing-engine/application/workers/reconciler.py).
* **`worker-outbox`**: Independent container running [OutboxRelayWorker.run()](file:///home/nitrov/distributed-payment-processing-engine/application/workers/outbox_relay.py) streaming events to Redpanda topics.

---

## Chaos Concurrency Harness & Load Testing (`tests/load/locustfile.py`)

To verify system resilience against race conditions, the engine includes a Locust load test harness simulating an **Idempotency Stampede**:

* **Simulated Race Condition**: Each simulated user cycle generates a unique `Idempotency-Key` and uses `gevent.pool.Group` to fire **10 concurrent requests simultaneously** with that exact key.
* **Empirical Assertions**:
  1. **Zero HTTP 500s**: Confirms that PostgreSQL connection pool limits are never exhausted under concurrency spikes.
  2. **Exactly 1 Orchestrator Execution**: Asserts that only 1 of the 10 requests acquires the Redis lock and invokes the orchestrator (`201 Created` or `202 Accepted`).
  3. **Guarded Replays**: Asserts that the remaining 9 requests are intercepted by the Redis distributed lock (`409 Conflict`) or safely return the cached response (`X-Cache: HIT`).
* **Execution**:
  ```bash
  uv run locust -f tests/load/locustfile.py --headless -u 1 -r 1 -t 10s --host http://localhost:8000
  ```

---

## Database Migrations

| Migration | Description |
|-----------|-------------|
| `001` | Create `payments` table |
| `002` | Add `outbox_events` table |
| `003` | Add full payment aggregate columns (`gateway_ref`, `failure_reason`, etc.) |
| `004` | Align column types to strict Numeric/Enum/UUID |
| `005` | Add `ledger_transactions` and `ledger_entries` tables |
| `006` | Add `status` (PENDING/PROCESSED/DLQ) and `retry_count` to `outbox_events`; drop old `processed` boolean |
| `007` | Add `amount_refunded` column and `PARTIALLY_REFUNDED` enum value to `paymentstatus` |

> [!IMPORTANT]
> All migrations must be applied before booting application pods. The `api` container in `docker-compose.yml` automatically runs `alembic upgrade head` before serving traffic.

---

## System Design RFC Reference

For the comprehensive architectural RFC detailing the theoretical underpinnings, mathematical proofs, and distributed systems trade-offs, consult [docs/architecture/system_design.md](file:///home/nitrov/distributed-payment-processing-engine/docs/architecture/system_design.md).
