"""
infrastructure/database/repositories/ledger.py
------------------------------------------------
SQLAlchemy concrete implementation of AbstractLedgerRepository.

Mapping rules:
  - ``LedgerTransaction`` (frozen domain dataclass) → ``LedgerTransactionModel``
  - ``LedgerEntry`` (frozen domain dataclass)       → ``LedgerEntryModel``
  - ``amount`` is stored as ``Decimal`` via the ``Numeric(18,4)`` column type;
    no ``float()`` cast is ever performed.
"""
from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from domain.entities.ledger import LedgerEntry, LedgerTransaction
from domain.interfaces.ledger_repository import AbstractLedgerRepository
from infrastructure.database.models import LedgerEntryModel, LedgerTransactionModel


class SqlAlchemyLedgerRepository(AbstractLedgerRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ------------------------------------------------------------------
    # Mapping helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _entry_to_model(entry: LedgerEntry) -> LedgerEntryModel:
        return LedgerEntryModel(
            id=entry.id,
            transaction_id=entry.transaction_id,
            account_id=entry.account_id,
            amount=entry.amount,        # Decimal → Numeric(18,4) directly
            currency=entry.currency,
            created_at=entry.created_at,
        )

    @staticmethod
    def _txn_to_models(
        txn: LedgerTransaction,
    ) -> tuple[LedgerTransactionModel, list[LedgerEntryModel]]:
        txn_model = LedgerTransactionModel(
            id=txn.id,
            reference_id=txn.reference,
            description=txn.description,
            created_at=txn.created_at,
        )
        entry_models = [SqlAlchemyLedgerRepository._entry_to_model(e) for e in txn.entries]
        return txn_model, entry_models

    @staticmethod
    def _model_to_entry(model: LedgerEntryModel) -> LedgerEntry:
        return LedgerEntry(
            id=model.id,
            transaction_id=model.transaction_id,
            account_id=model.account_id,
            amount=Decimal(str(model.amount)),   # Numeric → Decimal
            currency=model.currency,
            created_at=model.created_at,
        )

    @staticmethod
    def _model_to_txn(
        txn_model: LedgerTransactionModel,
        entry_models: list[LedgerEntryModel],
    ) -> LedgerTransaction:
        entries = [SqlAlchemyLedgerRepository._model_to_entry(e) for e in entry_models]
        # Construct directly (bypass build() so we don't re-validate on read)
        return LedgerTransaction(
            id=txn_model.id,
            reference=txn_model.reference_id,
            description=txn_model.description,
            entries=tuple(entries),
            created_at=txn_model.created_at,
        )

    # ------------------------------------------------------------------
    # Repository operations
    # ------------------------------------------------------------------

    async def add(self, ledger_txn: LedgerTransaction) -> None:
        """
        Persist the transaction and all its entries inside the active session.
        Both ORM objects are added to the session; the caller's UoW commits.
        """
        txn_model, entry_models = self._txn_to_models(ledger_txn)
        self._session.add(txn_model)
        for entry_model in entry_models:
            self._session.add(entry_model)

    async def get(self, ledger_txn_id: str) -> LedgerTransaction | None:
        stmt = (
            select(LedgerTransactionModel)
            .where(LedgerTransactionModel.id == ledger_txn_id)
            .options(selectinload(LedgerTransactionModel.entries))
        )
        result = await self._session.execute(stmt)
        txn_model = result.scalar_one_or_none()
        if txn_model is None:
            return None
        return self._model_to_txn(txn_model, list(txn_model.entries))

    async def get_by_reference(self, reference_id: str) -> list[LedgerTransaction]:
        stmt = (
            select(LedgerTransactionModel)
            .where(LedgerTransactionModel.reference_id == reference_id)
            .options(selectinload(LedgerTransactionModel.entries))
            .order_by(LedgerTransactionModel.created_at.asc())
        )
        result = await self._session.execute(stmt)
        txn_models = result.scalars().all()
        return [self._model_to_txn(m, list(m.entries)) for m in txn_models]
