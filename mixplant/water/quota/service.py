"""Quota service: versioned ceilings, batch pinning and enforced metering.

The quota is one station-wide table per grade and is never recomputed for a
shift.  A batch is pinned to the version current when it is declared; draws
for that batch are checked against the pinned version until the batch ends,
so publishing a new version migrates later batches without rewriting the one
already in the mixer.
"""

from __future__ import annotations

from typing import Any, Mapping

from ...errors import OverLimitError, ValidationError
from ...event.bus import Event
from ...service_base import PlantService
from .ledger import BatchPin, WaterLedger
from .quota import FRESH, RECYCLED, QuotaTable, current_or_closed
from .validation import require_quota_consistent

QUOTA_KIND = "water.quota"
PIN_KIND = "water.pin"
USAGE_KIND = "water.usage"
RINSE_KIND = "water.rinse"

SUBJECT_PREFIX = "water.quota:"
DEFAULT_SHIFT = "default"


def subject_for(grade: str) -> str:
    return f"{SUBJECT_PREFIX}{grade}"


class QuotaService(PlantService):
    """Owns the quota table and meters recycled/fresh water against it."""

    origin = "water"
    line = "batch"
    subject = ""

    def __init__(self, context, recipes=None, machine=None) -> None:
        super().__init__(context, machine)
        self.recipes = recipes
        self.table = QuotaTable()
        self.ledger = WaterLedger()

    # -- quota administration -------------------------------------------------

    def publish_quota(self, *, grade: str, recycled_ratio: float, max_litres: float) -> dict[str, Any]:
        """Publish a new immutable quota version for one grade.

        Publishing is a migration: the new version applies to batches started
        afterwards; batches already pinned keep their old version.

        The recipe/quota consistency gate runs before any generation is bumped
        or table entry appended, so a refused publish leaves no orphan state.
        """

        from .quota import WaterQuota

        candidate = WaterQuota(
            grade=grade,
            version=len(self.table.history(grade)) + 1,
            recycled_ratio=recycled_ratio,
            max_litres=max_litres,
        )
        if self.recipes is not None:
            selected = self.recipes.book.selected_name()
            if selected is not None and self.recipes.current().grade.code == grade:
                require_quota_consistent(self.recipes.current(), self.table.with_candidate(candidate))

        token = self.context.versions.bump(subject_for(grade), tick=self.context.clock.now())
        quota = self.table.publish(grade, recycled_ratio=recycled_ratio, max_litres=max_litres)
        if quota.version != token.value:
            raise ValidationError(
                "quota version fell out of step with its generation",
                grade=grade,
                quota_version=quota.version,
                generation=token.value,
            )
        record = self.store.publish(QUOTA_KIND, self.origin, quota.version, {"active": True, **quota.describe()})
        self.emit(
            "water.quota_published",
            {"grade": grade, "version": quota.version, "generation": quota.version, "seq": record.seq},
        )
        return {"quota": quota.describe(), "seq": record.seq}

    def adopt_quota(self, payload: Mapping[str, Any]) -> None:
        """Adopt a quota version read back from the stream after a restart."""

        from .quota import WaterQuota

        quota = WaterQuota.from_document(payload)
        self.table.adopt(quota)
        self.context.versions.generations.adopt(subject_for(quota.grade), quota.version)

    # -- batch pinning --------------------------------------------------------

    def handle_batch_declared(self, event: Event) -> None:
        """Pin a freshly declared batch to the quota currently in force."""

        if event.name != "agg.batch_declared":
            return
        self.pin_from_declaration(
            batch_id=str(event.payload["batch_id"]),
            grade=str(event.payload.get("grade", "")),
            batch_water_l=self._batch_water_from_targets(event.payload.get("targets")),
            tick=event.tick,
        )

    def pin_from_declaration(
        self, *, batch_id: str, grade: str, batch_water_l: float, tick: int
    ) -> BatchPin:
        existing = self.ledger.pin_of(batch_id)
        if existing is not None:
            return existing
        version = self.table.current(grade).version if self.table.has(grade) else 0
        pin = BatchPin(
            batch_id=batch_id,
            grade=grade,
            version=version,
            batch_water_l=max(0.0, float(batch_water_l)),
            start_tick=tick,
        )
        self.ledger.pin(pin)
        self.store.publish(PIN_KIND, self.origin, version, {"active": True, **pin.describe()})
        return pin

    def adopt_pin(self, payload: Mapping[str, Any]) -> None:
        pin = BatchPin(
            batch_id=str(payload["batch_id"]),
            grade=str(payload["grade"]),
            version=int(payload["version"]),
            batch_water_l=float(payload.get("batch_water_l", 0.0)),
            start_tick=int(payload.get("start_tick", 0)),
        )
        self.ledger.adopt_pin(pin)

    # -- metering -------------------------------------------------------------

    def draw(self, *, kind: str, batch_id: str, litres: float, idem: str, shift: str = DEFAULT_SHIFT) -> dict[str, Any]:
        """Meter one recycled or fresh draw for a pinned batch.

        A recycled draw that crosses the pinned ceiling is stopped on the
        spot: an alarm is raised and the over-limit error is re-raised, with
        no ledger write.  A retried request carrying a known ``idem`` key
        returns the original entry without appending a second stream record.
        """

        duplicate = self.ledger.entry_for_idem(idem)
        if duplicate is not None:
            return self._draw_result(
                duplicate.batch_id, entry=duplicate.describe(), seq=self._seq_for_idem(idem, USAGE_KIND)
            )

        pin = self.ledger.require_pin(batch_id)
        quota = self._pinned_quota(pin.grade, pin.version)
        try:
            entry = self.ledger.draw(
                idem=idem,
                batch_id=batch_id,
                kind=kind,
                litres=litres,
                quota=quota,
                tick=self.context.clock.now(),
                shift=shift,
            )
        except OverLimitError:
            ceiling = quota.ceiling_for(pin.batch_water_l)
            usage = self.ledger.usage_for(batch_id)
            self.raise_alarm(
                "water.recycled_over_quota",
                {
                    "batch_id": batch_id,
                    "grade": pin.grade,
                    "quota_version": pin.version,
                    "requested_l": round(litres, 4),
                    "already_recycled_l": usage[RECYCLED],
                    "limit_l": ceiling.limit_l,
                    "bound_kind": ceiling.bound_kind,
                    "ratio_bound_l": ceiling.ratio_bound_l,
                    "absolute_bound_l": ceiling.absolute_bound_l,
                },
            )
            raise
        record = self.store.publish(USAGE_KIND, self.origin, pin.version, {"active": True, **entry.describe()})
        self.emit(
            f"water.{kind}_drawn",
            {
                "batch_id": batch_id,
                "grade": pin.grade,
                "version": pin.version,
                "litres": entry.litres,
                "idem": idem,
                "generation": pin.version,
                "seq": record.seq,
            },
        )
        return self._draw_result(batch_id, entry=entry.describe(), seq=record.seq)

    def adopt_usage(self, payload: Mapping[str, Any]) -> None:
        fields = (
            "seq",
            "idem",
            "batch_id",
            "grade",
            "version",
            "kind",
            "litres",
            "recycled_before_l",
            "recycled_after_l",
            "fresh_after_l",
            "limit_l",
            "bound_kind",
            "tick",
            "shift",
        )
        self.ledger.adopt_entry({field: payload[field] for field in fields})

    def rinse(self, *, litres: float, idem: str, shift: str = DEFAULT_SHIFT) -> dict[str, Any]:
        """Meter shutdown rinse water; it never counts against a quota."""

        duplicate = self.ledger.rinse_for_idem(idem)
        if duplicate is not None:
            return {"rinse": duplicate.describe(), "seq": self._seq_for_idem(idem, RINSE_KIND)}

        entry = self.ledger.rinse(idem=idem, litres=litres, tick=self.context.clock.now(), shift=shift)
        record = self.store.publish(RINSE_KIND, self.origin, 0, {"active": True, **entry.describe()})
        self.emit("water.rinsed", {"litres": entry.litres, "idem": idem, "seq": record.seq})
        return {"rinse": entry.describe(), "seq": record.seq}

    def adopt_rinse(self, payload: Mapping[str, Any]) -> None:
        self.ledger.adopt_rinse(
            {
                "seq": payload["seq"],
                "idem": payload["idem"],
                "litres": payload["litres"],
                "tick": payload["tick"],
                "shift": payload.get("shift", DEFAULT_SHIFT),
            }
        )

    # -- reads ----------------------------------------------------------------

    def ceiling(self, grade: str, *, batch_water_l: float) -> dict[str, Any]:
        return current_or_closed(self.table, grade, batch_water_l=batch_water_l).describe()

    def batch_quota(self, batch_id: str) -> dict[str, Any]:
        pin = self.ledger.require_pin(batch_id)
        if self.table.has(pin.grade) and any(q.version == pin.version for q in self.table.history(pin.grade)):
            quota = self.table.version_of(pin.grade, pin.version)
            ceiling = quota.ceiling_for(pin.batch_water_l)
        else:
            from .quota import forbid_recycled

            ceiling = forbid_recycled(pin.grade, batch_water_l=pin.batch_water_l)
        usage = self.ledger.usage_for(batch_id)
        return {
            "batch_id": batch_id,
            "ceiling": ceiling.describe(),
            "used_recycled_l": usage[RECYCLED],
            "used_fresh_l": usage[FRESH],
            "remaining_recycled_l": round(max(ceiling.limit_l - usage[RECYCLED], 0.0), 4),
        }

    def quota_view(self) -> dict[str, Any]:
        """The document the console renders and the audit page links to."""

        return {
            "basis": "grade",
            "recomputed_per_shift": False,
            "quotas": self.table.describe(),
            "totals": self.ledger.totals(),
            "by_grade_version": self.ledger.by_grade_version(),
            "by_shift": self.ledger.by_shift(),
            "batches": [self.batch_quota(pin.batch_id) for pin in self.ledger.pins()],
        }

    def status(self) -> dict[str, Any]:
        return self.quota_view()

    # -- internals ------------------------------------------------------------

    def _seq_for_idem(self, idem: str, kind: str) -> int:
        """Return the stream sequence a retried write was first committed at."""

        for record in self.store.committed_records():
            if record.kind == kind and record.payload.get("idem") == idem:
                return record.seq
        # adopted after a restart before the tail query runs: fall back to ledger seq
        entry = self.ledger.entry_for_idem(idem) or self.ledger.rinse_for_idem(idem)
        return 0 if entry is None else entry.seq

    def _pinned_quota(self, grade: str, version: int):
        from ...errors import NotFoundError
        from .quota import WaterQuota

        if version == 0:
            return WaterQuota.closed(grade)
        try:
            return self.table.version_of(grade, version)
        except NotFoundError:
            raise NotFoundError(
                "the pinned quota version is no longer present in the table",
                grade=grade,
                version=version,
            )

    def _draw_result(self, batch_id: str, *, entry: dict[str, Any], seq: int) -> dict[str, Any]:
        return {"entry": entry, "seq": seq, "quota": self.batch_quota(batch_id)}

    @staticmethod
    def _batch_water_from_targets(targets: Any) -> float:
        if isinstance(targets, Mapping):
            value = targets.get("water")
            if value is not None:
                return float(value)
        return 0.0
