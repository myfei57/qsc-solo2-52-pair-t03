"""The water ledger: pinned ceilings, metered draws and shutdown rinses.

The ledger is the single place the metered water is interpreted against the
quota.  Every draw belongs to a batch that was *pinned* to the quota version
current when the batch started, so an entry can always be explained against
the table that was in force at the time.  Fresh and recycled water are kept in
separate columns and a shutdown rinse is its own entry kind that never touches
the recycled allowance.

Writes are idempotent: a caller that retries after a lost response repeats the
same ``idem`` key and gets the original entry back instead of a second one.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from ...errors import DuplicateError, NotFoundError, OverLimitError, ValidationError
from .quota import FRESH, RECYCLED, WATER_KINDS, WaterQuota


@dataclass(frozen=True, slots=True)
class BatchPin:
    """The quota version a batch is locked to for its whole life."""

    batch_id: str
    grade: str
    version: int
    batch_water_l: float
    start_tick: int

    def describe(self) -> dict[str, Any]:
        return {
            "batch_id": self.batch_id,
            "grade": self.grade,
            "version": self.version,
            "batch_water_l": self.batch_water_l,
            "start_tick": self.start_tick,
        }


@dataclass(frozen=True, slots=True)
class WaterEntry:
    """One metered draw of recycled or fresh water against a pinned batch."""

    seq: int
    idem: str
    batch_id: str
    grade: str
    version: int
    kind: str
    litres: float
    recycled_before_l: float
    recycled_after_l: float
    fresh_after_l: float
    limit_l: float
    bound_kind: str
    tick: int
    shift: str

    def describe(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "idem": self.idem,
            "batch_id": self.batch_id,
            "grade": self.grade,
            "version": self.version,
            "kind": self.kind,
            "litres": self.litres,
            "recycled_before_l": self.recycled_before_l,
            "recycled_after_l": self.recycled_after_l,
            "fresh_after_l": self.fresh_after_l,
            "limit_l": self.limit_l,
            "bound_kind": self.bound_kind,
            "tick": self.tick,
            "shift": self.shift,
        }


@dataclass(frozen=True, slots=True)
class RinseEntry:
    """Shutdown rinse water: metered and audited, but never quota counted."""

    seq: int
    idem: str
    litres: float
    tick: int
    shift: str

    def describe(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "idem": self.idem,
            "litres": self.litres,
            "tick": self.tick,
            "shift": self.shift,
            "purpose": "shutdown_rinse",
            "quota_counted": False,
        }


class WaterLedger:
    """Accumulates the water usage under the pinned quota versions."""

    __slots__ = ("_pins", "_entries", "_rinses", "_idem", "_seq")

    def __init__(self) -> None:
        self._pins: dict[str, BatchPin] = {}
        self._entries: list[WaterEntry] = []
        self._rinses: list[RinseEntry] = []
        self._idem: dict[str, tuple[str, Mapping[str, Any]]] = {}
        self._seq = 0

    def pin(self, pin: BatchPin) -> BatchPin:
        """Record or return the pin for ``batch_id``.

        Re-pinning the same batch is a no-op that returns the original pin:
        the version is decided once, when the batch starts, and a later table
        change must never move it.
        """

        existing = self._pins.get(pin.batch_id)
        if existing is not None:
            return existing
        self._pins[pin.batch_id] = pin
        return pin

    def adopt_pin(self, pin: BatchPin) -> BatchPin:
        return self.pin(pin)

    def is_pinned(self, batch_id: str) -> bool:
        return batch_id in self._pins

    def require_pin(self, batch_id: str) -> BatchPin:
        pin = self._pins.get(batch_id)
        if pin is None:
            raise NotFoundError("batch has no pinned water quota", batch_id=batch_id)
        return pin

    def pin_of(self, batch_id: str) -> BatchPin | None:
        return self._pins.get(batch_id)

    def draw(
        self,
        *,
        idem: str,
        batch_id: str,
        kind: str,
        litres: float,
        quota: WaterQuota,
        tick: int,
        shift: str = "default",
    ) -> WaterEntry:
        """Meter one draw, refusing a recycled draw that crosses the ceiling.

        The refusal carries both derived bounds and ``bound_kind`` so the
        operator is told whether the ratio or the absolute ceiling stopped it.
        Nothing is recorded when the draw is refused.
        """

        if not idem:
            raise ValidationError("a water draw needs an idempotency key")
        duplicate = self._idem.get(idem)
        if duplicate is not None:
            kind_tag, payload = duplicate
            if kind_tag != "draw":
                raise DuplicateError("idempotency key was already used for another water action", idem=idem)
            return WaterEntry(**dict(payload))
        if kind not in WATER_KINDS:
            raise ValidationError("water kind must be recycled or fresh", kind=kind)
        if litres <= 0:
            raise ValidationError("a water draw needs a positive volume", litres=litres)
        pin = self.require_pin(batch_id)
        if pin.grade != quota.grade:
            raise ValidationError(
                "quota grade does not match the pinned batch",
                batch_id=batch_id,
                pinned_grade=pin.grade,
                quota_grade=quota.grade,
            )
        if pin.version != quota.version:
            raise ValidationError(
                "a running batch stays on its pinned quota version",
                batch_id=batch_id,
                pinned_version=pin.version,
                quota_version=quota.version,
            )

        recycled_before = self.usage_for(batch_id)[RECYCLED]
        fresh_before = self.usage_for(batch_id)[FRESH]
        ceiling = quota.ceiling_for(pin.batch_water_l)
        recycled_after = recycled_before + (litres if kind == RECYCLED else 0.0)
        if kind == RECYCLED and recycled_after > ceiling.limit_l + 1e-9:
            raise OverLimitError(
                "recycled water would cross the grade ceiling",
                batch_id=batch_id,
                grade=quota.grade,
                quota_version=quota.version,
                requested_l=round(litres, 4),
                already_recycled_l=round(recycled_before, 4),
                would_total_l=round(recycled_after, 4),
                limit_l=ceiling.limit_l,
                remaining_l=round(max(ceiling.limit_l - recycled_before, 0.0), 4),
                ratio_bound_l=ceiling.ratio_bound_l,
                absolute_bound_l=ceiling.absolute_bound_l,
                bound_kind=ceiling.bound_kind,
            )

        self._seq += 1
        entry = WaterEntry(
            seq=self._seq,
            idem=idem,
            batch_id=batch_id,
            grade=quota.grade,
            version=quota.version,
            kind=kind,
            litres=round(litres, 4),
            recycled_before_l=round(recycled_before, 4),
            recycled_after_l=round(recycled_after, 4),
            fresh_after_l=round(fresh_before + (litres if kind == FRESH else 0.0), 4),
            limit_l=ceiling.limit_l,
            bound_kind=ceiling.bound_kind,
            tick=tick,
            shift=shift,
        )
        self._entries.append(entry)
        self._idem[idem] = ("draw", _entry_kwargs(entry))
        return entry

    def adopt_entry(self, entry: Mapping[str, Any]) -> None:
        """Re-adopt an entry read back from the stream after a restart."""

        idem = str(entry["idem"])
        if idem in self._idem:
            return
        restored = WaterEntry(**dict(entry))
        self._entries.append(restored)
        self._idem[idem] = ("draw", _entry_kwargs(restored))
        self._seq = max(self._seq, restored.seq)

    def rinse(self, *, idem: str, litres: float, tick: int, shift: str = "default") -> RinseEntry:
        """Meter shutdown rinse water outside every batch allowance."""

        if not idem:
            raise ValidationError("a rinse needs an idempotency key")
        duplicate = self._idem.get(idem)
        if duplicate is not None:
            kind_tag, payload = duplicate
            if kind_tag != "rinse":
                raise DuplicateError("idempotency key was already used for another water action", idem=idem)
            return RinseEntry(**dict(payload))
        if litres <= 0:
            raise ValidationError("a rinse needs a positive volume", litres=litres)
        self._seq += 1
        entry = RinseEntry(seq=self._seq, idem=idem, litres=round(litres, 4), tick=tick, shift=shift)
        self._rinses.append(entry)
        self._idem[idem] = ("rinse", _rinse_kwargs(entry))
        return entry

    def adopt_rinse(self, entry: Mapping[str, Any]) -> None:
        idem = str(entry["idem"])
        if idem in self._idem:
            return
        restored = RinseEntry(
            seq=int(entry["seq"]),
            idem=idem,
            litres=float(entry["litres"]),
            tick=int(entry["tick"]),
            shift=str(entry.get("shift", "default")),
        )
        self._rinses.append(restored)
        self._idem[idem] = ("rinse", _rinse_kwargs(restored))
        self._seq = max(self._seq, restored.seq)

    def usage_for(self, batch_id: str) -> dict[str, float]:
        recycled = round(
            sum(e.litres for e in self._entries if e.batch_id == batch_id and e.kind == RECYCLED), 4
        )
        fresh = round(sum(e.litres for e in self._entries if e.batch_id == batch_id and e.kind == FRESH), 4)
        return {RECYCLED: recycled, FRESH: fresh}

    def totals(self) -> dict[str, Any]:
        """Return the split totals used by the console and the roll-up."""

        return {
            "recycled_l": round(sum(e.litres for e in self._entries if e.kind == RECYCLED), 4),
            "fresh_l": round(sum(e.litres for e in self._entries if e.kind == FRESH), 4),
            "rinse_l": round(sum(r.litres for r in self._rinses), 4),
            "draws": len(self._entries),
            "rinses": len(self._rinses),
        }

    def by_grade_version(self) -> list[dict[str, Any]]:
        """Aggregate the split usage under the (grade, version) that explains it.

        This is the historical-immutable view: a batch keeps accruing under
        the version it was pinned to even after a newer quota is published.
        """

        rows: dict[tuple[str, int], dict[str, float]] = {}
        for entry in self._entries:
            key = (entry.grade, entry.version)
            row = rows.setdefault(key, {RECYCLED: 0.0, FRESH: 0.0})
            row[entry.kind] = round(row[entry.kind] + entry.litres, 4)
        return [
            {"grade": grade, "version": version, RECYCLED: usage[RECYCLED], FRESH: usage[FRESH]}
            for (grade, version), usage in sorted(rows.items())
        ]

    def by_shift(self) -> list[dict[str, Any]]:
        """Split the totals per shift; shifts never change a ceiling."""

        rows: dict[str, dict[str, float]] = {}
        for entry in self._entries:
            row = rows.setdefault(entry.shift, {RECYCLED: 0.0, FRESH: 0.0, "rinse": 0.0})
            row[entry.kind] = round(row[entry.kind] + entry.litres, 4)
        for rinse in self._rinses:
            row = rows.setdefault(rinse.shift, {RECYCLED: 0.0, FRESH: 0.0, "rinse": 0.0})
            row["rinse"] = round(row["rinse"] + rinse.litres, 4)
        return [
            {
                "shift": shift,
                "recycled_l": usage[RECYCLED],
                "fresh_l": usage[FRESH],
                "rinse_l": usage["rinse"],
            }
            for shift, usage in sorted(rows.items())
        ]

    def batch_summary(self, batch_id: str) -> dict[str, Any]:
        pin = self.require_pin(batch_id)
        usage = self.usage_for(batch_id)
        return {
            "batch_id": batch_id,
            "grade": pin.grade,
            "version": pin.version,
            "batch_water_l": pin.batch_water_l,
            "recycled_l": usage[RECYCLED],
            "fresh_l": usage[FRESH],
        }

    def entries(self) -> tuple[WaterEntry, ...]:
        return tuple(self._entries)

    def rinses(self) -> tuple[RinseEntry, ...]:
        return tuple(self._rinses)

    def pins(self) -> tuple[BatchPin, ...]:
        return tuple(self._pins.values())

    def has_idem(self, idem: str) -> bool:
        return idem in self._idem

    def entry_for_idem(self, idem: str) -> WaterEntry | None:
        """Return the draw already stored under ``idem``, if any."""

        match = self._idem.get(idem)
        if match is None or match[0] != "draw":
            return None
        return WaterEntry(**dict(match[1]))

    def rinse_for_idem(self, idem: str) -> RinseEntry | None:
        """Return the rinse already stored under ``idem``, if any."""

        match = self._idem.get(idem)
        if match is None or match[0] != "rinse":
            return None
        return RinseEntry(**dict(match[1]))


def _entry_kwargs(entry: WaterEntry) -> dict[str, Any]:
    return {field: getattr(entry, field) for field in WaterEntry.__slots__}


def _rinse_kwargs(entry: RinseEntry) -> dict[str, Any]:
    return {field: getattr(entry, field) for field in RinseEntry.__slots__}
