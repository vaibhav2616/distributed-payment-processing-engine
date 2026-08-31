# What It Is — Distributed Payment Processing Engine

## 1. System Overview

The **Distributed Payment Processing Engine** is an enterprise-grade, asynchronous financial transaction backend built with Python 3.12, FastAPI, PostgreSQL, Redis, and Redpanda (Kafka).

Rather than serving as a basic CRUD prototype, this system is an industrial reference implementation designed to solve the hardest distributed systems and concurrency problems encountered in mission-critical financial infrastructure:

* **Clean Architecture & Domain-Driven Design (DDD)**: Total isolation of financial domain rules and state machine logic from database, web, and message broker frameworks.
* **Double-Entry Ledger Accounting**: Every state mutation is mathematically proven balanced ($\sum \text{Debits} + \sum \text{Credits} = 0$) prior to persistence, backed by append-only database immutability.
* **Distributed Idempotency & Edge Defense**: Edge-level canonical JSON hashing and Redis-backed distributed locks (`SET NX EX`) that intercept concurrent duplicate requests and prevent double-charging.
* **3-Phase Lock-Free Orchestration Sagas**: Decoupling database connection lifetimes from third-party network latency, eliminating connection pool starvation during upstream acquirer degradation.
* **Distributed Circuit Breaking**: Multi-pod Redis circuit breaker (`CLOSED`, `OPEN`, `HALF-OPEN`) with canary probes, protecting external gateway connections from cascading collapse.
* **Asynchronous Webhook Processing with Split-Brain Mitigation**: Cryptographic dynamic HMAC authentication, replay attack tolerance windows, and `SELECT FOR UPDATE NOWAIT` locking to safely resolve out-of-order gateway callbacks.
* **Refund Orchestration & Compensating Transactions**: Finite state machine management across partial and full refunds with automated compensating sagas to restore balances on gateway rejection.
* **Transactional Outbox & Poison-Pill DLQ**: Elimination of the Dual-Write Problem via database-level outbox atomicity, combined with an asynchronous relay worker that isolates corrupted messages in a Dead Letter Queue.
* **Local Concurrency Harness & Chaos Verification**: A 6-container production-like topology running on Docker Compose, validated under empirical 10-way idempotency stampede load tests via Locust.

---

## 2. Architectural Choices & Trade-off Analysis

Every architectural decision in this engine was selected to address specific operational failure modes. Below is the technical rationale comparing each choice against common alternatives.

---

### 2.1 Clean Architecture vs. Traditional MVC (ActiveRecord / Django Style)

* **The Alternative:** Placing business logic directly in database models or web controllers (ActiveRecord pattern).
* **The Failure Mode:** In standard MVC, database abstractions leak across all system tiers. Unit testing requires spinning up live database instances or brittle mock frameworks. Upgrading database schemas or swapping storage adapters requires invasive refactoring across business logic.
* **Why We Chose Clean Architecture:**
  The `domain/` layer contains **zero framework dependencies** (no FastAPI, no SQLAlchemy, no Pydantic, no Redis). Domain models (`PaymentAggregate`, `LedgerTransaction`, `Money`) are pure Python data structures with strictly self-contained invariants. Complex business rules—such as ledger balance checks and state machine transitions—execute and test in sub-millisecond memory contexts without I/O dependencies.

---

### 2.2 Exact Fixed-Point Arithmetic (`Decimal` & `NUMERIC(18,4)`) vs. Floating-Point (`float`)

* **The Alternative:** Storing currency amounts as binary floating-point numbers (`float` / `DOUBLE PRECISION`).
* **The Failure Mode:** IEEE 754 floating-point arithmetic represents base-10 fractions inexactly in binary (e.g., `0.10 + 0.20 = 0.30000000000000004`). In high-volume settlement pipelines, fractional cent errors accumulate, causing ledger balancing mismatches and audit failures.
* **Why We Chose Fixed-Point Decimal:**
  Monetary values are strictly typed as Python standard library `Decimal` in memory and PostgreSQL `NUMERIC(18, 4)` on disk. Floating-point inputs are actively rejected at the API ingress schema with validation errors, and any attempt to pass a `float` into domain entities raises an immediate runtime `TypeError`.

---

### 2.3 Transactional Outbox vs. Direct Broker Publishing (`kafka.send()`)

* **The Alternative:** Calling `kafka.send()` directly inside the HTTP request handler after committing a database transaction.
* **The Failure Mode (The Dual-Write Problem):** If the database commit succeeds but the message broker is unreachable or network connectivity drops, the event is permanently lost, leaving downstream systems (billing, notifications, analytics) out of sync. Conversely, if the event publishes first and the database transaction fails, phantom events are processed downstream. Two-Phase Commit (2PC) across relational databases and Kafka is notoriously brittle and slow.
* **Why We Chose Transactional Outbox:**
  Domain events are written to the `outbox_events` table **within the exact same local ACID transaction** as the payment aggregate and ledger entries. The database commit guarantees atomicity: either both data and events persist, or neither does. A dedicated asynchronous background worker (`OutboxRelayWorker`) polls the table and handles broker transmission, decoupling client response latency from broker availability.

---

### 2.4 Canonical JSON Hashing vs. Bare Idempotency Keys

* **The Alternative:** Using only the raw `Idempotency-Key` header without inspecting the request body.
* **The Failure Mode (Payload Mutation Exploits):** If an attacker submits a charge for $10 with key `abc-123`, and subsequently submits a charge for $1,000 using that same key `abc-123`, a naive system returns the cached $10 response while potentially executing an invalid state transition or concealing an unauthorized operation.
* **Why We Chose Canonical Hashing:**
  The middleware normalizes the incoming JSON body (sorting keys and stripping non-semantic whitespace) and computes a SHA-256 digest stored alongside the Redis lock. If a request arrives with an existing idempotency key but an altered payload hash, the engine rejects it immediately with `409 Conflict`.

---

### 2.5 3-Phase Lock-Free Saga vs. Long-Lived Database Transactions

* **The Alternative:** Opening an ACID transaction, executing the HTTP call to the external gateway, and committing the transaction upon receiving the gateway response.
* **The Failure Mode (Connection Pool Starvation):** If third-party gateway latency degrades from 200ms to 5,000ms, database connections remain open during the entire network wait. Under concurrent traffic, the database connection pool is exhausted in seconds, causing application-wide downtime.
* **Why We Chose the 3-Phase Lock-Free Pattern:**
  Phase 1 creates the `PENDING` record in a rapid ~2ms transaction and commits. Phase 2 executes the HTTP gateway call with **zero database connections or row locks held**. Phase 3 acquires a fresh connection to record the terminal state (`CAPTURED` or `FAILED`). If the gateway times out, the system safely returns `202 Accepted` and delegates resolution to the `BackgroundReconciliationEngine`.

---

### 2.6 Distributed Redis Circuit Breaker vs. In-Memory Local Breakers

* **The Alternative:** In-memory circuit breakers running independently inside each API pod.
* **The Failure Mode:** In a horizontally scaled cluster of 20 pods, failures are distributed across nodes. An upstream gateway outage requiring 5 consecutive failures could require up to 100 failed customer transactions before individual pods trip their local breakers, prolonging cascading failures.
* **Why We Chose Distributed Redis Circuit Breaking:**
  State (`CLOSED`, `OPEN`, `HALF-OPEN`) and consecutive failure counters are coordinated globally in Redis. When 5 consecutive upstream timeouts or 5xx errors occur cluster-wide, all pods instantly trip to `OPEN`, immediately rejecting outbound requests with `503 Service Unavailable` (`Retry-After: 60`). Recovery uses atomic Lua scripts to permit exactly one canary probe in `HALF-OPEN` mode.

---

### 2.7 Asynchronous Webhooks with `NOWAIT` vs. Blocking Row Locks

* **The Alternative:** Webhook receivers that wait synchronously for locks (`SELECT FOR UPDATE`) when encountering concurrent updates.
* **The Failure Mode:** If the reconciler or orchestrator is currently updating a row, a blocking webhook consumer ties up an API thread waiting for the lock, risking deadlock and resource exhaustion.
* **Why We Chose `SELECT FOR UPDATE NOWAIT`:**
  When a row is locked by an in-flight operation, `NOWAIT` immediately aborts the query and raises `ConcurrentUpdateException`, mapped to `409 Conflict`. Payment gateways (Stripe, Adyen, Razorpay) are explicitly built to treat 409/429/5xx responses as transient contention and apply exponential backoff. This offloads concurrency queue management to the gateway's webhook infrastructure while keeping internal worker pools responsive.

---

### 2.8 Compensating Refund Sagas vs. Immediate Irreversible Updates

* **The Alternative:** Deducting funds and marking payments refunded before confirming the gateway outcome, or relying on complex two-phase commit protocols.
* **The Failure Mode:** If the external gateway declines a refund request (e.g., customer card canceled, acquirer settlement closed), an optimistic local update leaves the database in an inconsistent state with locked funds that were never reversed upstream.
* **Why We Chose Compensating Sagas:**
  The `RefundOrchestrator` reserves the refund amount locally in Phase 1, invokes the gateway in Phase 2, and if a `GatewayDeclineException` occurs, executes an automated compensating transaction in Phase 3 (`payment.fail_refund(amount)`). The reserved amount is credited back to the balance and the aggregate status reverts to `CAPTURED`, preserving financial consistency without manual support intervention.
