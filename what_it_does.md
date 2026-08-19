# What It Does — Payment Chassis: A Complete Operational Breakdown

> *A file-by-file, flow-by-flow walkthrough of every mechanism in this system, written so you can trace any request from entry to exit and explain exactly what happens and why.*

---

## 1. What Happens the Moment the App Starts

Everything begins in [`main.py`](./main.py). The `create_app()` factory function runs **once** at process boot, in this exact order:

### Step 1 — Structured Logging is Configured First
```python
configure_logging(log_level=settings.LOG_LEVEL, log_format=settings.LOG_FORMAT)
```
`core/logging.py` bootstraps `structlog`. From this moment on, every `logger.info(...)` call anywhere in the system — HTTP handlers, Redis clients, Kafka workers, outbox relays — automatically emits a JSON object with:
- `timestamp` (ISO 8601, UTC)
- `level` (info / warning / error)
- `logger` (module name)
- `filename`, `func_name`, `lineno` (callsite)
- `correlation_id` (if a request is in flight — more on this below)

In development, set `LOG_FORMAT=console` to get coloured human-readable output. In production, `LOG_FORMAT=json` gives you output that Datadog, Grafana Loki, or CloudWatch can index and query.

### Step 2 — RFC 7807 Exception Handlers are Registered
```python
register_exception_handlers(app)
```
`presentation/middleware/exception_handler.py` registers **10 handlers** — one per exception class, ordered from most specific to most generic. This means:
- A `DuplicateTransactionError` always returns a `409 Conflict`
- A `PaymentGatewayError` always returns a `502 Bad Gateway`
- An uncaught `Exception` always returns a `500` — **without leaking stack traces to the client**

### Step 3 — Middleware is Attached (Order Is Critical)
```python
app.add_middleware(IdempotencyMiddleware)   # inner — runs second
app.add_middleware(CorrelationIdMiddleware) # outer — runs first
```
FastAPI/Starlette middleware is a stack: middleware added last runs **outermost** (first on the way in, last on the way out). So `CorrelationIdMiddleware` wraps `IdempotencyMiddleware` wraps the route handler. This ordering is intentional — the correlation ID must exist before the idempotency check runs, so the Redis lock log lines carry the ID.

### Step 4 — Routers are Mounted
```python
app.include_router(payments_router, prefix="/api/v1")
```
All payment endpoints are under `/api/v1/payments` — versioned from day one.

### Step 5 — Lifespan Events Wire Infrastructure
On startup:
```python
await kafka_producer.start()
asyncio.create_task(poll_outbox_events())
```
- The Kafka producer connects to the broker and is held open for the process lifetime.
- The outbox relay starts as a background `asyncio.Task` — an infinite loop that polls the database every 2 seconds.

On shutdown, `kafka_producer.stop()` flushes buffered messages and closes the connection cleanly.

---

## 2. What Happens on Every Incoming HTTP Request

### Phase 1 — Correlation ID Middleware (`presentation/middleware/correlation_id.py`)

The **first** thing that touches every request. It does three things:

**A. Resolve the Correlation ID**
```python
correlation_id = request.headers.get("X-Correlation-ID") or str(uuid.uuid4())
```
If the client (another microservice, API gateway, or browser) sends `X-Correlation-ID: abc-123`, that ID is used. If not, a fresh UUID4 is generated. This means every request has a unique, traceable ID from the moment it enters the system.

**B. Bind the ID into Three Places Simultaneously**
```python
request.state.correlation_id = correlation_id     # available to route handlers
token = correlation_id_ctx.set(correlation_id)    # ContextVar for custom processor
structlog.contextvars.bind_contextvars(...)        # structlog's own async-safe store
```
After this line, **every log statement fired during this request** — whether in the route handler, the use case, the repository, or the Kafka producer — will include `"correlation_id": "abc-123"` automatically, with no code change needed in those modules.

**C. Echo the ID Back**
```python
response.headers["X-Correlation-ID"] = correlation_id
```
The client always receives the correlation ID back in the response header. This enables end-to-end tracing: the client logs `X-Correlation-ID: abc-123`, you query your log aggregator for that ID, and you see every log line from every layer that touched this request.

**On exit:** The ContextVar token is reset (`correlation_id_ctx.reset(token)`) and structlog's context is cleared. This prevents correlation IDs from leaking between requests in the same async worker.

---

### Phase 2 — Idempotency Middleware (`presentation/middleware/idempotency.py`)

Only activates for `POST` requests with an `X-Idempotency-Key` header. This is the mechanism that prevents double-charges.

#### The Complete Idempotency Flow:

```
Request arrives with X-Idempotency-Key: "client-uuid-xyz"
        │
        ▼
Check Redis: GET idempotency:resp:client-uuid-xyz
        │
   ┌────┴──────────────────────────────────┐
   │ Cache HIT                             │ Cache MISS
   ▼                                       ▼
Return the cached response         Attempt to SET idempotency:lock:client-uuid-xyz
immediately (200 OK)               NX=True (only if not exists), EX=15s
                                           │
                                   ┌───────┴───────────────────┐
                                   │ Lock ACQUIRED             │ Lock FAILED
                                   ▼                           ▼
                              Call next handler         Return 409 Conflict
                              (the actual route)        "Concurrent request
                                   │                    with same key"
                              If response 200:
                              Cache body in Redis
                              EX=300s (5 min)
                                   │
                              FINALLY: Release lock
                              via Lua script (atomic)
```

**Why a Lua script for lock release?**
The release must be atomic: "get the value, compare to my token, delete only if it matches." If you do this in three separate Redis commands, another process could sneak in between steps. A Lua script runs as a single atomic operation on the Redis server — no race condition possible.

**Why 15 seconds for the lock?**
This is the maximum time a legitimate request is allowed to be in flight. If the process crashes mid-request, the lock expires automatically — the next retry attempt will acquire a fresh lock and process normally.

---

### Phase 3 — Presentation Layer: The Route Handler (`presentation/api/payments.py`)

```python
@router.post("/", response_model=PaymentResponse, status_code=201)
async def create_payment(
    payload: PaymentCreateRequest,
    uow: AbstractUnitOfWork = Depends(get_uow),
) -> PaymentResponse:
```

**What this layer does:**
- Validates inbound JSON against `PaymentCreateRequest` (Pydantic schema)
- If validation fails, FastAPI raises `RequestValidationError` → caught by the RFC 7807 handler → `422 Unprocessable Content` with structured error details
- Constructs a **domain entity** from the validated data
- Calls the use case with the entity
- Maps the returned domain entity back to a `PaymentResponse` (Pydantic) for serialization

**What this layer does NOT do:**
- Does not import SQLAlchemy, Redis, or Kafka
- Does not contain business logic
- Does not know how payments are stored

The route handler is thin by design. It is a **translation boundary** between HTTP and the application layer.

---

### Phase 4 — Application Layer: Use Case Orchestration (`application/use_cases/create_payment.py`)

`CreatePaymentUseCase.execute()` is the **conductor** — it orchestrates infrastructure without knowing its details.

#### Step 1: Gateway Charge
```python
gateway_response = await self.uow.gateway.charge_with_fallback({...})
```
The gateway call happens **before** the database transaction opens. Why? Because we don't want to hold a database connection open while waiting for a network call to a third-party API. If the gateway fails, we raise `PaymentGatewayError` immediately — nothing is committed, nothing is corrupted.

#### Step 2: Atomic Persistence (inside `async with self.uow`)
```python
async with self.uow as uow:
    payment_data.mark_success()          # domain behaviour
    await uow.payments.add(payment_data) # repo port call
    await uow.outbox.enqueue(...)        # outbox port call
```
When `async with self.uow` exits normally, `SqlAlchemyUnitOfWork.__aexit__` calls `session.commit()`. **Both** the payment row and the outbox event row are committed in the same database transaction. They either both land or both roll back. This is the Transactional Outbox pattern.

If any exception fires inside this block, `__aexit__` calls `session.rollback()` — neither row is written.

---

### Phase 5 — Infrastructure: Repository Adapters (`infrastructure/database/repositories/`)

The use case calls `uow.payments.add(entity)`. This arrives at `SqlAlchemyPaymentRepository.add()`:

```python
async def add(self, entity: PaymentEntity) -> None:
    self._session.add(self._to_model(entity))
```

`_to_model()` maps the pure Python `PaymentEntity` dataclass → `PaymentModel` SQLAlchemy ORM object. This mapping is the **only place** that knows about both the domain and the ORM simultaneously. It is a structural firewall.

Similarly, `_to_entity()` maps the other direction — when reading from the DB, the ORM model is converted back to a domain entity before being returned to the application layer. The application layer and domain layer never see SQLAlchemy types.

---

### Phase 6 — Infrastructure: The Payment Gateway Client (`infrastructure/external/gateway_client.py`)

```python
@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    retry=retry_if_exception_type((httpx.RequestError, httpx.TimeoutException, PaymentGatewayException)),
    reraise=True,
)
async def _call_gateway(self, url: str, payload: dict) -> dict:
```

**What tenacity does here:**
- Attempt 1: Immediate
- Attempt 2: Wait 2 seconds
- Attempt 3: Wait 4 seconds (exponential backoff)
- After 3 failures: re-raise the exception

**The fallback flow:**
```python
try:
    return await self._call_gateway(self.primary_url, payload)  # 3 retries
except Exception:
    return await self._call_gateway(self.fallback_url, payload) # 3 more retries
    # If this also fails → raise PaymentGatewayError → caught by exception handler → 502
```

The use case never sees `httpx.RequestError` or `tenacity.RetryError`. It only ever sees `PaymentGatewayError` (a domain concept). The HTTP library is completely hidden.

---

## 3. What Happens After the HTTP Request Returns (Background)

### The Transactional Outbox Relay (`infrastructure/workers/outbox_relay.py`)

This background worker runs in a continuous loop, completely independent of incoming requests.

```
Every 2 seconds:
        │
        ▼
Open async DB session
        │
        ▼
SELECT * FROM outbox_events
WHERE processed = false
ORDER BY created_at ASC
LIMIT 50
FOR UPDATE SKIP LOCKED     ← critical for horizontal scaling
        │
        ▼
For each event:
  1. Derive Kafka topic from event_type  ("payment.completed" → "payment-completed")
  2. await kafka_producer.send_and_wait(topic, payload)
  3. event.processed = True
  4. session.add(event)
        │
        ▼
session.commit()   ← atomic: all 50 events marked processed together
        │
        ▼
await asyncio.sleep(2)
```

**Why `FOR UPDATE SKIP LOCKED`?**
If you run two instances of this service (horizontal scaling), both workers could try to process the same events simultaneously. `SKIP LOCKED` tells PostgreSQL: "lock the rows I'm about to process, and skip any rows already locked by another worker." Each worker gets a non-overlapping batch. Events are never duplicated, never missed.

**Why at-least-once, not exactly-once?**
If the worker crashes after publishing to Kafka but before marking `processed = True`, the same event will be published again on the next poll. This is safe because Kafka consumers are expected to be idempotent. This is the standard industry trade-off — exactly-once delivery is far more complex and costly than idempotent consumers.

---

## 4. What Happens When Things Go Wrong (Error Paths)

### 4.1 Validation Failure (client sent bad data)
```
POST /api/v1/payments  { "amount": -50, "currency": "XY" }
        │
        ▼
PaymentCreateRequest Pydantic validation fails
        │
        ▼
FastAPI raises RequestValidationError
        │
        ▼
_handle_request_validation_error() fires
        │
        ▼
Response: 422  Content-Type: application/problem+json
{
  "type": "https://api.example.com/errors/request-validation-error",
  "title": "Request Validation Failed",
  "status": 422,
  "detail": "amount: Input should be greater than 0; currency: String should have at most 3 characters",
  "instance": "/api/v1/payments",
  "correlation_id": "abc-123",
  "validation_errors": [...]
}
```

### 4.2 Both Payment Gateways Down
```
Both primary and fallback acquirers return 500 after 3 retries each
        │
        ▼
PaymentGatewayException raised in gateway_client.py
        │
        ▼
Caught in CreatePaymentUseCase.execute()
        │
        ▼
Re-raised as PaymentGatewayError (domain exception)
        │
        ▼
_handle_payment_gateway_error() fires
        │
        ▼
Response: 502 Bad Gateway  Content-Type: application/problem+json
{
  "type": "https://api.example.com/errors/payment-gateway-error",
  "title": "Payment Gateway Unavailable",
  "status": 502,
  "detail": "Payment gateway unavailable.",
  "instance": "/api/v1/payments",
  "correlation_id": "abc-123"
}
```
The client knows: retry later. No stack trace exposed. No internal service name leaked.

### 4.3 Database Failure Mid-Transaction
```
uow.payments.add(entity) succeeds  (staged in SQLAlchemy session)
uow.outbox.enqueue(...) succeeds   (staged in SQLAlchemy session)
session.commit() → database connection drops
        │
        ▼
SQLAlchemy raises OperationalError
        │
        ▼
UoW __aexit__ catches it → session.rollback()
        │
        ▼
Exception propagates to _handle_unhandled_exception()
        │
        ▼
Response: 500 Internal Server Error  (safe message only)
        │
        ▼
Full traceback logged via structlog (with correlation_id)
```
Neither the payment row nor the outbox event was written. The system is consistent.

---

## 5. What Each File Does — Complete Reference

### `core/`

| File | What It Does |
|---|---|
| `config.py` | Reads `DATABASE_URL`, `REDIS_URL`, `KAFKA_BOOTSTRAP_SERVERS`, `LOG_LEVEL`, `LOG_FORMAT` from env/`.env`. Fails loudly at startup if required values are missing. |
| `logging.py` | Configures structlog. Defines `correlation_id_ctx` ContextVar. Provides `configure_logging()` factory. Quiets noisy third-party loggers (uvicorn.access, sqlalchemy.engine). |

### `domain/`

| File | What It Does |
|---|---|
| `entities/payment.py` | Pure Python `@dataclass`. Enforces: amount > 0, currency = 3 chars, transaction_id not empty. Has `mark_success()` / `mark_failed()` state transitions with invariant checks. Has `PaymentEntity.create()` factory. **Zero external imports.** |
| `exceptions.py` | Defines `DomainException` base + 6 subclasses. These are the only exception types the application layer raises. Infrastructure exceptions are wrapped into domain exceptions at the boundary. |
| `interfaces/repository.py` | Abstract base classes: `AbstractRepository[T]` with `add/get/exists`. `AbstractPaymentRepository` specialised for `PaymentEntity`. **Zero external imports.** |

### `application/`

| File | What It Does |
|---|---|
| `uow.py` | `AbstractUnitOfWork` defines the contract (payments, outbox, gateway ports). `SqlAlchemyUnitOfWork` implements it: opens session, attaches repositories, commits/rolls back on exit. |
| `use_cases/create_payment.py` | The only business orchestration: charge gateway → mark entity success → persist payment + outbox event atomically. Raises `PaymentGatewayError` on gateway failure. **No ORM imports.** |

### `infrastructure/database/`

| File | What It Does |
|---|---|
| `session.py` | Creates the async SQLAlchemy engine + session factory. One import, two lines. |
| `models.py` | ORM table definitions: `PaymentModel` (payments), `OutboxEventModel` (outbox_events). |
| `repositories/payment.py` | `SqlAlchemyPaymentRepository`: implements the domain port. Maps `PaymentEntity ↔ PaymentModel`. |
| `repositories/outbox.py` | `SqlAlchemyOutboxRepository.enqueue()`: writes an `OutboxEventModel` row within the active session. |

### `infrastructure/cache/`

| File | What It Does |
|---|---|
| `redis.py` | `RedisClient`: `get`, `set` with TTL, `acquire_lock` (SET NX EX), `release_lock` (Lua CAS script). Used by idempotency middleware. |

### `infrastructure/messaging/`

| File | What It Does |
|---|---|
| `kafka_producer.py` | `KafkaProducerService`: holds an `AIOKafkaProducer` instance. `start()` / `stop()` called in lifespan. `send_event(topic, message)` publishes a JSON-serialized message. Logs via structlog. |

### `infrastructure/workers/`

| File | What It Does |
|---|---|
| `outbox_relay.py` | Infinite async loop. Polls `outbox_events` (WHERE processed=false, FOR UPDATE SKIP LOCKED, LIMIT 50), publishes each to Kafka, marks processed=true, commits. Logs per-event context via `bound_contextvars`. |

### `infrastructure/external/`

| File | What It Does |
|---|---|
| `gateway_client.py` | `PaymentGatewayClient`: `charge_with_fallback()` tries primary → 3 retries exponential backoff → fallback → 3 retries → raises `PaymentGatewayException`. Uses `httpx.AsyncClient` with timeouts. Logs via structlog. |

### `presentation/middleware/`

| File | What It Does |
|---|---|
| `correlation_id.py` | `CorrelationIdMiddleware`: reads/generates `X-Correlation-ID`, binds to request state + ContextVar + structlog, echoes in response, resets on exit. |
| `idempotency.py` | `IdempotencyMiddleware`: cache-check → distributed lock → call next → cache response → release lock. Returns 409 on concurrent duplicate. |
| `exception_handler.py` | `register_exception_handlers()`: wires 10 exception-class-specific handlers. All return `application/problem+json` with `type/title/status/detail/instance/correlation_id`. |
| `__init__.py` | Re-exports all three middleware components cleanly. |

### `presentation/api/`

| File | What It Does |
|---|---|
| `payments.py` | `POST /` route: validates `PaymentCreateRequest`, builds `PaymentEntity`, runs `CreatePaymentUseCase`, maps result to `PaymentResponse`, returns 201. |

### `alembic/`

| File | What It Does |
|---|---|
| `env.py` | Configures Alembic to use the project's models and database URL. |
| `versions/001_create_payments_table.py` | Creates `payments` table with all columns. |
| `versions/002_add_outbox_events_table.py` | Creates `outbox_events` table with a composite index on `(processed, created_at)` for fast relay queries. |

### Root Files

| File | What It Does |
|---|---|
| `main.py` | Application factory: configure logging → register exception handlers → attach middleware → mount routers → lifespan events. |
| `pyproject.toml` | Declares all 12 runtime dependencies with minimum version pins. |
| `.env.example` | Template for required environment variables. Copy to `.env` to run locally. |

---

## 6. The Full Request Lifecycle — End to End

```
Client sends:
  POST /api/v1/payments
  X-Idempotency-Key: txn-abc-001
  X-Correlation-ID: trace-xyz-789
  Body: { "transaction_id": "txn-abc-001", "amount": 2500.00, "currency": "INR" }

─────────────────────────────────────────────────────────────────────────────

MIDDLEWARE LAYER
  ① CorrelationIdMiddleware
     - Reads X-Correlation-ID: trace-xyz-789
     - Binds to request.state, ContextVar, and structlog
     - All subsequent logs carry: correlation_id=trace-xyz-789

  ② IdempotencyMiddleware
     - Checks Redis: idempotency:resp:txn-abc-001 → MISS
     - Acquires lock: idempotency:lock:txn-abc-001 → OK (15s TTL)

PRESENTATION LAYER
  ③ FastAPI route: POST /api/v1/payments
     - Pydantic validates body → PaymentCreateRequest OK
     - Constructs PaymentEntity(transaction_id="txn-abc-001", amount=2500.0, currency="INR")
     - __post_init__ enforces invariants → PASS

APPLICATION LAYER
  ④ CreatePaymentUseCase.execute()
     - Calls uow.gateway.charge_with_fallback({...})

INFRASTRUCTURE LAYER
  ⑤ PaymentGatewayClient
     - POST https://api.primary-acquirer.com/charge
     - Response: 200 { "reference_id": "gw_ref_123" }

  ⑥ Back in use case: payment_data.mark_success() → status = SUCCESS

  ⑦ async with SqlAlchemyUnitOfWork:
     - uow.payments.add(entity)
       → PaymentModel inserted (staged)
     - uow.outbox.enqueue(aggregate_type="Payment", event_type="payment.completed", ...)
       → OutboxEventModel inserted (staged)
     - __aexit__ → session.commit()
       → BOTH rows written atomically ✓

PRESENTATION LAYER
  ⑧ Route handler maps PaymentEntity → PaymentResponse
     - Returns: 201 Created
     {
       "transaction_id": "txn-abc-001",
       "amount": 2500.0,
       "currency": "INR",
       "status": "SUCCESS",
       "created_at": "2025-06-09T14:30:00Z"
     }

  ⑨ IdempotencyMiddleware caches the 201 response body in Redis (5 min TTL)
  ⑩ IdempotencyMiddleware releases the distributed lock (Lua script)
  ⑪ CorrelationIdMiddleware sets X-Correlation-ID: trace-xyz-789 on response
  ⑫ Response delivered to client

─────────────────────────────────────────────────────────────────────────────

BACKGROUND (2 seconds later)
  ⑬ poll_outbox_events() wakes up
     - SELECT outbox_events WHERE processed=false FOR UPDATE SKIP LOCKED
     - Finds the row for txn-abc-001
     - kafka_producer.send_and_wait("payment-completed", { ...payload... })
     - event.processed = True
     - session.commit()
     → Downstream services (e.g., notification service) consume from "payment-completed" topic

─────────────────────────────────────────────────────────────────────────────

IF CLIENT RETRIES with same X-Idempotency-Key: txn-abc-001
  ② IdempotencyMiddleware
     - Checks Redis: idempotency:resp:txn-abc-001 → HIT ✓
     - Returns cached 201 response immediately
     - Use case NEVER runs. Gateway NEVER charged. DB NEVER touched.
```

---

## 7. What "Zero Framework Imports in Domain" Actually Means

Run this check mentally on `domain/entities/payment.py`:

```python
from __future__ import annotations    # ✓ stdlib
import uuid                           # ✓ stdlib
from dataclasses import dataclass     # ✓ stdlib
from datetime import datetime         # ✓ stdlib
from enum import Enum                 # ✓ stdlib
```

That is the entire import list. No `pydantic`, no `sqlalchemy`, no `fastapi`, no `redis`, no `httpx`. 

This means:
- You can unit test `PaymentEntity` with zero infrastructure running
- You can run the domain layer in a Lambda function, a CLI script, or a different web framework with no changes
- The domain's correctness is provable in isolation

---

*Read this document alongside the code and you will understand not just what every line does, but why it exists and what disaster it prevents.*
