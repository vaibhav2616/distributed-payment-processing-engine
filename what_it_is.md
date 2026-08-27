# What It Is — Distributed Payment Processing Engine

## 1. What Is It?

This project is a **production-grade, asynchronous distributed payment processing engine** built in Python. It acts as a structural foundation for robust microservices.

Rather than being a simple CRUD app, it implements the heavy lifting required for financial systems:
- **Clean Architecture**: Separates domain logic from framework noise.
- **Transactional Outbox Pattern**: Ensures event-driven architectures don't lose messages during partial failures.
- **Idempotency**: Prevents double-charging users during network retries.
- **Strict Ledgering**: Uses double-entry accounting with exact Decimal precision.

## 2. Why Were These Architectural Choices Made?

### Clean Architecture vs. MVC (Django/Rails Style)
*Why not just put business logic in the web controllers or models?*
In standard MVC, the database ORM leaks into every part of the application. If you want to change the database, or even just write unit tests without spinning up PostgreSQL, it becomes incredibly difficult.
By using Clean Architecture, the `domain` layer has **zero imports from FastAPI or SQLAlchemy**. This allows us to test complex financial rules (like the zero-sum ledger invariant) in milliseconds without any external dependencies.

### Transactional Outbox vs. Direct Kafka Publishing
*Why not just call `kafka.send()` after saving to the database?*
If you save a payment to the database and then call `kafka.send()`, the network call to Kafka might fail. If it fails, your database has recorded a successful payment, but downstream services (like fulfillment or notification) never find out.
The **Transactional Outbox** pattern saves the payment AND the event to the database in a single atomic transaction. A background relay then reliably delivers the event to Kafka. If Kafka goes down, the relay just retries later. No data is lost.

### Canonical JSON Hashing for Idempotency
*Why hash the payload? Why not just use the Idempotency-Key header?*
If a client sends a request with `Idempotency-Key: 123` to charge $10, and then maliciously retries with `Idempotency-Key: 123` to charge $1000, a naive system might just return the cached $10 success response.
We hash the JSON body (canonicalized to ignore whitespace and key order) and store it with the lock. If the hash changes for the same key, we reject it with a `409 Conflict`.

### Decimal vs. Float
*Why not use floats for money?*
Floats in Python (and hardware) are approximations (`0.1 + 0.2 = 0.30000000000000004`). In a payment system, this leads to missing pennies and failed audits. We strictly use `Decimal` at the domain layer, and stringified decimals in JSON, to guarantee precision.
