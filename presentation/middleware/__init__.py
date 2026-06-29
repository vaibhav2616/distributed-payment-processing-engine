"""
presentation/middleware/__init__.py
-------------------------------------
Public surface of the middleware package.  Import from here to keep
main.py and other consumers clean and insulated from internal paths.
"""
from presentation.middleware.correlation_id import CorrelationIdMiddleware
from presentation.middleware.idempotency import IdempotencyMiddleware
from presentation.middleware.exception_handler import register_exception_handlers

__all__ = [
    "CorrelationIdMiddleware",
    "IdempotencyMiddleware",
    "register_exception_handlers",
]
