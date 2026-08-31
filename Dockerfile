FROM python:3.12-slim

# Set environment variables
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:$PATH" \
    PYTHONPATH="/app"

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv

# Copy dependency specifications for layer caching
COPY pyproject.toml uv.lock .python-version ./

# Install project dependencies
RUN uv sync --frozen --no-dev

# Copy application codebase
COPY alembic.ini ./
COPY alembic/ ./alembic/
COPY core/ ./core/
COPY domain/ ./domain/
COPY infrastructure/ ./infrastructure/
COPY application/ ./application/
COPY presentation/ ./presentation/
COPY main.py ./

EXPOSE 8000

# Default command
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
