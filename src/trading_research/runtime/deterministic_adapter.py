"""Fixture-driven test double for `execution.adapter_protocol.PaperExecutionAdapter`
(docs/milestone-3.md Step 10).

This is *not* a second production trading framework — it is a deterministic,
offline substitute for the real LumiBot paper-broker connection, which this
environment cannot exercise end-to-end without live credentials and network
access to an actual paper-trading broker (Alpaca paper, Tradier, ...; see
`runtime/lumibot/adapter.py`'s module docstring for the full boundary
explanation). Test code (and the CLI's default wiring, absent the `paper`
extra / broker credentials) pre-registers the exact outcome for each
intent_id; nothing here ever fetches a quote, calls a network endpoint, or
uses randomness — every scripted outcome is supplied by the caller.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..execution.adapter_protocol import BrokerExecutionSnapshot
from ..execution.models import PaperExecutionEvent, PaperExecutionResult, PaperOrderIntent


class DeterministicAdapterError(RuntimeError):
    """No scripted outcome was registered for a submitted/reconciled intent."""


@dataclass
class DeterministicPaperAdapter:
    _registered: dict = field(default_factory=dict)
    _reconciliations: dict = field(default_factory=dict)
    submit_calls: list = field(default_factory=list)
    reconcile_calls: list = field(default_factory=list)

    def register(
        self,
        intent_id: str,
        events: tuple[PaperExecutionEvent, ...],
        result: PaperExecutionResult,
    ) -> None:
        self._registered[intent_id] = (tuple(events), result)

    def register_reconciliation(self, intent_id: str, snapshot: BrokerExecutionSnapshot) -> None:
        self._reconciliations[intent_id] = snapshot

    def submit(
        self, intent: PaperOrderIntent
    ) -> tuple[tuple[PaperExecutionEvent, ...], PaperExecutionResult]:
        self.submit_calls.append(intent.intent_id)
        if intent.intent_id not in self._registered:
            raise DeterministicAdapterError(
                f"no scripted outcome registered for intent {intent.intent_id!r} — "
                "call .register(...) in the test setup before submitting"
            )
        return self._registered[intent.intent_id]

    def reconcile(self, intent_id: str) -> BrokerExecutionSnapshot:
        self.reconcile_calls.append(intent_id)
        if intent_id not in self._reconciliations:
            raise DeterministicAdapterError(
                f"no scripted reconciliation registered for intent {intent_id!r}"
            )
        return self._reconciliations[intent_id]
