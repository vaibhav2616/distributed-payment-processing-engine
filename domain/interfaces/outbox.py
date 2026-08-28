"""
domain/interfaces/outbox.py
-----------------------------
Abstract Outbox port — domain-layer contract for the transactional outbox.

Keeps the application layer free of SQLAlchemy / infrastructure knowledge.
The concrete SqlAlchemy adapter (infrastructure/database/repositories/outbox.py)
implements this interface and is injected via the Unit of Work.

NO framework imports.  NO ORM imports.  Pure ABC + stdlib only.
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from domain.events.payment_events import DomainEvent


class AbstractOutboxRepository(ABC):
    """
    Port for the Transactional Outbox pattern.

    An event enqueued here is persisted within the *same database transaction*
    as the aggregate state change, guaranteeing exactly-once delivery semantics
    when combined with the outbox relay worker.
    """

    @abstractmethod
    async def enqueue_event(self, event: DomainEvent) -> None:
        """
        Persist a domain event to the outbox table inside the current
        unit-of-work transaction.

        The relay worker will later read these rows and publish them to Kafka,
        marking each one as processed atomically via SELECT FOR UPDATE SKIP LOCKED.

        Args:
            event: A fully-constructed, immutable DomainEvent instance whose
                   ``to_dict()`` will be serialised as the outbox payload.
        """
        ...
