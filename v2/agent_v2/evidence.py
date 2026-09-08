"""Evidence ledger shared by tool execution, synthesis, and verification."""

from __future__ import annotations

from collections import defaultdict

from v2.agent_v2.models import EvidenceItem, ToolEnvelope


class EvidenceConflictError(ValueError):
    """One evidence identifier was reused for different evidence content."""


class EvidenceLedger:
    def __init__(self) -> None:
        self._items: dict[str, EvidenceItem] = {}

    def add(self, item: EvidenceItem) -> None:
        if not item.id:
            raise ValueError("evidence id is required")
        current = self._items.get(item.id)
        if current is not None and current != item:
            raise EvidenceConflictError(f"conflicting evidence id: {item.id}")
        self._items[item.id] = item

    def extend(self, items: list[EvidenceItem]) -> None:
        for item in items:
            self.add(item)

    def ingest(self, envelope: ToolEnvelope) -> None:
        self.extend(envelope.evidence)

    def get(self, evidence_id: str) -> EvidenceItem | None:
        return self._items.get(evidence_id)

    def ids(self) -> set[str]:
        return set(self._items)

    def items(self) -> list[EvidenceItem]:
        return list(self._items.values())

    def by_entity(self) -> dict[str, list[EvidenceItem]]:
        grouped: dict[str, list[EvidenceItem]] = defaultdict(list)
        for item in self._items.values():
            grouped[item.entity].append(item)
        return dict(grouped)
