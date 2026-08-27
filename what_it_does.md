# What It Does — Distributed Payment Processing Engine

## The Core Flow

At its heart, this system takes payment requests from clients and safely orchestrates them through an external payment gateway, internal ledgers, and downstream event consumers.

### 1. The HTTP Request Phase
When a client sends a `POST /api/v1/payments` request, the **Idempotency Middleware** intercepts it.
- **What it does:** Checks Redis for the `X-Idempotency-Key` and hashes the JSON body to verify the payload hasn't mutated.
- **Why we chose this:** Alternative approaches simply rely on database unique constraints, which require a full database round-trip and transaction overhead to reject a duplicate. Redis allows us to drop duplicates in milliseconds at the edge.

### 2. The Orchestration Phase
The request is routed to the `PaymentOrchestrator`, which initiates a 3-phase commit process:
- **Phase 1: Local State:** Opens a database transaction, saves the payment as `PENDING`, and appends an outbox event.
- **Phase 2: Network Call:** Calls the external payment gateway.
- **Phase 3: Final State:** If the gateway succeeds, records the `CAPTURED` state, writes the double-entry ledger lines, and commits.
- **Why we chose this:** The alternative is distributed transactions (Two-Phase Commit / 2PC). 2PC is notoriously slow and causes heavy locking. Our 3-phase saga pattern avoids locking external systems while providing high throughput.

### 3. The Asynchronous Reconciliation Phase
If the payment gateway times out during Phase 2, the HTTP request returns `202 Accepted` (Pending).
- **What it does:** A background `BackgroundReconciliationEngine` sweeps the database for stale `PENDING` payments and verifies their status with the gateway directly.
- **Why we chose this:** Without a background reconciler, transient network errors result in "zombie" payments that require manual engineering intervention to fix. By using `SELECT FOR UPDATE SKIP LOCKED`, multiple pods can reconcile zombies concurrently without stepping on each other.

### 4. The Outbox Relay Phase
Once the payment state is committed, downstream systems (like inventory or email) need to know.
- **What it does:** The `OutboxRelayWorker` polls the `outbox_events` table and pushes events to Kafka. It employs a poison-pill circuit breaker that marks un-publishable events as Dead Letter Queue (DLQ) to prevent blocking the queue.
- **Why we chose this:** Kafka streams provide guaranteed delivery to an arbitrary number of consumers, compared to direct HTTP webhooks which are difficult to retry and monitor at scale. The outbox pattern bridges the gap between atomic SQL transactions and eventual Kafka delivery.
