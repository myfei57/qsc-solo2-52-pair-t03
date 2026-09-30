"""Append-only water ledger and the quota binding of each batch."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from ..errors import DuplicateError, NotFoundError, ValidationError
from .quota import FRESH, RECLAIMED, WATER_KINDS, WaterQuota

BATCHING = "batching"
FLUSH = "flush"
PRIME = "prime"
PURPOSES = (BATCHING, FLUSH, PRIME)


@dataclass(frozen=True, slots=True)
class QuotaBinding:
    """The quota version a batch is locked to when batching starts."""

    batch_id: str
    grade: str
    quantity_m3: float
    quota_version: int
    recycled_limit_l: float
    total_water_l: float
    bound_tick: int

    def __post_init__(self) -> None:
        if not self.batch_id:
            raise ValidationError("water quota binding needs a batch id")
        if not self.grade:
            raise ValidationError("water quota binding needs a grade")
        if self.quantity_m3 <= 0:
            raise ValidationError("water quota binding needs a positive quantity")
        if self.quota_version < 0:
            raise ValidationError("water quota version must not be negative")
        if self.recycled_limit_l < 0:
            raise ValidationError("recycled-water limit must not be negative")
        if self.total_water_l <= 0:
            raise ValidationError("planned water volume must be positive")

    def describe(self) -> dict[str, Any]:
        return {
            "batch_id": self.batch_id,
            "grade": self.grade,
            "quantity_m3": self.quantity_m3,
            "scope": "grade",
            "quota_version": self.quota_version,
            "recycled_limit_l": self.recycled_limit_l,
            "total_water_l": self.total_water_l,
            "bound_tick": self.bound_tick,
        }


def bind_quota(*, batch_id: str, grade: str, quantity_m3: float, total_water_l: float, quota: WaterQuota | None, tick: int) -> QuotaBinding:
    """Freeze the quota that is in force when a batch starts."""

    if total_water_l <= 0:
        raise ValidationError("planned water volume must be positive", total_water_l=total_water_l)
    if quota is None:
        # Old journals and grades configured before a quota exists retain the
        # historical zero-recycled interpretation instead of failing replay.
        limit = 0.0
        version = 0
    else:
        limit = quota.limit_for(total_water_l)
        version = quota.version
    return QuotaBinding(
        batch_id=batch_id,
        grade=grade,
        quantity_m3=round(quantity_m3, 4),
        quota_version=version,
        recycled_limit_l=limit,
        total_water_l=round(total_water_l, 4),
        bound_tick=tick,
    )


@dataclass(frozen=True, slots=True)
class WaterLedgerEntry:
    """One idempotent metered water allocation."""

    event_id: str
    batch_id: str
    purpose: str
    source: str
    litres: float
    tick: int
    quota_version: int = 0
    grade: str = ""
    recycled_limit_l: float = 0.0
    excluded_from_quota: bool = False

    def __post_init__(self) -> None:
        if not self.event_id:
            raise ValidationError("water ledger entry needs an idempotency key")
        if self.purpose not in PURPOSES:
            raise ValidationError("unknown water purpose", purpose=self.purpose)
        if self.source not in WATER_KINDS:
            raise ValidationError("water source must be fresh or reclaimed", source=self.source)
        if self.litres <= 0:
            raise ValidationError("water usage must be positive", litres=self.litres)
        if self.purpose == BATCHING and not self.batch_id:
            raise ValidationError("batching water must be attributed to a batch")
        if self.recycled_limit_l < 0:
            raise ValidationError("recycled-water limit must not be negative")

    def same_identity(self, other: "WaterLedgerEntry") -> bool:
        return (
            self.event_id == other.event_id
            and self.batch_id == other.batch_id
            and self.purpose == other.purpose
            and self.source == other.source
            and round(self.litres, 4) == round(other.litres, 4)
        )

    def describe(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "batch_id": self.batch_id,
            "purpose": self.purpose,
            "source": self.source,
            "water_kind": self.source,
            "litres": self.litres,
            "tick": self.tick,
            "quota_version": self.quota_version,
            "grade": self.grade,
            "recycled_limit_l": self.recycled_limit_l,
            "excluded_from_quota": self.excluded_from_quota,
        }

    @classmethod
    def from_document(cls, document: Mapping[str, Any]) -> "WaterLedgerEntry":
        return cls(
            event_id=str(document["event_id"]),
            batch_id=str(document.get("batch_id", "")),
            purpose=str(document["purpose"]),
            source=str(document["source"]),
            litres=float(document["litres"]),
            tick=int(document.get("tick", 0)),
            quota_version=int(document.get("quota_version", 0)),
            grade=str(document.get("grade", "")),
            recycled_limit_l=float(document.get("recycled_limit_l", 0.0)),
            excluded_from_quota=bool(document.get("excluded_from_quota", False)),
        )


class WaterLedger:
    """Current and historical water allocations with replay-safe identity."""

    __slots__ = ("_entries", "_bindings")

    def __init__(self) -> None:
        self._entries: list[WaterLedgerEntry] = []
        self._bindings: dict[str, QuotaBinding] = {}

    def bind(self, binding: QuotaBinding) -> QuotaBinding:
        existing = self._bindings.get(binding.batch_id)
        if existing is not None and existing != binding:
            raise DuplicateError("a batch cannot rebind to another water quota", batch_id=binding.batch_id)
        self._bindings[binding.batch_id] = binding
        return binding

    def adopt_binding(self, binding: QuotaBinding) -> QuotaBinding:
        self._bindings.setdefault(binding.batch_id, binding)
        return binding

    def binding(self, batch_id: str) -> QuotaBinding:
        binding = self._bindings.get(batch_id)
        if binding is None:
            raise NotFoundError("batch has no bound water quota", batch_id=batch_id)
        return binding

    def record(self, entry: WaterLedgerEntry) -> tuple[WaterLedgerEntry, bool]:
        """Record one entry; return the stored entry and whether it was reused."""

        for existing in self._entries:
            if existing.event_id == entry.event_id:
                if existing.same_identity(entry):
                    return existing, True
                raise DuplicateError(
                    "water ledger idempotency key was reused with different usage",
                    event_id=entry.event_id,
                )
        self._entries.append(entry)
        return entry, False

    def adopt_entry(self, entry: WaterLedgerEntry) -> bool:
        """Hydrate an entry from the journal and report whether it existed."""

        _, reused = self.record(entry)
        return reused

    def find(self, event_id: str) -> WaterLedgerEntry | None:
        for entry in self._entries:
            if entry.event_id == event_id:
                return entry
        return None

    def entries(self) -> tuple[WaterLedgerEntry, ...]:
        return tuple(self._entries)

    def bindings(self) -> tuple[QuotaBinding, ...]:
        return tuple(self._bindings[key] for key in sorted(self._bindings))

    def batch_usage(self, batch_id: str) -> dict[str, Any]:
        binding = self.binding(batch_id)
        entries = tuple(
            entry
            for entry in self._entries
            if entry.batch_id == batch_id and entry.purpose == BATCHING and not entry.excluded_from_quota
        )
        reclaimed = round(sum(e.litres for e in entries if e.source == RECLAIMED), 4)
        fresh = round(sum(e.litres for e in entries if e.source == FRESH), 4)
        limit = binding.recycled_limit_l
        return {
            "batch_id": batch_id,
            "grade": binding.grade,
            "scope": "grade",
            "migration_policy": "finish_running_batch_under_bound_version",
            "quota_version": binding.quota_version,
            "reclaimed_l": reclaimed,
            "recycled_l": reclaimed,
            "fresh_l": fresh,
            "recycled_limit_l": limit,
            "reclaimed_limit_l": limit,
            "recycled_remaining_l": round(max(0.0, limit - reclaimed), 4),
            "reclaimed_remaining_l": round(max(0.0, limit - reclaimed), 4),
            "recycled_over_limit_l": round(max(0.0, reclaimed - limit), 4),
            "reclaimed_over_limit_l": round(max(0.0, reclaimed - limit), 4),
            "within_limit": reclaimed <= round(limit + 0.0001, 4),
            "entries": [entry.describe() for entry in entries],
        }

    def totals(self) -> dict[str, Any]:
        values: dict[str, float] = {}
        for purpose in PURPOSES:
            for source in WATER_KINDS:
                values[f"{purpose}_{source}_l"] = 0.0
                values[f"{source}_{purpose}_l"] = 0.0
        for entry in self._entries:
            values[f"{entry.purpose}_{entry.source}_l"] += entry.litres
            values[f"{entry.source}_{entry.purpose}_l"] += entry.litres
        return {key: round(value, 4) for key, value in sorted(values.items())}

    def describe(self) -> dict[str, Any]:
        return {
            "scope": "grade",
            "bindings": [self._bindings[key].describe() for key in sorted(self._bindings)],
            "batches": [self.batch_usage(batch_id) for batch_id in sorted(self._bindings)],
            "totals": self.totals(),
            "entries": [entry.describe() for entry in self._entries],
        }
