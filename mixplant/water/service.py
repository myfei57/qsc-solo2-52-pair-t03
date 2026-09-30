"""Water service: batching water, flushing water and the pump prime."""

from __future__ import annotations

from ..errors import DuplicateError, NotFoundError, OverLimitError
from ..service_base import PlantService
from ..statemachine.phases import MixPhase
from .dispense import dispense_reading
from .ledger import BATCHING, FLUSH, PRIME, QuotaBinding, WaterLedger, WaterLedgerEntry, bind_quota
from .prime import prime_reading
from .quota import FRESH, RECLAIMED, WATER_KINDS
from .tank import tank_state
from .valve import valve_state

VALVE_KIND = "water.valve"
DISPENSE_KIND = "water.dispense"
USAGE_KIND = "water.usage"
BINDING_KIND = "water.binding"
FLUSH_KIND = "water.flush"
PRIME_KIND = "water.prime"
TANK_KIND = "water.tank"

QUOTA_LIMIT_CODE = "recycled_water_grade_quota"


class WaterService(PlantService):
    """Meters water and keeps quota-bound usage separate from flush water."""

    origin = "water"
    line = "mix"
    subject = "water"

    def __init__(self, context, machine=None, tank_level_l: float = 20000.0, quotas=None) -> None:
        super().__init__(context, machine)
        self._level_l = tank_level_l
        self.quotas = quotas
        self.ledger = WaterLedger()

    def open_valve(self, *, target_l: float, source: str = FRESH) -> dict[str, Any]:
        """Open a batching or flush-water valve."""

        self.require_order(target_l > 0, "the water valve needs a positive target", target_l=target_l)
        self._require_source(source)
        state = valve_state(open=True, target_l=target_l)
        self.publish(VALVE_KIND, state.describe())
        self.publish(TANK_KIND, tank_state("tank-1", self._level_l).describe())
        self.emit("water.valve_opened", {"target_l": state.target_l, "source": source})
        return {"valve": state.describe(), "tank_l": self._level_l, "source": source}

    def dispense(
        self,
        *,
        litres: float,
        rate_lps: float,
        source: str = FRESH,
        event_id: str | None = None,
        batch_id: str | None = None,
    ) -> dict[str, Any]:
        """Meter batching water into the mixer against the batch's frozen quota."""

        return self._meter(
            purpose=BATCHING,
            litres=litres,
            rate_lps=rate_lps,
            source=source,
            event_id=event_id,
            batch_id=batch_id,
        )

    def flush(
        self,
        *,
        litres: float,
        rate_lps: float,
        source: str = FRESH,
        event_id: str | None = None,
    ) -> dict[str, Any]:
        """Meter shutdown flush water; this never consumes a grade quota."""

        return self._meter(
            purpose=FLUSH,
            litres=litres,
            rate_lps=rate_lps,
            source=source,
            event_id=event_id,
            batch_id=None,
        )

    def bind_current_batch(self, *, total_water_l: float) -> QuotaBinding:
        """Freeze the current grade quota for the batch currently declared."""

        batch = self.context.batches.latest()
        if batch is None:
            raise NotFoundError("declare a batch before binding its water quota")
        quota = None if self.quotas is None else self.quotas.current_for(batch.grade)
        binding = bind_quota(
            batch_id=batch.batch_id,
            grade=batch.grade,
            quantity_m3=batch.quantity,
            total_water_l=total_water_l,
            quota=quota,
            tick=self.context.clock.now(),
        )
        try:
            self.ledger.bind(binding)
        except DuplicateError:
            return self.ledger.binding(binding.batch_id)
        self.publish(BINDING_KIND, binding.describe())
        self.emit("water.quota_bound", binding.describe())
        return binding

    def adopt_binding(self, binding: QuotaBinding) -> None:
        self.ledger.adopt_binding(binding)

    def adopt_usage(self, entry: WaterLedgerEntry) -> None:
        self.ledger.adopt_entry(entry)

    def prime(self, *, litres: float, source: str = FRESH, event_id: str | None = None) -> dict[str, Any]:
        """Confirm the prime water the pump needs before it starts."""

        self._require_source(source)
        event_id = event_id or self._auto_event_id(PRIME, litres)
        duplicate = self.ledger.find(event_id)
        reading = prime_reading(thresholds=self.context.thresholds, litres=litres)
        if duplicate is not None:
            candidate = WaterLedgerEntry(
                event_id=event_id,
                batch_id=duplicate.batch_id,
                purpose=PRIME,
                source=source,
                litres=reading.litres,
                tick=self.context.clock.now(),
            )
            if not duplicate.same_identity(candidate):
                from ..errors import DuplicateError

                raise DuplicateError(
                    "water ledger idempotency key was reused with different usage",
                    event_id=event_id,
                )
            return {"prime": reading.describe(), "ledger": duplicate.describe(), "replayed": True}
        if not reading.ok:
            self.raise_alarm("water.prime_insufficient", reading.describe())
        entry = WaterLedgerEntry(
            event_id=event_id,
            batch_id="",
            purpose=PRIME,
            source=source,
            litres=reading.litres,
            tick=self.context.clock.now(),
            excluded_from_quota=True,
        )
        self.ledger.record(entry)
        self.publish(USAGE_KIND, entry.describe())
        self.publish(PRIME_KIND, {**reading.describe(), "source": source, "event_id": event_id})
        self.emit("water.primed", {"litres": reading.litres, "source": source, "event_id": event_id})
        return {"prime": reading.describe(), "ledger": entry.describe()}

    def tank(self) -> dict[str, Any]:
        return tank_state("tank-1", self._level_l).describe()

    def restore_tank(self, level_l: float) -> None:
        """Set the in-memory tank level during journal replay."""

        self._level_l = round(level_l, 4)

    def quota_usage(self, batch_id: str) -> dict[str, Any]:
        """Return one batch's quota calculation and separated water totals."""

        return self.ledger.batch_usage(batch_id)

    def account(self) -> dict[str, Any]:
        """Return the quota and usage document shared by console and audit."""

        return {
            "scope": "grade",
            "migration_policy": "finish_running_batch_under_bound_version",
            "quotas": None if self.quotas is None else self.quotas.status(),
            "batches": [self.ledger.batch_usage(binding.batch_id) for binding in self.ledger.bindings()],
            "totals": self.ledger.totals(),
        }

    def status(self) -> dict[str, Any]:
        valve = self.payload_entry("water", "valve")
        dispense = self.payload_entry("water", "dispense")
        prime = self.payload_entry("water", "prime")
        return {
            "valve_open": bool(valve.get("active", False)) if valve else False,
            "dispensed_l": float(dispense.get("litres", 0.0)) if dispense else 0.0,
            "dispensed_ok": None if dispense is None else bool(dispense.get("ok", False)),
            "primed_l": float(prime.get("litres", 0.0)) if prime else 0.0,
            "prime_ok": None if prime is None else bool(prime.get("ok", False)),
            "tank_l": self._level_l,
            "quota_scope": "grade",
            "quotas": None if self.quotas is None else self.quotas.status(),
            "ledger": self.ledger.describe(),
        }

    def _meter(
        self,
        *,
        purpose: str,
        litres: float,
        rate_lps: float,
        source: str,
        event_id: str | None,
        batch_id: str | None,
    ) -> dict[str, Any]:
        self._require_source(source)
        reading = dispense_reading(thresholds=self.context.thresholds, litres=litres, rate_lps=rate_lps)
        event_id = event_id or self._auto_event_id(purpose, reading.litres, source)
        duplicate = self.ledger.find(event_id)
        if duplicate is not None:
            candidate = WaterLedgerEntry(
                event_id=event_id,
                batch_id=duplicate.batch_id,
                purpose=purpose,
                source=source,
                litres=reading.litres,
                tick=self.context.clock.now(),
            )
            if not duplicate.same_identity(candidate):
                from ..errors import DuplicateError

                raise DuplicateError(
                    "water ledger idempotency key was reused with different usage",
                    event_id=event_id,
                )
            duplicate_binding = self.ledger.binding(duplicate.batch_id) if duplicate.batch_id else None
            result = self._meter_result(purpose, reading, source, duplicate, duplicate_binding)
            result["replayed"] = True
            return result

        self.require("water.dispense")
        binding = None
        grade = ""
        quota_version = 0
        candidate_batch_id = ""
        if purpose == BATCHING:
            batch = self._require_batch(batch_id)
            binding = self.ledger.binding(batch.batch_id)
            candidate_batch_id = binding.batch_id
            grade = binding.grade
            quota_version = binding.quota_version
            if source == RECLAIMED:
                self._require_recycled_capacity(binding, reading.litres)

        entry = WaterLedgerEntry(
            event_id=event_id,
            batch_id=candidate_batch_id,
            purpose=purpose,
            source=source,
            litres=reading.litres,
            tick=self.context.clock.now(),
            quota_version=quota_version,
            grade=grade,
            recycled_limit_l=0.0 if binding is None else binding.recycled_limit_l,
            excluded_from_quota=purpose != BATCHING,
        )
        self.ledger.record(entry)
        self._level_l = round(self._level_l - reading.litres, 4)
        self.publish(USAGE_KIND, entry.describe())
        kind = DISPENSE_KIND if purpose == BATCHING else FLUSH_KIND
        event_name = "water.dispensed" if purpose == BATCHING else "water.flushed"
        self.publish(kind, {**reading.describe(), "source": source, "event_id": event_id})
        self.publish(TANK_KIND, tank_state("tank-1", self._level_l).describe())
        self.publish(VALVE_KIND, valve_state(open=False, target_l=reading.litres).describe())
        if not reading.ok:
            self.raise_alarm("water.flow_over_limit", reading.describe())
        self.emit(event_name, {**entry.describe(), "rate_lps": reading.rate_lps, "ok": reading.ok})
        if purpose == BATCHING and self.machine.is_at(MixPhase.IDLE.value):
            self.advance(MixPhase.WATER_IN.value, "water reached the mixer before the binder")
        return self._meter_result(purpose, reading, source, entry, binding)

    def _meter_result(self, purpose: str, reading, source: str, entry: WaterLedgerEntry, binding=None) -> dict[str, Any]:
        key = "dispense" if purpose == BATCHING else "flush"
        result = {
            key: reading.describe(),
            "source": source,
            "purpose": purpose,
            "tank_l": self._level_l,
            "ledger": entry.describe(),
        }
        if binding is not None:
            result["quota_usage"] = self.ledger.batch_usage(binding.batch_id)
        return result

    def _require_batch(self, batch_id: str | None):
        if batch_id is None:
            batch = self.context.batches.latest()
        else:
            batch = self.context.batches.find(batch_id)
        if batch is None:
            raise NotFoundError("declare a batch before metering batching water", batch_id=batch_id)
        self.ledger.binding(batch.batch_id)
        return batch

    def _require_recycled_capacity(self, binding: QuotaBinding, litres: float) -> None:
        usage = self.ledger.batch_usage(binding.batch_id)
        used = float(usage["recycled_l"])
        limit = binding.recycled_limit_l
        proposed = round(used + litres, 4)
        if proposed > round(limit + 0.0001, 4):
            detail = {
                "limit_code": QUOTA_LIMIT_CODE,
                "scope": "grade",
                "batch_id": binding.batch_id,
                "grade": binding.grade,
                "quota_version": binding.quota_version,
                "used_reclaimed_l": used,
                "used_recycled_l": used,
                "requested_l": litres,
                "requested_reclaimed_l": litres,
                "requested_recycled_l": litres,
                "requested_total_l": proposed,
                "limit_l": limit,
                "reclaimed_limit_l": limit,
                "recycled_limit_l": limit,
                "remaining_l": usage["recycled_remaining_l"],
                "remaining_reclaimed_l": usage["recycled_remaining_l"],
                "remaining_recycled_l": usage["recycled_remaining_l"],
            }
            self.raise_alarm("water.reclaimed_quota_blocked", detail)
            raise OverLimitError(
                f"reclaimed water is blocked by grade quota v{binding.quota_version} for {binding.grade}",
                **detail,
            )

    @staticmethod
    def _require_source(source: str) -> None:
        if source not in WATER_KINDS:
            from ..errors import ValidationError

            raise ValidationError("water source must be fresh or reclaimed", source=source)

    def _auto_event_id(self, purpose: str, litres: float, source: str = FRESH) -> str:
        return f"auto:{purpose}:{self.context.clock.now()}:{len(self.ledger.entries())}:{round(litres, 4)}:{source}"
