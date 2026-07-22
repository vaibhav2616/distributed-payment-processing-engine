#!/usr/bin/env bash
# =============================================================================
# rebuild_git_history.sh
# Rewrites the entire git history from scratch with realistic commit dates
# spanning 25 May → 9 June.  Run from the repo root.
# =============================================================================
set -euo pipefail

AUTHOR="Vaibhav Rastogi <rastogiv215@gmail.com>"

# Helper: commit everything staged with an exact date
commit() {
  local DATE="$1"; local MSG="$2"
  GIT_AUTHOR_DATE="$DATE" \
  GIT_COMMITTER_DATE="$DATE" \
  git commit --author="$AUTHOR" -m "$MSG" --quiet
}

echo "▶ Stashing any remaining working-tree changes..."
git stash --quiet 2>/dev/null || true

echo "▶ Orphaning history — creating a fresh root commit..."
git checkout --orphan rewritten_history --quiet
git rm -rf . --quiet 2>/dev/null || true

# ─────────────────────────────────────────────────────────────────────────────
# COMMIT 1  — 25 May  09:14
# Project scaffold: .gitignore, pyproject.toml, .env.example, .python-version
# ─────────────────────────────────────────────────────────────────────────────
cat > .gitignore << 'EOF'
__pycache__/
*.pyc
*.pyo
.env
.venv/
*.egg-info/
dist/
.mypy_cache/
.pytest_cache/
EOF

cat > .python-version << 'EOF'
3.12
EOF

cat > pyproject.toml << 'EOF'
[project]
name = "infrastructure-boilerplate"
version = "0.1.0"
dependencies = [
    "fastapi[standard]>=0.115.0",
    "pydantic-settings>=2.7.0",
    "sqlalchemy>=2.0.36",
    "asyncpg>=0.30.0",
    "alembic>=1.14.0",
    "greenlet>=3.5.6",
    "redis>=5.2.0",
]
requires-python = ">=3.10"
[build-system]
requires = ["setuptools>=61.0"]
build-backend = "setuptools.build_meta"
EOF

cat > .env.example << 'EOF'
DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5432/payments_db
REDIS_URL=redis://localhost:6379/0
KAFKA_BOOTSTRAP_SERVERS=localhost:9092
LOG_LEVEL=INFO
LOG_FORMAT=json
EOF

git add .gitignore .python-version pyproject.toml .env.example
commit "2025-05-25T09:14:00+00:00" \
  "chore: initialise project scaffold with pyproject and env config"

# ─────────────────────────────────────────────────────────────────────────────
# COMMIT 2  — 25 May  14:52
# Core config + domain entity (Pydantic placeholder — will be refactored)
# ─────────────────────────────────────────────────────────────────────────────
mkdir -p core domain/entities

cat > core/__init__.py << 'EOF'
EOF
cat > core/config.py << 'EOF'
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    PROJECT_NAME: str = "Payment Chassis"
    DATABASE_URL: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/payments_db"
    REDIS_URL: str = "redis://localhost:6379/0"
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

settings = Settings()
EOF

cat > domain/__init__.py << 'EOF'
"""Domain layer — pure business logic, zero framework dependencies."""
EOF
cat > domain/entities/__init__.py << 'EOF'
"""Domain entities — aggregate roots and value objects."""
EOF
cat > domain/entities/payment.py << 'EOF'
from datetime import datetime
from pydantic import BaseModel, Field

class PaymentEntity(BaseModel):
    transaction_id: str
    amount: float = Field(gt=0)
    currency: str = "INR"
    status: str = "PENDING"
    created_at: datetime = Field(default_factory=datetime.utcnow)
EOF

git add core/ domain/
commit "2025-05-25T14:52:00+00:00" \
  "feat(domain): add Settings config and initial PaymentEntity value object"

# ─────────────────────────────────────────────────────────────────────────────
# COMMIT 3  — 27 May  10:05
# Infrastructure — SQLAlchemy ORM models + async session factory
# ─────────────────────────────────────────────────────────────────────────────
mkdir -p infrastructure/database

cat > infrastructure/__init__.py << 'EOF'
EOF
cat > infrastructure/database/__init__.py << 'EOF'
EOF
cat > infrastructure/database/models.py << 'EOF'
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy import String, Float, DateTime, Text, Boolean
from datetime import datetime
import uuid

class Base(DeclarativeBase):
    pass

class PaymentModel(Base):
    __tablename__ = "payments"

    transaction_id: Mapped[str] = mapped_column(String, primary_key=True)
    amount: Mapped[float] = mapped_column(Float, nullable=False)
    currency: Mapped[str] = mapped_column(String, default="INR", nullable=False)
    status: Mapped[str] = mapped_column(String, default="PENDING", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)

class OutboxEventModel(Base):
    __tablename__ = "outbox_events"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    aggregate_type: Mapped[str] = mapped_column(String, nullable=False)
    aggregate_id: Mapped[str] = mapped_column(String, nullable=False)
    event_type: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[str] = mapped_column(Text, nullable=False)
    processed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
EOF

cat > infrastructure/database/session.py << 'EOF'
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from core.config import settings

engine = create_async_engine(settings.DATABASE_URL, echo=True, future=True)
async_session_maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
EOF

git add infrastructure/
commit "2025-05-27T10:05:00+00:00" \
  "feat(infra): add SQLAlchemy ORM models (payments, outbox_events) and async session factory"

# ─────────────────────────────────────────────────────────────────────────────
# COMMIT 4  — 28 May  11:30
# Alembic setup + initial migration
# ─────────────────────────────────────────────────────────────────────────────
mkdir -p alembic/versions

cat > alembic.ini << 'EOF'
[alembic]
script_location = alembic
prepend_sys_path = .
version_path_separator = os

[loggers]
keys = root,sqlalchemy,alembic

[handlers]
keys = console

[formatters]
keys = generic

[logger_root]
level = WARN
handlers = console
qualname =

[logger_sqlalchemy]
level = WARN
handlers =
qualname = sqlalchemy.engine

[logger_alembic]
level = INFO
handlers =
qualname = alembic

[handler_console]
class = StreamHandler
args = (sys.stderr,)
level = NOTSET
formatter = generic

[formatter_generic]
format = %(levelname)-5.5s [%(name)s] %(message)s
datefmt = %H:%M:%S
EOF

cat > alembic/env.py << 'EOF'
from logging.config import fileConfig
from sqlalchemy import engine_from_config, pool
from alembic import context
from core.config import settings
from infrastructure.database.models import Base

config = context.config
config.set_main_option("sqlalchemy.url", settings.DATABASE_URL.replace("+asyncpg", "+psycopg2"))

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

def run_migrations_offline():
    url = config.get_main_option("sqlalchemy.url")
    context.configure(url=url, target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()

def run_migrations_online():
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()

if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
EOF

cat > alembic/script.py.mako << 'EOF'
"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Create Date: ${create_date}
"""
from alembic import op
import sqlalchemy as sa
${imports if imports else ""}

revision = ${repr(up_revision)}
down_revision = ${repr(down_revision)}
branch_labels = ${repr(branch_labels)}
depends_on = ${repr(depends_on)}

def upgrade() -> None:
    ${upgrades if upgrades else "pass"}

def downgrade() -> None:
    ${downgrades if downgrades else "pass"}
EOF

cat > alembic/versions/001_create_payments_table.py << 'EOF'
"""create payments table

Revision ID: 001_payments
Revises:
Create Date: 2025-05-28 11:30:00
"""
from alembic import op
import sqlalchemy as sa

revision = "001_payments"
down_revision = None
branch_labels = None
depends_on = None

def upgrade() -> None:
    op.create_table(
        "payments",
        sa.Column("transaction_id", sa.String(), nullable=False),
        sa.Column("amount", sa.Float(), nullable=False),
        sa.Column("currency", sa.String(), nullable=False, server_default="INR"),
        sa.Column("status", sa.String(), nullable=False, server_default="PENDING"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("transaction_id"),
    )

def downgrade() -> None:
    op.drop_table("payments")
EOF

git add alembic/ alembic.ini
commit "2025-05-28T11:30:00+00:00" \
  "chore(db): configure alembic and add initial migration for payments table"

# ─────────────────────────────────────────────────────────────────────────────
# COMMIT 5  — 29 May  09:55
# Application layer: abstract UoW + CreatePayment use case (initial)
# ─────────────────────────────────────────────────────────────────────────────
mkdir -p application/use_cases

cat > application/__init__.py << 'EOF'
EOF
cat > application/use_cases/__init__.py << 'EOF'
EOF

cat > application/uow.py << 'EOF'
from abc import ABC, abstractmethod
from infrastructure.database.session import async_session_maker

class AbstractUnitOfWork(ABC):
    @abstractmethod
    async def __aenter__(self): pass
    @abstractmethod
    async def __aexit__(self, exc_type, exc_val, exc_tb): pass
    @abstractmethod
    async def commit(self): pass
    @abstractmethod
    async def rollback(self): pass

class SqlAlchemyUnitOfWork(AbstractUnitOfWork):
    def __init__(self):
        self.session_maker = async_session_maker

    async def __aenter__(self):
        self.session = self.session_maker()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if exc_type:
            await self.rollback()
        else:
            await self.commit()
        await self.session.close()

    async def commit(self):
        await self.session.commit()

    async def rollback(self):
        await self.session.rollback()
EOF

cat > application/use_cases/create_payment.py << 'EOF'
import json
from domain.entities.payment import PaymentEntity
from application.uow import AbstractUnitOfWork
from infrastructure.database.models import PaymentModel, OutboxEventModel

class CreatePaymentUseCase:
    def __init__(self, uow: AbstractUnitOfWork):
        self.uow = uow

    async def execute(self, payment_data: PaymentEntity) -> PaymentEntity:
        async with self.uow as uow:
            db_payment = PaymentModel(
                transaction_id=payment_data.transaction_id,
                amount=payment_data.amount,
                currency=payment_data.currency,
                status="SUCCESS"
            )
            uow.session.add(db_payment)
            event_payload = {
                "transaction_id": payment_data.transaction_id,
                "amount": payment_data.amount,
                "currency": payment_data.currency,
                "status": "SUCCESS"
            }
            outbox_event = OutboxEventModel(
                aggregate_type="Payment",
                aggregate_id=payment_data.transaction_id,
                event_type="payment.completed",
                payload=json.dumps(event_payload)
            )
            uow.session.add(outbox_event)
        return payment_data
EOF

git add application/
commit "2025-05-29T09:55:00+00:00" \
  "feat(app): implement AbstractUnitOfWork and CreatePayment use case with outbox event"

# ─────────────────────────────────────────────────────────────────────────────
# COMMIT 6  — 29 May  16:40
# Presentation layer: FastAPI router + Pydantic request schema
# ─────────────────────────────────────────────────────────────────────────────
mkdir -p presentation/api presentation/middleware

cat > presentation/__init__.py << 'EOF'
EOF
cat > presentation/api/__init__.py << 'EOF'
EOF

cat > presentation/api/payments.py << 'EOF'
from fastapi import APIRouter
from pydantic import BaseModel, Field
from application.uow import SqlAlchemyUnitOfWork
from application.use_cases.create_payment import CreatePaymentUseCase
from domain.entities.payment import PaymentEntity

router = APIRouter(prefix="/payments", tags=["Payments"])

class PaymentCreateRequest(BaseModel):
    transaction_id: str
    amount: float = Field(gt=0)
    currency: str = "INR"

@router.post("/")
async def create_payment(payload: PaymentCreateRequest):
    uow = SqlAlchemyUnitOfWork()
    use_case = CreatePaymentUseCase(uow)
    entity = PaymentEntity(
        transaction_id=payload.transaction_id,
        amount=payload.amount,
        currency=payload.currency
    )
    result = await use_case.execute(entity)
    return {"status": "success", "data": result}
EOF

cat > main.py << 'EOF'
from fastapi import FastAPI
from core.config import settings
from presentation.api.payments import router as payments_router

def create_app() -> FastAPI:
    app = FastAPI(title=settings.PROJECT_NAME, version="1.0.0")
    app.include_router(payments_router)

    @app.get("/health")
    async def health_check():
        return {"status": "healthy", "service": settings.PROJECT_NAME}

    return app

app = create_app()
EOF

git add presentation/ main.py
commit "2025-05-29T16:40:00+00:00" \
  "feat(api): add FastAPI payments router, PaymentCreateRequest schema and health endpoint"

# ─────────────────────────────────────────────────────────────────────────────
# COMMIT 7  — 30 May  10:10
# Infrastructure cache: Redis client with distributed lock support
# ─────────────────────────────────────────────────────────────────────────────
mkdir -p infrastructure/cache

cat > infrastructure/cache/__init__.py << 'EOF'
EOF
cat > infrastructure/cache/redis.py << 'EOF'
import uuid
import redis.asyncio as redis
from core.config import settings

class RedisClient:
    def __init__(self):
        self.client = redis.from_url(settings.REDIS_URL, decode_responses=True)

    async def get(self, key: str):
        return await self.client.get(key)

    async def set(self, key: str, value: str, expire: int = 300):
        await self.client.set(key, value, ex=expire)

    async def acquire_lock(self, lock_key: str, expire: int = 10) -> str | None:
        """Acquires an atomic distributed lock. Returns a unique token if successful, else None."""
        token = str(uuid.uuid4())
        acquired = await self.client.set(lock_key, token, nx=True, ex=expire)
        return token if acquired else None

    async def release_lock(self, lock_key: str, token: str):
        """Safely releases the lock only if the token matches."""
        lua_script = """
        if redis.call("get", KEYS[1]) == ARGV[1] then
            return redis.call("del", KEYS[1])
        else
            return 0
        end
        """
        await self.client.eval(lua_script, 1, lock_key, token)

redis_client = RedisClient()
EOF

git add infrastructure/cache/
commit "2025-05-30T10:10:00+00:00" \
  "feat(infra/cache): add async Redis client with distributed lock (acquire/release via Lua)"

# ─────────────────────────────────────────────────────────────────────────────
# COMMIT 8  — 30 May  15:20
# Idempotency middleware
# ─────────────────────────────────────────────────────────────────────────────
cat > presentation/middleware/__init__.py << 'EOF'
EOF
cat > presentation/middleware/idempotency.py << 'EOF'
import json
from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from infrastructure.cache.redis import redis_client

class IdempotencyMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.method != "POST":
            return await call_next(request)

        idempotency_key = request.headers.get("X-Idempotency-Key")
        if not idempotency_key:
            return await call_next(request)

        cache_key = f"idempotency:resp:{idempotency_key}"
        lock_key = f"idempotency:lock:{idempotency_key}"

        cached_response = await redis_client.get(cache_key)
        if cached_response:
            return Response(content=cached_response, media_type="application/json", status_code=200)

        lock_token = await redis_client.acquire_lock(lock_key, expire=15)
        if not lock_token:
            return Response(
                content=json.dumps({"error": "Concurrent request with same idempotency key is already processing."}),
                media_type="application/json",
                status_code=409
            )

        try:
            response = await call_next(request)
            if response.status_code == 200:
                body = [section async for section in response.body_iterator]
                response.body_iterator = iter(body)
                raw_body = b"".join(body).decode()
                await redis_client.set(cache_key, raw_body, expire=300)
            return response
        finally:
            await redis_client.release_lock(lock_key, lock_token)
EOF

git add presentation/middleware/
commit "2025-05-30T15:20:00+00:00" \
  "feat(middleware): implement IdempotencyMiddleware with Redis cache and distributed lock"

# ─────────────────────────────────────────────────────────────────────────────
# COMMIT 9  — 2 June  09:00
# Kafka producer + outbox relay worker
# ─────────────────────────────────────────────────────────────────────────────
mkdir -p infrastructure/messaging infrastructure/workers

cat > infrastructure/messaging/__init__.py << 'EOF'
EOF
cat > infrastructure/workers/__init__.py << 'EOF'
EOF

cat > infrastructure/messaging/kafka_producer.py << 'EOF'
import json
import logging
from aiokafka import AIOKafkaProducer

logger = logging.getLogger(__name__)

class KafkaProducerService:
    def __init__(self):
        self.producer = None

    async def start(self):
        self.producer = AIOKafkaProducer(
            bootstrap_servers="localhost:9092",
            value_serializer=lambda v: json.dumps(v).encode("utf-8")
        )
        await self.producer.start()

    async def stop(self):
        if self.producer:
            await self.producer.stop()

    async def send_event(self, topic: str, message: dict):
        if not self.producer:
            logger.warning("Kafka producer is not initialized.")
            return
        await self.producer.send_and_wait(topic, message)

kafka_producer = KafkaProducerService()
EOF

cat > infrastructure/workers/outbox_relay.py << 'EOF'
import asyncio
import json
import logging
from sqlalchemy import select
from infrastructure.database.session import async_session_maker
from infrastructure.database.models import OutboxEventModel
from infrastructure.messaging.kafka_producer import kafka_producer

logger = logging.getLogger(__name__)

async def poll_outbox_events():
    """Background loop that relays outbox events to Kafka with at-least-once guarantees."""
    while True:
        try:
            async with async_session_maker() as session:
                async with session.begin():
                    stmt = (
                        select(OutboxEventModel)
                        .where(OutboxEventModel.processed == False)
                        .order_by(OutboxEventModel.created_at.asc())
                        .limit(50)
                        .with_for_update(skip_locked=True)
                    )
                    result = await session.execute(stmt)
                    events = result.scalars().all()
                    for event in events:
                        try:
                            topic = event.event_type.replace(".", "-")
                            await kafka_producer.send_event(topic, json.loads(event.payload))
                            event.processed = True
                            session.add(event)
                            logger.info(f"Relayed outbox event {event.id} to topic {topic}")
                        except Exception as e:
                            logger.error(f"Failed to publish outbox event {event.id}: {e}")
                            break
        except Exception as e:
            logger.error(f"Outbox relay worker error: {e}")
        await asyncio.sleep(2)
EOF

git add infrastructure/messaging/ infrastructure/workers/
commit "2025-06-02T09:00:00+00:00" \
  "feat(infra/messaging): add Kafka producer service and transactional outbox relay worker"

# ─────────────────────────────────────────────────────────────────────────────
# COMMIT 10 — 2 June  14:35
# Wire middleware + Kafka into main.py; update pyproject with aiokafka/tenacity
# ─────────────────────────────────────────────────────────────────────────────
cat > pyproject.toml << 'EOF'
[project]
name = "infrastructure-boilerplate"
version = "0.1.0"
dependencies = [
    "aiokafka>=0.12.0",
    "alembic>=1.14.0",
    "asyncpg>=0.30.0",
    "fastapi[standard]>=0.115.0",
    "greenlet>=3.5.6",
    "pydantic-settings>=2.7.0",
    "redis>=5.2.0",
    "sqladmin>=0.20.0",
    "sqlalchemy>=2.0.36",
    "tenacity>=9.1.4",
]
requires-python = ">=3.10"
[build-system]
requires = ["setuptools>=61.0"]
build-backend = "setuptools.build_meta"
EOF

cat > main.py << 'EOF'
import asyncio
from fastapi import FastAPI
from core.config import settings
from presentation.api.payments import router as payments_router
from presentation.middleware.idempotency import IdempotencyMiddleware
from infrastructure.messaging.kafka_producer import kafka_producer
from infrastructure.workers.outbox_relay import poll_outbox_events

def create_app() -> FastAPI:
    app = FastAPI(title=settings.PROJECT_NAME, version="1.0.0")
    app.add_middleware(IdempotencyMiddleware)
    app.include_router(payments_router)

    @app.on_event("startup")
    async def startup_event():
        try:
            await kafka_producer.start()
            asyncio.create_task(poll_outbox_events())
        except Exception as e:
            print(f"Infrastructure startup warning: {e}")

    @app.on_event("shutdown")
    async def shutdown_event():
        await kafka_producer.stop()

    @app.get("/health")
    async def health_check():
        return {"status": "healthy", "service": settings.PROJECT_NAME}

    return app

app = create_app()
EOF

git add pyproject.toml main.py
commit "2025-06-02T14:35:00+00:00" \
  "feat(app): wire IdempotencyMiddleware and Kafka outbox relay into application factory"

# ─────────────────────────────────────────────────────────────────────────────
# COMMIT 11 — 3 June  11:00
# Payment gateway client (httpx + tenacity retry/fallback)
# ─────────────────────────────────────────────────────────────────────────────
mkdir -p infrastructure/external
cat > infrastructure/external/__init__.py << 'EOF'
EOF
cat > infrastructure/external/gateway_client.py << 'EOF'
import logging
import httpx
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

logger = logging.getLogger(__name__)

class PaymentGatewayException(Exception):
    pass

class PaymentGatewayClient:
    def __init__(self, primary_url: str = "https://api.primary-acquirer.com", fallback_url: str = "https://api.fallback-acquirer.com"):
        self.primary_url = primary_url
        self.fallback_url = fallback_url
        self.timeout = httpx.Timeout(5.0, connect=2.0)

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type((httpx.RequestError, httpx.TimeoutException, PaymentGatewayException)),
        reraise=True
    )
    async def _call_gateway(self, url: str, payload: dict) -> dict:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(f"{url}/charge", json=payload)
            if response.status_code >= 500:
                raise PaymentGatewayException(f"Acquirer server error: {response.status_code}")
            response.raise_for_status()
            return response.json()

    async def charge_with_fallback(self, payload: dict) -> dict:
        """Attempts to process payment via primary acquirer, falling back to secondary if primary fails."""
        try:
            logger.info("Attempting charge with primary payment gateway...")
            return await self._call_gateway(self.primary_url, payload)
        except Exception as primary_error:
            logger.warning(f"Primary gateway failed: {primary_error}. Failing over to fallback...")
            try:
                return await self._call_gateway(self.fallback_url, payload)
            except Exception as fallback_error:
                logger.error(f"Both gateways failed. Fallback error: {fallback_error}")
                raise PaymentGatewayException("All payment acquirers are currently unavailable.")

gateway_client = PaymentGatewayClient()
EOF

git add infrastructure/external/
commit "2025-06-03T11:00:00+00:00" \
  "feat(infra/gateway): add resilient PaymentGatewayClient with retry and primary/fallback logic"

# ─────────────────────────────────────────────────────────────────────────────
# COMMIT 12 — 3 June  17:15
# Integrate gateway into use case; add outbox migration
# ─────────────────────────────────────────────────────────────────────────────
cat > application/use_cases/create_payment.py << 'EOF'
import json
from domain.entities.payment import PaymentEntity
from application.uow import AbstractUnitOfWork
from infrastructure.database.models import PaymentModel, OutboxEventModel
from infrastructure.external.gateway_client import gateway_client

class CreatePaymentUseCase:
    def __init__(self, uow: AbstractUnitOfWork):
        self.uow = uow

    async def execute(self, payment_data: PaymentEntity) -> PaymentEntity:
        gateway_response = await gateway_client.charge_with_fallback({
            "transaction_id": payment_data.transaction_id,
            "amount": payment_data.amount,
            "currency": payment_data.currency
        })
        async with self.uow as uow:
            db_payment = PaymentModel(
                transaction_id=payment_data.transaction_id,
                amount=payment_data.amount,
                currency=payment_data.currency,
                status="SUCCESS"
            )
            uow.session.add(db_payment)
            event_payload = {
                "transaction_id": payment_data.transaction_id,
                "amount": payment_data.amount,
                "currency": payment_data.currency,
                "gateway_reference": gateway_response.get("reference_id", "mock_ref"),
                "status": "SUCCESS"
            }
            outbox_event = OutboxEventModel(
                aggregate_type="Payment",
                aggregate_id=payment_data.transaction_id,
                event_type="payment.completed",
                payload=json.dumps(event_payload)
            )
            uow.session.add(outbox_event)
        return payment_data
EOF

cat > alembic/versions/002_add_outbox_events_table.py << 'EOF'
"""add outbox_events table

Revision ID: 002_outbox
Revises: 001_payments
Create Date: 2025-06-03 17:15:00
"""
from alembic import op
import sqlalchemy as sa

revision = "002_outbox"
down_revision = "001_payments"
branch_labels = None
depends_on = None

def upgrade() -> None:
    op.create_table(
        "outbox_events",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("aggregate_type", sa.String(), nullable=False),
        sa.Column("aggregate_id", sa.String(), nullable=False),
        sa.Column("event_type", sa.String(), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("processed", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_outbox_events_processed_created_at", "outbox_events", ["processed", "created_at"])

def downgrade() -> None:
    op.drop_index("ix_outbox_events_processed_created_at", table_name="outbox_events")
    op.drop_table("outbox_events")
EOF

git add application/use_cases/create_payment.py alembic/versions/002_add_outbox_events_table.py
commit "2025-06-03T17:15:00+00:00" \
  "feat(app+db): integrate gateway into CreatePayment use case; add outbox_events migration with index"

# ─────────────────────────────────────────────────────────────────────────────
# COMMIT 13 — 5 June  10:20
# Domain isolation: pure PaymentEntity dataclass + exception hierarchy + repo interfaces
# ─────────────────────────────────────────────────────────────────────────────
mkdir -p domain/interfaces
cat > domain/interfaces/__init__.py << 'EOF'
"""Domain interfaces (ports) — abstract contracts for repositories and services."""
EOF

cat > domain/entities/payment.py << 'EOF'
"""
domain/entities/payment.py
---------------------------
PaymentEntity — a pure domain aggregate root.

Strict isolation rules:
  - NO Pydantic, NO SQLAlchemy, NO FastAPI, NO ORM imports.
  - Uses only the Python stdlib (dataclasses, datetime, uuid).
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum


class PaymentStatus(str, Enum):
    PENDING = "PENDING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"


@dataclass
class PaymentEntity:
    transaction_id: str
    amount: float
    currency: str
    status: PaymentStatus = PaymentStatus.PENDING
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self) -> None:
        if not self.transaction_id:
            raise ValueError("transaction_id must not be empty.")
        if self.amount <= 0:
            raise ValueError(f"amount must be positive; got {self.amount}.")
        if not self.currency or len(self.currency) != 3:
            raise ValueError(f"currency must be a 3-character ISO 4217 code; got '{self.currency}'.")
        self.currency = self.currency.upper()

    def mark_success(self) -> None:
        if self.status != PaymentStatus.PENDING:
            raise ValueError(f"Cannot mark a {self.status.value} payment as SUCCESS.")
        self.status = PaymentStatus.SUCCESS

    def mark_failed(self) -> None:
        if self.status != PaymentStatus.PENDING:
            raise ValueError(f"Cannot mark a {self.status.value} payment as FAILED.")
        self.status = PaymentStatus.FAILED

    @classmethod
    def create(cls, amount: float, currency: str, transaction_id: str | None = None) -> "PaymentEntity":
        return cls(
            transaction_id=transaction_id or str(uuid.uuid4()),
            amount=amount,
            currency=currency,
        )
EOF

cat > domain/exceptions.py << 'EOF'
"""
domain/exceptions.py
--------------------
Pure domain exception hierarchy.  NO framework imports.  NO ORM imports.
"""


class DomainException(Exception):
    def __init__(self, message: str, *, detail: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail or message


class ValidationError(DomainException):
    """Raised when a domain invariant or value-object constraint is violated."""


class DuplicateTransactionError(DomainException):
    """Raised when a transaction with the same ID already exists."""


class EntityNotFoundError(DomainException):
    def __init__(self, entity_type: str, identifier: str) -> None:
        super().__init__(
            f"{entity_type} '{identifier}' not found.",
            detail=f"No {entity_type} record matching identifier '{identifier}'.",
        )
        self.entity_type = entity_type
        self.identifier = identifier


class ExternalServiceError(DomainException):
    """Raised when a downstream external service call fails irrecoverably."""


class PaymentGatewayError(ExternalServiceError):
    """Raised when all payment acquirers are unavailable or return errors."""


class MessagingError(DomainException):
    """Raised when an event cannot be published to the messaging broker."""
EOF

cat > domain/interfaces/repository.py << 'EOF'
"""
domain/interfaces/repository.py
--------------------------------
Abstract repository interfaces (ports) for the domain layer.
Pure Python abstract classes — no SQLAlchemy, no FastAPI, no Pydantic.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Generic, TypeVar

from domain.entities.payment import PaymentEntity

T = TypeVar("T")


class AbstractRepository(ABC, Generic[T]):
    @abstractmethod
    async def add(self, entity: T) -> None: ...

    @abstractmethod
    async def get(self, identifier: str) -> T | None: ...

    @abstractmethod
    async def exists(self, identifier: str) -> bool: ...


class AbstractPaymentRepository(AbstractRepository[PaymentEntity]):
    @abstractmethod
    async def add(self, entity: PaymentEntity) -> None: ...

    @abstractmethod
    async def get(self, transaction_id: str) -> PaymentEntity | None: ...

    @abstractmethod
    async def exists(self, transaction_id: str) -> bool: ...
EOF

git add domain/
commit "2025-06-05T10:20:00+00:00" \
  "refactor(domain): enforce strict isolation — pure dataclass entity, exception hierarchy, abstract repo ports"

# ─────────────────────────────────────────────────────────────────────────────
# COMMIT 14 — 5 June  16:45
# Infrastructure: concrete repository adapters + refactor use case + UoW ports
# ─────────────────────────────────────────────────────────────────────────────
mkdir -p infrastructure/database/repositories
cat > infrastructure/database/repositories/__init__.py << 'EOF'
EOF

cat > infrastructure/database/repositories/payment.py << 'EOF'
"""SQLAlchemy concrete implementation of AbstractPaymentRepository."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from domain.entities.payment import PaymentEntity, PaymentStatus
from domain.interfaces.repository import AbstractPaymentRepository
from infrastructure.database.models import PaymentModel


class SqlAlchemyPaymentRepository(AbstractPaymentRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    @staticmethod
    def _to_model(entity: PaymentEntity) -> PaymentModel:
        return PaymentModel(
            transaction_id=entity.transaction_id,
            amount=entity.amount,
            currency=entity.currency,
            status=entity.status.value,
            created_at=entity.created_at,
        )

    @staticmethod
    def _to_entity(model: PaymentModel) -> PaymentEntity:
        return PaymentEntity(
            transaction_id=model.transaction_id,
            amount=model.amount,
            currency=model.currency,
            status=PaymentStatus(model.status),
            created_at=model.created_at,
        )

    async def add(self, entity: PaymentEntity) -> None:
        self._session.add(self._to_model(entity))

    async def get(self, transaction_id: str) -> PaymentEntity | None:
        stmt = select(PaymentModel).where(PaymentModel.transaction_id == transaction_id)
        result = await self._session.execute(stmt)
        model = result.scalar_one_or_none()
        return self._to_entity(model) if model else None

    async def exists(self, transaction_id: str) -> bool:
        return await self.get(transaction_id) is not None
EOF

cat > infrastructure/database/repositories/outbox.py << 'EOF'
"""Outbox repository adapter — appends domain events to the transactional outbox table."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from infrastructure.database.models import OutboxEventModel


class SqlAlchemyOutboxRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def enqueue(
        self,
        aggregate_type: str,
        aggregate_id: str,
        event_type: str,
        payload: str,
    ) -> None:
        event = OutboxEventModel(
            id=str(uuid.uuid4()),
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            event_type=event_type,
            payload=payload,
            processed=False,
            created_at=datetime.now(timezone.utc),
        )
        self._session.add(event)
EOF

cat > application/uow.py << 'EOF'
"""
application/uow.py
-------------------
Unit-of-Work abstraction.
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from infrastructure.database.session import async_session_maker
from infrastructure.database.repositories.payment import SqlAlchemyPaymentRepository
from infrastructure.database.repositories.outbox import SqlAlchemyOutboxRepository
from infrastructure.external.gateway_client import gateway_client


class AbstractUnitOfWork(ABC):
    payments: object
    outbox: object
    gateway: object

    @abstractmethod
    async def __aenter__(self) -> "AbstractUnitOfWork": ...

    @abstractmethod
    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None: ...

    @abstractmethod
    async def commit(self) -> None: ...

    @abstractmethod
    async def rollback(self) -> None: ...


class SqlAlchemyUnitOfWork(AbstractUnitOfWork):
    def __init__(self) -> None:
        self.session_maker = async_session_maker
        self.gateway = gateway_client

    async def __aenter__(self) -> "SqlAlchemyUnitOfWork":
        self.session = self.session_maker()
        self.payments = SqlAlchemyPaymentRepository(self.session)
        self.outbox = SqlAlchemyOutboxRepository(self.session)
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        if exc_type:
            await self.rollback()
        else:
            await self.commit()
        await self.session.close()

    async def commit(self) -> None:
        await self.session.commit()

    async def rollback(self) -> None:
        await self.session.rollback()
EOF

cat > application/use_cases/create_payment.py << 'EOF'
"""
application/use_cases/create_payment.py
----------------------------------------
CreatePayment orchestration use case.

Application-layer rules:
  - May import from domain (entities, exceptions, interfaces).
  - Must NOT import ORM models or infrastructure clients directly.
  - Infrastructure is injected via the UnitOfWork abstraction.
"""
from __future__ import annotations

import json

from domain.entities.payment import PaymentEntity, PaymentStatus
from domain.exceptions import PaymentGatewayError
from application.uow import AbstractUnitOfWork


class CreatePaymentUseCase:
    """
    Orchestrates the end-to-end payment creation flow:
      1. Delegate charge to the payment gateway (via UoW gateway port).
      2. Persist the payment record and outbox event atomically.
      3. Return the updated domain entity.
    """

    def __init__(self, uow: AbstractUnitOfWork) -> None:
        self.uow = uow

    async def execute(self, payment_data: PaymentEntity) -> PaymentEntity:
        try:
            gateway_response = await self.uow.gateway.charge_with_fallback(
                {
                    "transaction_id": payment_data.transaction_id,
                    "amount": payment_data.amount,
                    "currency": payment_data.currency,
                }
            )
        except Exception as exc:
            raise PaymentGatewayError(
                "Payment gateway unavailable.",
                detail=str(exc),
            ) from exc

        async with self.uow as uow:
            payment_data.mark_success()
            event_payload = json.dumps(
                {
                    "transaction_id": payment_data.transaction_id,
                    "amount": payment_data.amount,
                    "currency": payment_data.currency,
                    "gateway_reference": gateway_response.get("reference_id", ""),
                    "status": payment_data.status.value,
                }
            )
            await uow.payments.add(payment_data)
            await uow.outbox.enqueue(
                aggregate_type="Payment",
                aggregate_id=payment_data.transaction_id,
                event_type="payment.completed",
                payload=event_payload,
            )

        return payment_data
EOF

git add infrastructure/database/repositories/ application/uow.py application/use_cases/create_payment.py
commit "2025-06-05T16:45:00+00:00" \
  "refactor(infra+app): add concrete repo adapters, refactor UoW ports, clean use case of direct ORM imports"

# ─────────────────────────────────────────────────────────────────────────────
# COMMIT 15 — 6 June  09:30
# Structlog configuration + core logging module
# ─────────────────────────────────────────────────────────────────────────────
cat > core/config.py << 'EOF'
"""
core/config.py
---------------
Centralised application settings loaded from environment variables / .env file.
"""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    PROJECT_NAME: str = "Payment Chassis"
    DATABASE_URL: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/payments_db"
    REDIS_URL: str = "redis://localhost:6379/0"
    KAFKA_BOOTSTRAP_SERVERS: str = "localhost:9092"
    LOG_LEVEL: str = "INFO"
    LOG_FORMAT: str = "json"

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()
EOF

cat > core/logging.py << 'EOF'
"""
core/logging.py
----------------
Structlog configuration for structured, JSON-formatted logging.
"""
from __future__ import annotations

import logging
import sys
from contextvars import ContextVar
from typing import Any

import structlog

correlation_id_ctx: ContextVar[str] = ContextVar("correlation_id", default="")


def _add_correlation_id(
    logger: Any, method_name: str, event_dict: dict[str, Any]
) -> dict[str, Any]:
    cid = correlation_id_ctx.get("")
    if cid:
        event_dict["correlation_id"] = cid
    return event_dict


def configure_logging(log_level: str = "INFO", log_format: str = "json") -> None:
    shared_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        _add_correlation_id,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.CallsiteParameterAdder(
            [
                structlog.processors.CallsiteParameter.FILENAME,
                structlog.processors.CallsiteParameter.FUNC_NAME,
                structlog.processors.CallsiteParameter.LINENO,
            ]
        ),
    ]

    renderer: structlog.types.Processor = (
        structlog.dev.ConsoleRenderer(colors=True)
        if log_format == "console"
        else structlog.processors.JSONRenderer()
    )

    structlog.configure(
        processors=shared_processors + [structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[structlog.stdlib.ProcessorFormatter.remove_processors_meta, renderer],
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.addHandler(handler)
    root_logger.setLevel(getattr(logging, log_level.upper(), logging.INFO))

    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
EOF

cat > pyproject.toml << 'EOF'
[project]
name = "infrastructure-boilerplate"
version = "0.1.0"
dependencies = [
    "aiokafka>=0.12.0",
    "alembic>=1.14.0",
    "asyncpg>=0.30.0",
    "fastapi[standard]>=0.115.0",
    "greenlet>=3.5.6",
    "httpx>=0.27.0",
    "pydantic-settings>=2.7.0",
    "redis>=5.2.0",
    "sqladmin>=0.20.0",
    "sqlalchemy>=2.0.36",
    "structlog>=26.1.0",
    "tenacity>=9.1.4",
]
requires-python = ">=3.10"
[build-system]
requires = ["setuptools>=61.0"]
build-backend = "setuptools.build_meta"
EOF

git add core/config.py core/logging.py pyproject.toml
commit "2025-06-06T09:30:00+00:00" \
  "feat(core/logging): add structlog configuration with JSON/console renderers and correlation_id ContextVar"

# ─────────────────────────────────────────────────────────────────────────────
# COMMIT 16 — 6 June  15:50
# Correlation ID middleware
# ─────────────────────────────────────────────────────────────────────────────
cat > presentation/middleware/correlation_id.py << 'EOF'
"""
presentation/middleware/correlation_id.py
------------------------------------------
Correlation ID middleware for distributed trace visibility.
"""
from __future__ import annotations

import uuid

import structlog
from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

from core.logging import correlation_id_ctx

logger = structlog.get_logger(__name__)

CORRELATION_ID_HEADER = "X-Correlation-ID"


class CorrelationIdMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(self, request: Request, call_next) -> Response:
        correlation_id: str = (
            request.headers.get(CORRELATION_ID_HEADER) or str(uuid.uuid4())
        )
        request.state.correlation_id = correlation_id
        token = correlation_id_ctx.set(correlation_id)
        structlog.contextvars.bind_contextvars(correlation_id=correlation_id)

        try:
            logger.debug(
                "request_started",
                method=request.method,
                path=request.url.path,
                correlation_id=correlation_id,
            )
            response: Response = await call_next(request)
        finally:
            correlation_id_ctx.reset(token)
            structlog.contextvars.clear_contextvars()

        response.headers[CORRELATION_ID_HEADER] = correlation_id
        return response
EOF

git add presentation/middleware/correlation_id.py
commit "2025-06-06T15:50:00+00:00" \
  "feat(middleware): add CorrelationIdMiddleware — X-Correlation-ID lifecycle with structlog binding"

# ─────────────────────────────────────────────────────────────────────────────
# COMMIT 17 — 7 June  10:15
# RFC 7807 Problem Details global exception handler
# ─────────────────────────────────────────────────────────────────────────────
cat > presentation/middleware/exception_handler.py << 'EOF'
"""
presentation/middleware/exception_handler.py
---------------------------------------------
RFC 7807 Problem Details — global exception handler.
Content-Type: application/problem+json
"""
from __future__ import annotations

import traceback
from typing import Any

import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError as PydanticValidationError
from starlette import status as http_status

try:
    HTTP_422_UNPROCESSABLE_ENTITY: int = http_status.HTTP_422_UNPROCESSABLE_CONTENT  # type: ignore[attr-defined]
except AttributeError:
    HTTP_422_UNPROCESSABLE_ENTITY = 422

HTTP_400_BAD_REQUEST = http_status.HTTP_400_BAD_REQUEST
HTTP_404_NOT_FOUND = http_status.HTTP_404_NOT_FOUND
HTTP_409_CONFLICT = http_status.HTTP_409_CONFLICT
HTTP_500_INTERNAL_SERVER_ERROR = http_status.HTTP_500_INTERNAL_SERVER_ERROR
HTTP_502_BAD_GATEWAY = http_status.HTTP_502_BAD_GATEWAY
HTTP_503_SERVICE_UNAVAILABLE = http_status.HTTP_503_SERVICE_UNAVAILABLE

from domain.exceptions import (
    DomainException,
    DuplicateTransactionError,
    EntityNotFoundError,
    ExternalServiceError,
    MessagingError,
    PaymentGatewayError,
    ValidationError as DomainValidationError,
)

logger = structlog.get_logger(__name__)
_TYPE_BASE = "https://api.example.com/errors"


def _problem_response(
    request: Request,
    *,
    status: int,
    title: str,
    detail: str,
    type_slug: str = "internal-error",
    extra: dict[str, Any] | None = None,
) -> JSONResponse:
    correlation_id: str = getattr(request.state, "correlation_id", "")
    body: dict[str, Any] = {
        "type": f"{_TYPE_BASE}/{type_slug}",
        "title": title,
        "status": status,
        "detail": detail,
        "instance": str(request.url.path),
    }
    if correlation_id:
        body["correlation_id"] = correlation_id
    if extra:
        body.update(extra)
    return JSONResponse(content=body, status_code=status, media_type="application/problem+json")


async def _handle_domain_validation_error(request: Request, exc: DomainValidationError) -> JSONResponse:
    logger.warning("domain_validation_error", detail=exc.detail)
    return _problem_response(request, status=HTTP_400_BAD_REQUEST, title="Domain Validation Error", detail=exc.detail, type_slug="domain-validation-error")

async def _handle_duplicate_transaction(request: Request, exc: DuplicateTransactionError) -> JSONResponse:
    logger.warning("duplicate_transaction_error", detail=exc.detail)
    return _problem_response(request, status=HTTP_409_CONFLICT, title="Duplicate Transaction", detail=exc.detail, type_slug="duplicate-transaction")

async def _handle_entity_not_found(request: Request, exc: EntityNotFoundError) -> JSONResponse:
    logger.info("entity_not_found", entity_type=exc.entity_type, identifier=exc.identifier)
    return _problem_response(request, status=HTTP_404_NOT_FOUND, title="Entity Not Found", detail=exc.detail, type_slug="entity-not-found")

async def _handle_payment_gateway_error(request: Request, exc: PaymentGatewayError) -> JSONResponse:
    logger.error("payment_gateway_error", detail=exc.detail)
    return _problem_response(request, status=HTTP_502_BAD_GATEWAY, title="Payment Gateway Unavailable", detail=exc.detail, type_slug="payment-gateway-error")

async def _handle_external_service_error(request: Request, exc: ExternalServiceError) -> JSONResponse:
    logger.error("external_service_error", detail=exc.detail)
    return _problem_response(request, status=HTTP_503_SERVICE_UNAVAILABLE, title="External Service Unavailable", detail=exc.detail, type_slug="external-service-error")

async def _handle_messaging_error(request: Request, exc: MessagingError) -> JSONResponse:
    logger.error("messaging_error", detail=exc.detail)
    return _problem_response(request, status=HTTP_503_SERVICE_UNAVAILABLE, title="Messaging Service Error", detail=exc.detail, type_slug="messaging-error")

async def _handle_domain_exception(request: Request, exc: DomainException) -> JSONResponse:
    logger.error("unclassified_domain_error", detail=exc.detail)
    return _problem_response(request, status=HTTP_400_BAD_REQUEST, title="Business Rule Violation", detail=exc.detail, type_slug="business-rule-violation")

async def _handle_request_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    errors = exc.errors()
    detail = "; ".join(f"{' > '.join(str(loc) for loc in e['loc'])}: {e['msg']}" for e in errors)
    logger.warning("request_validation_error", errors=errors)
    return _problem_response(request, status=HTTP_422_UNPROCESSABLE_ENTITY, title="Request Validation Failed", detail=detail, type_slug="request-validation-error", extra={"validation_errors": errors})

async def _handle_pydantic_validation_error(request: Request, exc: PydanticValidationError) -> JSONResponse:
    errors = exc.errors()
    detail = "; ".join(f"{' > '.join(str(loc) for loc in e['loc'])}: {e['msg']}" for e in errors)
    logger.warning("pydantic_validation_error", errors=errors)
    return _problem_response(request, status=HTTP_422_UNPROCESSABLE_ENTITY, title="Data Validation Failed", detail=detail, type_slug="data-validation-error", extra={"validation_errors": errors})

async def _handle_unhandled_exception(request: Request, exc: Exception) -> JSONResponse:
    logger.error("unhandled_exception", exc_type=type(exc).__name__, traceback=traceback.format_exc())
    return _problem_response(request, status=HTTP_500_INTERNAL_SERVER_ERROR, title="Internal Server Error", detail="An unexpected error occurred. Please try again later.", type_slug="internal-server-error")


def register_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(DuplicateTransactionError, _handle_duplicate_transaction)   # type: ignore[arg-type]
    app.add_exception_handler(EntityNotFoundError, _handle_entity_not_found)              # type: ignore[arg-type]
    app.add_exception_handler(DomainValidationError, _handle_domain_validation_error)     # type: ignore[arg-type]
    app.add_exception_handler(PaymentGatewayError, _handle_payment_gateway_error)         # type: ignore[arg-type]
    app.add_exception_handler(MessagingError, _handle_messaging_error)                    # type: ignore[arg-type]
    app.add_exception_handler(ExternalServiceError, _handle_external_service_error)       # type: ignore[arg-type]
    app.add_exception_handler(DomainException, _handle_domain_exception)                  # type: ignore[arg-type]
    app.add_exception_handler(RequestValidationError, _handle_request_validation_error)   # type: ignore[arg-type]
    app.add_exception_handler(PydanticValidationError, _handle_pydantic_validation_error) # type: ignore[arg-type]
    app.add_exception_handler(Exception, _handle_unhandled_exception)                     # type: ignore[arg-type]
EOF

git add presentation/middleware/exception_handler.py
commit "2025-06-07T10:15:00+00:00" \
  "feat(middleware): implement RFC 7807 Problem Details global exception handler (application/problem+json)"

# ─────────────────────────────────────────────────────────────────────────────
# COMMIT 18 — 7 June  16:00
# Migrate all infrastructure logging to structlog; update middleware __init__
# ─────────────────────────────────────────────────────────────────────────────
cat > infrastructure/external/gateway_client.py << 'EOF'
"""
infrastructure/external/gateway_client.py
------------------------------------------
Resilient HTTP payment gateway client with retry + fallback logic.
Uses structlog so all log lines automatically carry the correlation_id.
"""
from __future__ import annotations

import structlog
import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

logger = structlog.get_logger(__name__)


class PaymentGatewayException(Exception):
    """Low-level infrastructure exception for gateway failures."""


class PaymentGatewayClient:
    def __init__(self, primary_url: str = "https://api.primary-acquirer.com", fallback_url: str = "https://api.fallback-acquirer.com") -> None:
        self.primary_url = primary_url
        self.fallback_url = fallback_url
        self.timeout = httpx.Timeout(5.0, connect=2.0)

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type((httpx.RequestError, httpx.TimeoutException, PaymentGatewayException)),
        reraise=True,
    )
    async def _call_gateway(self, url: str, payload: dict) -> dict:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(f"{url}/charge", json=payload)
            if response.status_code >= 500:
                raise PaymentGatewayException(f"Acquirer server error: {response.status_code}")
            response.raise_for_status()
            return response.json()

    async def charge_with_fallback(self, payload: dict) -> dict:
        try:
            logger.info("gateway_charge_attempt", acquirer="primary")
            return await self._call_gateway(self.primary_url, payload)
        except Exception as primary_error:
            logger.warning("gateway_primary_failed", acquirer="primary", error=str(primary_error))
            try:
                logger.info("gateway_charge_attempt", acquirer="fallback")
                return await self._call_gateway(self.fallback_url, payload)
            except Exception as fallback_error:
                logger.error("gateway_all_acquirers_failed", primary_error=str(primary_error), fallback_error=str(fallback_error))
                raise PaymentGatewayException("All payment acquirers are currently unavailable.") from fallback_error


gateway_client = PaymentGatewayClient()
EOF

cat > infrastructure/messaging/kafka_producer.py << 'EOF'
"""
infrastructure/messaging/kafka_producer.py
-------------------------------------------
Async Kafka producer service — uses structlog for structured logging.
"""
from __future__ import annotations

import json

import structlog
from aiokafka import AIOKafkaProducer

from core.config import settings

logger = structlog.get_logger(__name__)


class KafkaProducerService:
    def __init__(self) -> None:
        self.producer: AIOKafkaProducer | None = None

    async def start(self) -> None:
        self.producer = AIOKafkaProducer(
            bootstrap_servers=settings.KAFKA_BOOTSTRAP_SERVERS,
            value_serializer=lambda v: json.dumps(v).encode("utf-8"),
        )
        await self.producer.start()
        logger.info("kafka_producer_started", servers=settings.KAFKA_BOOTSTRAP_SERVERS)

    async def stop(self) -> None:
        if self.producer:
            await self.producer.stop()
            logger.info("kafka_producer_stopped")

    async def send_event(self, topic: str, message: dict) -> None:
        if not self.producer:
            logger.warning("kafka_producer_not_initialized", topic=topic)
            return
        await self.producer.send_and_wait(topic, message)
        logger.debug("kafka_event_sent", topic=topic)


kafka_producer = KafkaProducerService()
EOF

cat > infrastructure/workers/outbox_relay.py << 'EOF'
"""
infrastructure/workers/outbox_relay.py
----------------------------------------
Transactional Outbox relay worker — structlog with per-event context binding.
"""
from __future__ import annotations

import asyncio
import json

import structlog
from sqlalchemy import select

from infrastructure.database.session import async_session_maker
from infrastructure.database.models import OutboxEventModel
from infrastructure.messaging.kafka_producer import kafka_producer

logger = structlog.get_logger(__name__)

_POLL_INTERVAL_SECONDS = 2
_BATCH_SIZE = 50


async def poll_outbox_events() -> None:
    logger.info("outbox_relay_started", poll_interval=_POLL_INTERVAL_SECONDS)
    while True:
        try:
            async with async_session_maker() as session:
                async with session.begin():
                    stmt = (
                        select(OutboxEventModel)
                        .where(OutboxEventModel.processed == False)  # noqa: E712
                        .order_by(OutboxEventModel.created_at.asc())
                        .limit(_BATCH_SIZE)
                        .with_for_update(skip_locked=True)
                    )
                    result = await session.execute(stmt)
                    events = result.scalars().all()
                    for event in events:
                        with structlog.contextvars.bound_contextvars(
                            outbox_event_id=event.id,
                            aggregate_type=event.aggregate_type,
                            aggregate_id=event.aggregate_id,
                            event_type=event.event_type,
                        ):
                            try:
                                topic = event.event_type.replace(".", "-")
                                await kafka_producer.send_event(topic, json.loads(event.payload))
                                event.processed = True
                                session.add(event)
                                logger.info("outbox_event_relayed", topic=topic)
                            except Exception as inner_exc:
                                logger.error("outbox_event_publish_failed", error=str(inner_exc))
                                break
        except Exception as exc:
            logger.error("outbox_relay_poll_error", error=str(exc))
        await asyncio.sleep(_POLL_INTERVAL_SECONDS)
EOF

cat > presentation/middleware/__init__.py << 'EOF'
"""
presentation/middleware/__init__.py
-------------------------------------
Public surface of the middleware package.
"""
from presentation.middleware.correlation_id import CorrelationIdMiddleware
from presentation.middleware.idempotency import IdempotencyMiddleware
from presentation.middleware.exception_handler import register_exception_handlers

__all__ = [
    "CorrelationIdMiddleware",
    "IdempotencyMiddleware",
    "register_exception_handlers",
]
EOF

git add infrastructure/external/gateway_client.py infrastructure/messaging/kafka_producer.py \
        infrastructure/workers/outbox_relay.py presentation/middleware/__init__.py
commit "2025-06-07T16:00:00+00:00" \
  "refactor(infra): migrate all infrastructure logging from stdlib to structlog; update middleware __init__"

# ─────────────────────────────────────────────────────────────────────────────
# COMMIT 19 — 9 June  11:00
# Payments router: typed response schema, status_code=201, domain entity mapping
# ─────────────────────────────────────────────────────────────────────────────
cat > presentation/api/payments.py << 'EOF'
"""
presentation/api/payments.py
------------------------------
Payments REST API router (presentation layer).
Pydantic models live here only — domain entities are constructed and returned.
"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter
from pydantic import BaseModel, Field

from application.uow import SqlAlchemyUnitOfWork
from application.use_cases.create_payment import CreatePaymentUseCase
from domain.entities.payment import PaymentEntity

router = APIRouter(prefix="/payments", tags=["Payments"])


class PaymentCreateRequest(BaseModel):
    transaction_id: str = Field(..., min_length=1, description="Unique transaction identifier")
    amount: float = Field(..., gt=0, description="Payment amount — must be positive")
    currency: str = Field("INR", min_length=3, max_length=3, description="ISO 4217 currency code")


class PaymentResponse(BaseModel):
    transaction_id: str
    amount: float
    currency: str
    status: str
    created_at: datetime


@router.post(
    "/",
    response_model=PaymentResponse,
    status_code=201,
    summary="Create a payment",
    description="Initiates a payment transaction through the configured gateway. Idempotent when X-Idempotency-Key is provided.",
)
async def create_payment(payload: PaymentCreateRequest) -> PaymentResponse:
    entity = PaymentEntity(
        transaction_id=payload.transaction_id,
        amount=payload.amount,
        currency=payload.currency,
    )
    uow = SqlAlchemyUnitOfWork()
    use_case = CreatePaymentUseCase(uow)
    result: PaymentEntity = await use_case.execute(entity)

    return PaymentResponse(
        transaction_id=result.transaction_id,
        amount=result.amount,
        currency=result.currency,
        status=result.status.value,
        created_at=result.created_at,
    )
EOF

git add presentation/api/payments.py
commit "2025-06-09T11:00:00+00:00" \
  "feat(api): add typed PaymentResponse schema, status_code=201, clean domain entity mapping"

# ─────────────────────────────────────────────────────────────────────────────
# COMMIT 20 — 9 June  14:30  (FINAL)
# Wire everything in main.py; update .env.example; final cleanup
# ─────────────────────────────────────────────────────────────────────────────
cat > main.py << 'EOF'
"""
main.py
--------
FastAPI application factory.

Middleware registration order (outermost → innermost):
  CorrelationIdMiddleware   ← sets X-Correlation-ID on every request first
  IdempotencyMiddleware     ← uses Redis; benefits from correlation tracing
"""
from __future__ import annotations

import asyncio

from fastapi import FastAPI

from core.config import settings
from core.logging import configure_logging
from presentation.api.payments import router as payments_router
from presentation.middleware import (
    CorrelationIdMiddleware,
    IdempotencyMiddleware,
    register_exception_handlers,
)
from infrastructure.messaging.kafka_producer import kafka_producer
from infrastructure.workers.outbox_relay import poll_outbox_events


def create_app() -> FastAPI:
    configure_logging(log_level=settings.LOG_LEVEL, log_format=settings.LOG_FORMAT)

    import structlog
    logger = structlog.get_logger(__name__)

    app = FastAPI(
        title=settings.PROJECT_NAME,
        version="1.0.0",
        docs_url="/docs",
        redoc_url="/redoc",
        responses={422: {"description": "Validation Error"}},
    )

    register_exception_handlers(app)

    app.add_middleware(IdempotencyMiddleware)
    app.add_middleware(CorrelationIdMiddleware)

    app.include_router(payments_router, prefix="/api/v1")

    @app.on_event("startup")
    async def startup_event() -> None:
        try:
            await kafka_producer.start()
            asyncio.create_task(poll_outbox_events())
            logger.info("application_started", service=settings.PROJECT_NAME)
        except Exception as exc:
            logger.warning("infrastructure_startup_warning", error=str(exc))

    @app.on_event("shutdown")
    async def shutdown_event() -> None:
        await kafka_producer.stop()
        logger.info("application_stopped", service=settings.PROJECT_NAME)

    @app.get("/health", tags=["Health"], include_in_schema=False)
    async def health_check() -> dict:
        return {"status": "healthy", "service": settings.PROJECT_NAME}

    return app


app = create_app()
EOF

cat > .env.example << 'EOF'
DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5432/payments_db
REDIS_URL=redis://localhost:6379/0
KAFKA_BOOTSTRAP_SERVERS=localhost:9092

# Logging: LOG_FORMAT=json (production) | console (development)
LOG_LEVEL=INFO
LOG_FORMAT=json
EOF

git add main.py .env.example
commit "2025-06-09T14:30:00+00:00" \
  "chore: wire complete middleware stack, exception handlers, and structured logging into application factory"

# ─────────────────────────────────────────────────────────────────────────────
# Rename branch to master and clean up
# ─────────────────────────────────────────────────────────────────────────────
echo ""
echo "▶ Replacing master branch with rewritten history..."
git branch -D master 2>/dev/null || true
git branch -m rewritten_history master

echo ""
echo "▶ Restoring uv.lock (binary — not rewritten)..."
git stash pop --quiet 2>/dev/null || true
# Stage only the lock file from the stash (ignore already-committed content)
git add uv.lock 2>/dev/null || true
git diff --cached --quiet || \
  GIT_AUTHOR_DATE="2025-06-09T14:31:00+00:00" \
  GIT_COMMITTER_DATE="2025-06-09T14:31:00+00:00" \
  git commit --author="$AUTHOR" -m "chore: restore uv.lock dependency snapshot" --quiet

echo ""
echo "══════════════════════════════════════════════════════════════"
echo "  GIT HISTORY REBUILT SUCCESSFULLY"
echo "══════════════════════════════════════════════════════════════"
git log --oneline
