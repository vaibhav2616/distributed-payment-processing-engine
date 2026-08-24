# V1 Architecture Documentation — Distributed Payment Processing Engine

## Table of Contents

1. [System Overview](#system-overview)
2. [Layer Map](#layer-map)
3. [Domain Invariants](#domain-invariants)
4. [Eventual Consistency & Reconciliation](#eventual-consistency--reconciliation)
5. [Idempotency Contract](#idempotency-contract)
6. [Outbox Relay & Poison-Pill Circuit Breaker](#outbox-relay--poison-pill-circuit-breaker)
7. [PCI-DSS Compliance](#pci-dss-compliance)
8. [Observability & Telemetry](#observability--telemetry)
9. [Database Migrations](#database-migrations)

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

## Database Migrations

| Migration | Description |
|-----------|-------------|
| `001` | Create `payments` table |
| `002` | Add `outbox_events` table |
| `003` | Add full payment aggregate columns (`gateway_ref`, `failure_reason`, etc.) |
| `004` | Align column types to strict Numeric/Enum/UUID |
| `005` | Add `ledger_transactions` and `ledger_entries` tables |
| `006` | Add `status` (PENDING/PROCESSED/DLQ) and `retry_count` to `outbox_events`; drop old `processed` boolean |

> [!IMPORTANT]
> Migrations `005` and `006` must be applied before deploying the Outbox Relay Worker or the V1 API.
