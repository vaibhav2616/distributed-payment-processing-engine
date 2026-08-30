# Contributing to Distributed Payment Processing Engine

## Architecture Rules

Before submitting a pull request, ensure these rules are not violated:

### Layer Isolation (non-negotiable)
- `domain/` **must never** import from `infrastructure/`, `application/`, or `presentation/`
- `application/` **must never** import from `infrastructure/` directly (only via UoW ports)
- `presentation/` **must never** import from `infrastructure/` directly

### Code Standards
- All new domain entities must be pure Python `@dataclass` objects
- All new use cases must raise domain exceptions (`domain/exceptions.py`) only
- All new routes must have `response_model=` and explicit `status_code=`
- All new log calls must use `structlog.get_logger(__name__)`

### Commit Convention
Follow [Conventional Commits](https://www.conventionalcommits.org/):
- `feat(scope): description`
- `fix(scope): description`
- `refactor(scope): description`
- `chore: description`
- `docs: description`
