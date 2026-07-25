# What It Is — Payment Chassis: An Architectural Identity Document

> *Written so you can read this once and explain every design decision fluently to any engineer, interviewer, or stakeholder.*

---

## 1. What Is It?

**Payment Chassis** is a **production-grade, asynchronous backend infrastructure boilerplate** built in Python. It is not a finished product — it is a **chassis**: a pre-engineered structural frame onto which real microservices can be bolted.

Think of it the way automotive engineers think of a vehicle platform. BMW builds the 3 Series, 5 Series, and X5 on variations of the same chassis. The chassis handles steering, suspension, and safety — the product team handles the bodywork and features. This project does the same for software: it handles all the hard, cross-cutting, "boring-but-critical" infrastructure concerns — so any future team building a payments microservice, an orders service, or a notifications service only has to write **business logic**, not boilerplate plumbing.

In concrete technical terms, it is a **FastAPI application** that demonstrates how to correctly implement:

- **Clean Architecture** (strict layer separation)
- **Domain-Driven Design** (domain purity, entities, ports)
- **Transactional Outbox Pattern** (guaranteed event publishing)
- **Idempotency** (safe retry semantics for payment APIs)
- **Distributed tracing** (correlation IDs across all async boundaries)
- **Structured logging** (machine-parseable JSON telemetry)
- **RFC 7807 error standards** (uniform error responses)

---

## 2. Who Built It, and Who Is It For?

### Who built it?
This was engineered as a solo principal-level architecture exercise — applying enterprise HLD/LLD standards that are normally only seen inside large engineering organizations (Stripe, Razorpay, Google Pay, Swiggy, etc.).

### Who is it for?

| Audience | Why they care |
|---|---|
| **You (the builder)** | Reference it whenever starting a new microservice — copy the structure, fill in the domain logic |
| **Engineering interviewers** | Demonstrates mastery of distributed systems, DDD, async Python, and production reliability patterns |
| **Future teammates** | A new engineer can onboard in hours because every folder has a clear contract |
| **Architects reviewing designs** | It speaks the same language as HLD/LLD documents at scale companies |

---

## 3. Why Does It Exist? (The Problem It Solves)

Most tutorials and starter templates give you a FastAPI app that looks like this:

```python
@app.post("/pay")
async def pay(data: dict):
    db.execute(...)          # ORM leaking into route
    requests.post(...)       # sync HTTP call blocking the event loop
    kafka.send(...)          # hope this doesn't fail
    return {"ok": True}     # what if the DB committed but Kafka failed?
```

This pattern fails silently in production in at least **six critical ways**:

1. **Dual-write problem**: DB commits but the Kafka event never fires → downstream services are out of sync forever.
2. **No idempotency**: The client retries → you charge the customer twice.
3. **No correlation**: A bug appears in logs — you have no way to trace which request caused it across 5 services.
4. **ORM in routes**: Business logic becomes inseparable from HTTP concerns — untestable, unswappable.
5. **No standard errors**: Every route returns a different error shape — API consumers can't handle errors programmatically.
6. **Fragile gateway calls**: One network blip = 500 error with no retry, no fallback.

**Payment Chassis solves all six**, cleanly and permanently, as architectural defaults — not as afterthoughts.

---

## 4. When Is It Used?

### When you start a new microservice
Copy the `domain/`, `application/`, `infrastructure/`, and `presentation/` directories. Delete the payment-specific files. Add your own entities and use cases. The middleware, logging, error handling, Kafka, Redis, and database plumbing are already wired and production-ready.

### When preparing for system design interviews
This codebase is a **live, runnable answer** to: *"Design a scalable, reliable payments microservice."* Every pattern here maps directly to questions asked at senior/staff engineer interviews.

### When onboarding a team to Clean Architecture
Show engineers this codebase as the canonical reference. The rules are enforced structurally — you *cannot* import SQLAlchemy into the domain layer without violating the architecture's own import contract.

### When building anything that requires at-least-once event delivery
The Transactional Outbox pattern implemented here is the same pattern used by Uber, Shopify, and every serious distributed system team for guaranteed event publishing.

---

## 5. Where Does It Live? (Structural Identity)

```
infrastructure_boilerplate/
│
├── core/                    ← Cross-cutting concerns (config, logging)
├── domain/                  ← Pure business rules — NO framework code here
│   ├── entities/            ← Aggregate roots (PaymentEntity)
│   ├── interfaces/          ← Abstract ports (AbstractPaymentRepository)
│   └── exceptions.py        ← Business error taxonomy
│
├── application/             ← Orchestration — USE CASES live here
│   ├── uow.py               ← Unit of Work (transaction boundary)
│   └── use_cases/           ← One file per business workflow
│
├── infrastructure/          ← All external I/O adapters
│   ├── database/            ← SQLAlchemy models, session, repo implementations
│   ├── cache/               ← Redis client
│   ├── messaging/           ← Kafka producer
│   ├── workers/             ← Outbox relay background task
│   └── external/            ← Payment gateway HTTP client
│
├── presentation/            ← HTTP surface: routers + middleware
│   ├── api/                 ← FastAPI routers (versioned)
│   └── middleware/          ← Correlation ID, Idempotency, Error handling
│
├── alembic/                 ← Database schema migrations
├── main.py                  ← Application factory (wires everything)
└── pyproject.toml           ← Dependency manifest
```

Every directory is a **bounded context with explicit rules**. The domain never imports from infrastructure. The application never imports from presentation. Violations are visible immediately as import errors.

---

## 6. How Is It Architected? (Design Philosophy)

### 6.1 Clean Architecture (The Dependency Rule)

The single most important rule in this codebase:

> **Dependencies only point inward. Outer layers know about inner layers. Inner layers know nothing about outer layers.**

```
  [Presentation]
       ↓ imports
  [Application]
       ↓ imports
    [Domain]       ← knows nothing about anyone else
       ↑
  [Infrastructure] ← implements domain interfaces, plugged in via DI
```

The `domain/` directory can be copy-pasted into any project and will compile with zero changes — it has no external dependencies.

### 6.2 Domain-Driven Design (DDD) Patterns Applied

| DDD Concept | Where In This Project |
|---|---|
| **Entity / Aggregate Root** | `domain/entities/payment.py` — `PaymentEntity` dataclass with invariants |
| **Value Objects** | `PaymentStatus` enum — immutable, behaviour-carrying |
| **Repository Port** | `domain/interfaces/repository.py` — abstract contract only |
| **Repository Adapter** | `infrastructure/database/repositories/payment.py` — SQLAlchemy impl |
| **Use Case / Application Service** | `application/use_cases/create_payment.py` — orchestration only |
| **Domain Events** | Outbox events (`payment.completed`) written atomically in the same UoW |
| **Anti-Corruption Layer** | Gateway client isolates the domain from third-party HTTP response shapes |

### 6.3 Twelve-Factor App Alignment

| Factor | Implementation |
|---|---|
| Config via environment | `pydantic-settings` reads `.env`, validates types at boot |
| Stateless processes | No local state — Redis for distributed state, PostgreSQL for persistence |
| Logs as event streams | `structlog` emits JSON to stdout — collected by any log aggregator |
| Disposability | Kafka producer started/stopped cleanly in lifespan events |
| Backing services as attached resources | DB, Redis, Kafka URLs all injected via config |

### 6.4 What Makes It "Enterprise-Grade"

The term is overloaded. In this project, it means very specifically:

1. **Zero data loss guarantees** — the Transactional Outbox pattern means an event is either published exactly once or retried safely. There is no scenario where money moves without a corresponding audit event.

2. **Safe retries by design** — idempotency is not a feature bolted on later. It is a middleware that intercepts every POST request before it reaches business logic.

3. **Full observability** — every log line in the entire system (HTTP handler, Redis lock, Kafka publish, outbox relay) carries the same `correlation_id`. You can trace a single payment request across every system component in a single log query.

4. **Uniform error contracts** — every error, from a validation failure to a gateway timeout, returns the same RFC 7807 JSON shape. Clients never need conditional parsing logic.

5. **Infrastructure is replaceable** — because the domain defines abstract ports (`AbstractPaymentRepository`) and the application layer only speaks to those ports, you can swap PostgreSQL for DynamoDB, Kafka for RabbitMQ, or Redis for Memcached by writing a new adapter. No business logic changes.

---

## 7. Key Technologies and Why Each Was Chosen

| Technology | Role | Why This, Not That |
|---|---|---|
| **FastAPI** | HTTP framework | Native async, automatic OpenAPI docs, Pydantic integration |
| **SQLAlchemy 2.0 (async)** | ORM + persistence | Mature, type-safe, supports async sessions cleanly |
| **Alembic** | Schema migrations | The standard alongside SQLAlchemy — declarative, reversible |
| **asyncpg** | PostgreSQL driver | The fastest async Postgres driver in Python |
| **pydantic-settings** | Config management | Type-validated env vars, `.env` support, fail-fast at startup |
| **structlog** | Structured logging | JSON output, context binding, plays nicely with stdlib logging |
| **redis-py (async)** | Distributed locking + cache | Lua scripting for atomic lock operations |
| **AIOKafka** | Event streaming | Native asyncio Kafka client, no thread pool overhead |
| **httpx** | External HTTP client | Async-native, supports timeouts, replaces requests |
| **tenacity** | Retry / fallback logic | Decorator-based, exponential backoff, clean separation from business code |

---

## 8. The Five Promises This Chassis Keeps

1. **A payment will never be charged twice** — idempotency middleware + distributed lock.
2. **A payment event will never be lost** — transactional outbox + at-least-once Kafka delivery.
3. **Every error will be diagnosable** — correlation IDs on every log line across every layer.
4. **Every API error will be parseable** — RFC 7807 `application/problem+json` always.
5. **New features will never pollute existing contracts** — domain isolation enforced structurally.

---

*This document is your architectural passport. Read it before any technical discussion, interview, or code review involving this project.*
