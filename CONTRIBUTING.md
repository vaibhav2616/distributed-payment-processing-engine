# Contributing to Distributed Payment Processing Engine

## Architecture Invariants (Non-Negotiable)

Before submitting a pull request, verify that these structural rules are strictly maintained:

### 1. Layer Isolation
* `domain/` **must never** import from `infrastructure/`, `application/`, or `presentation/`. It must remain 100% pure Python standard library.
* `application/` **must never** import from `infrastructure/` directly; all persistence interactions must traverse abstract Unit of Work ports (`application/uow.py`).
* `presentation/` **must never** import repository implementations or database models directly.

### 2. Financial & Data Correctness
* **No Floats**: Monetary values must always use `decimal.Decimal` in Python and `NUMERIC(18, 4)` in PostgreSQL. Never instantiate a `float` for monetary amounts.
* **Balanced Ledgers**: Every `LedgerTransaction` must be built through `LedgerTransaction.build()` and prove $\sum \text{Entries} = 0.0000$ before submission to the database.
* **Transactional Outbox**: All domain event emissions must be written to `outbox_events` inside the same database transaction as the aggregate mutation.

### 3. Code Standards
* All new domain entities must be pure Python `@dataclass` objects.
* All new use cases must raise domain exceptions (`domain/exceptions.py`) only.
* All new HTTP routes must declare explicit `response_model=` and `status_code=`.
* All new log statements must use `structlog.get_logger(__name__)` and avoid logging sensitive card/PCI-DSS fields.

---

## Local Development Workflow

### Setup & Testing
This project uses `uv` for high-speed, deterministic dependency management.

```bash
# 1. Install dependencies
uv sync

# 2. Run the complete test suite (107 unit & integration tests)
uv run pytest -v

# 3. Boot the local production topology (PostgreSQL, Redis, Redpanda, API, Workers)
docker compose up --build -d

# 4. Check cluster health
docker compose ps

# 5. Execute the Idempotency Stampede chaos load test
uv run locust -f tests/load/locustfile.py --headless -u 1 -r 1 -t 10s --host http://localhost:8000
```

---

## Commit Convention

Follow [Conventional Commits](https://www.conventionalcommits.org/):
* `feat(scope): description`
* `fix(scope): description`
* `refactor(scope): description`
* `chore: description`
* `docs: description`
