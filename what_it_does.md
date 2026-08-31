# What It Does — Distributed Payment Processing Engine

## End-to-End Operational Lifecycle

The **Distributed Payment Processing Engine** safely processes financial transactions through a multi-stage pipeline designed for fault tolerance, data integrity, and high-concurrency protection. Below is the technical breakdown of each phase in the system's runtime execution.

---

### 1. Ingress & Edge Idempotency Defense (`POST /api/v1/payments`)

When an HTTP client initiates a charge attempt:

1. **Trace Injection**: `TraceIdMiddleware` extracts or generates an `X-Trace-Id`, binding it to asynchronous context variables across all downstream loggers, repository operations, and message payloads.
2. **Canonical Hashing**: `IdempotencyMiddleware` intercepts the request. The request body is parsed and canonicalized (keys sorted, non-semantic whitespace eliminated) and hashed via SHA-256.
3. **Cache Lookup**: Redis is checked for an existing response key (`idempotency:resp:<key>`). If found, the cached response is returned immediately with `X-Cache: HIT`, bypassing application and database layers entirely.
4. **Distributed Lock Acquisition**: If no cache entry exists, the middleware executes an atomic Redis lock acquisition:
   ```text
   SET idempotency:lock:<key> <token> NX EX 15
   ```
   * If the lock fails (another clone is actively in-flight): The middleware immediately returns `HTTP 409 Conflict` (`X-Idempotency-Status: LOCKED`), shielding the database from concurrency stampedes.
   * If the lock succeeds: The request proceeds into the application layer.

---

### 2. 3-Phase Payment Orchestration Saga

The `PaymentOrchestrator` (`application/use_cases/payment_orchestrator.py`) executes a 3-phase saga pattern that decouples database connection lifetimes from third-party network latency:

* **Phase 1: Local State Initialization ($\approx 2\text{ms}$)**
  * Opens a database transaction via `SqlAlchemyUnitOfWork`.
  * Creates and validates a `PaymentAggregate` in state `PENDING`.
  * Enqueues a `payment.created` event into the `outbox_events` table.
  * Commits the transaction and **immediately releases the database connection back to the connection pool**.
* **Phase 2: Lock-Free Network Call**
  * The orchestrator calls `PaymentGatewayClient.charge()` over HTTP.
  * **Zero database connections and zero row locks are held during this network wait.** Even if the gateway stalls for 5,000ms, database pool capacity is completely unaffected.
* **Phase 3: Local State Finalization ($\approx 2\text{ms}$)**
  * A fresh Unit of Work acquires a new connection from the pool.
  * The payment row is retrieved using `SELECT ... FOR UPDATE NOWAIT`.
  * **On Success**: Transitions state to `CAPTURED`. Constructs a mathematically verified zero-sum `LedgerTransaction` (debit `1100.ACCOUNTS_RECEIVABLE`, credit `2100.GATEWAY_PAYABLE`). Enqueues `payment.captured` in the outbox. Commits and returns `201 Created`.
  * **On Hard Decline**: Transitions state to `FAILED`. Enqueues `payment.failed` in the outbox. Commits and returns `422 Unprocessable Entity`.
  * **On Timeout / Network Disconnect**: The local database is left in state `PENDING`. Returns `202 Accepted` (`reconciler_needed=True`), handing off recovery to the background reconciler.

---

### 3. Distributed Circuit Breaking

All outbound gateway communication is wrapped by `RedisCircuitBreaker`:

* **Normal Operation (`CLOSED`)**: Gateway calls execute normally.
* **Failure Tripping (`OPEN`)**: If 5 consecutive gateway calls experience network timeouts or 5xx server errors, the breaker transitions to `OPEN` with a **60-second Redis TTL**.
* **Fast Failure**: During the `OPEN` window, any outbound attempt immediately raises `CircuitBreakerOpenException` without touching the network. The API router returns `503 Service Unavailable` with a `Retry-After: 60` header.
* **Canary Testing (`HALF-OPEN`)**: After the 60-second cooldown expires, an atomic Lua CAS script allows exactly **one probe request** through.
  * If the canary succeeds: The breaker resets to `CLOSED`.
  * If the canary fails: The breaker returns to `OPEN` for another 60-second period.
* **Domain Error Segregation**: HTTP 4xx responses (card declined, insufficient funds) are valid domain outcomes and **never** increment the circuit breaker failure counter.

---

### 4. Background Reconciliation Engine

The `BackgroundReconciliationEngine` (`application/workers/reconciler.py`) resolves "zombie" `PENDING` payments resulting from gateway timeouts or pod crashes:

1. **Phase 1 (Unlocked ID Sweep)**: Queries `payments` for IDs with `status = 'PENDING'` older than 300 seconds (configurable). The connection is returned to the pool immediately.
2. **Phase 2 (Lock-Free Verification)**: For each ID, the worker queries `gateway.verify_status(payment_id)` over HTTP with zero database connections held.
3. **Phase 3 (Just-in-Time Finalization)**: Opens a short-lived transaction, locks the specific row with `SELECT FOR UPDATE NOWAIT`, and drives it to `CAPTURED` (generating ledger lines) or `FAILED`. If another worker holds the row, it safely skips and retries on the next iteration.

---

### 5. Asynchronous Webhook Receiver & Split-Brain Mitigation

External payment gateways deliver asynchronous settlement updates via `POST /api/v1/webhooks`:

1. **Replay Attack Defense**: `verify_webhook_signature` parses `X-Gateway-Timestamp`. If the timestamp deviates by $> 300$ seconds from the server's current clock, the request is rejected with `401 Unauthorized`.
2. **Dynamic HMAC Verification**: The signature is cryptographically verified against `f"{timestamp}.{raw_body.decode()}"` using constant-time `hmac.compare_digest`.
3. **Concurrency Control (`NOWAIT`)**: The receiver executes `SELECT ... FOR UPDATE NOWAIT` on the payment record. If the row is currently locked by the reconciler or orchestrator, it raises `ConcurrentUpdateException` and returns `409 Conflict`. This signals the gateway to back off and retry later using its exponential backoff queue.
4. **Split-Brain State Collisions**:
   * If the payment is already resolved and matches the incoming status: Safely returns `200 OK` (true idempotency).
   * If the payment is already resolved but differs from the incoming status (e.g., database says `FAILED` but webhook says `CAPTURED`): Rolls back, dispatches a `CRITICAL` telemetry security alert, and returns `200 OK` to stop gateway retries while flagging the record for manual audit.

---

### 6. Refund Orchestration & Compensating Sagas

Refund operations (`POST /api/v1/refunds`) execute a multi-phase saga supporting both partial and full reversals:

1. **Domain Invariants**: The `PaymentAggregate` enforces that refunds are only valid on `CAPTURED` or `PARTIALLY_REFUNDED` payments. If `amount_refunded + refund_amount > amount_captured`, the aggregate rejects the operation with `InvalidRefundAmountError` (`422 Unprocessable Entity`).
2. **Local Reservation (Phase 1)**: The orchestrator reserves the refund amount on the payment aggregate, transitions the status to `PARTIALLY_REFUNDED` or `REFUNDED`, and commits.
3. **Gateway Reversal (Phase 2)**: Calls the gateway's refund endpoint over HTTP.
4. **Compensating Rollback (Phase 3)**: If the gateway rejects the refund (`GatewayDeclineException`), the orchestrator opens a fresh transaction, executes `payment.fail_refund(amount)` (restoring the reserved funds back to available balance), enqueues a `refund.failed` outbox event, and commits, restoring balance consistency automatically.

---

### 7. Transactional Outbox Streaming & Poison-Pill DLQ

All downstream event notifications stream reliably through the `OutboxRelayWorker` (`application/workers/outbox_relay.py`):

1. **Atomic Ingestion**: Whenever domain state mutates, the corresponding event is saved to `outbox_events` inside the same database transaction.
2. **Horizontal Polling**: Workers fetch pending batches using `SELECT ... FOR UPDATE SKIP LOCKED`, allowing multiple worker pods to process non-overlapping queues in parallel without deadlocks.
3. **Kafka/Redpanda Dispatch**: Events are published to their designated topic.
4. **Poison-Pill Quarantine**: If an event permanently fails serialization or exceeds `MAX_RETRIES = 5`, the worker marks `event.status = 'DLQ'` and logs a `CRITICAL` alert. The worker continues streaming subsequent events, preventing head-of-line blocking.

---

### 8. Containerized Production Topology & Concurrency Testing

The system deploys as a decoupled cluster defined in [docker-compose.yml](file:///home/nitrov/distributed-payment-processing-engine/docker-compose.yml):

* **Stateful Infrastructure**: PostgreSQL 16 (`pgdata`), Redis 7 Alpine (`redisdata`), Redpanda Kafka (`redpandadata`).
* **Stateless Application Services**:
  * `api`: FastAPI HTTP server running under Uvicorn with auto-applied migrations.
  * `worker-reconciler`: Background reconciler sweeping stale pending payments.
  * `worker-outbox`: Outbox relay streaming events to Redpanda.
* **Chaos Concurrency Harness**: Validated using [tests/load/locustfile.py](file:///home/nitrov/distributed-payment-processing-engine/tests/load/locustfile.py), triggering a 10-way simultaneous Idempotency Stampede that empirically proves:
  * Exactly 1 request executes the orchestrator.
  * Exactly 9 requests are intercepted by the Redis distributed lock (`409 Conflict`) or cache.
  * Zero 500 errors occur and database connection pools are never exhausted.
