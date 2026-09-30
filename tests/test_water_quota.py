"""Recycled-water quotas: versioned per grade, pinned and enforced per batch."""

from __future__ import annotations

import pytest

from mixplant.errors import DuplicateError, OverLimitError, ValidationError
from mixplant.runtime import PlantRuntime
from mixplant.water.quota import FRESH, RECYCLED, QuotaTable, WaterQuota, current_or_closed
from mixplant.water.quota.validation import quota_findings
from tests.support import RECIPE, batch_and_weigh, publish_and_select, run


# ---------------------------------------------------------------------------
# domain
# ---------------------------------------------------------------------------


def test_ceiling_is_the_smaller_of_ratio_and_absolute() -> None:
    quota = WaterQuota(grade="C30", version=1, recycled_ratio=0.3, max_litres=500)
    ceiling = quota.ceiling_for(1050)
    assert ceiling.ratio_bound_l == pytest.approx(315.0)
    assert ceiling.absolute_bound_l == 500.0
    assert ceiling.limit_l == pytest.approx(315.0)
    assert ceiling.bound_kind == "ratio"

    tight = WaterQuota(grade="C30", version=1, recycled_ratio=0.3, max_litres=100)
    assert tight.ceiling_for(1050).limit_l == 100.0
    assert tight.ceiling_for(1050).bound_kind == "absolute"


def test_versions_are_immutable_and_dense_per_grade() -> None:
    table = QuotaTable()
    first = table.publish("C30", recycled_ratio=0.3, max_litres=500)
    second = table.publish("C30", recycled_ratio=0.6, max_litres=5000)
    assert first.version == 1 and second.version == 2
    assert first.ceiling_for(1050).limit_l == pytest.approx(315.0)
    assert table.history("C30")[-1].version == 2
    assert [q.describe()["version"] for q in table.history("C30")] == [1, 2]


def test_unconfigured_grade_is_closed_not_unlimited() -> None:
    ceiling = current_or_closed(QuotaTable(), "C30", batch_water_l=1050)
    assert ceiling.limit_l == 0.0
    assert ceiling.bound_kind == "unconfigured"


def test_quota_rejects_bad_ratio() -> None:
    with pytest.raises(ValidationError):
        WaterQuota(grade="C30", version=1, recycled_ratio=1.5, max_litres=500)


# ---------------------------------------------------------------------------
# pinning, enforcement and split accounting
# ---------------------------------------------------------------------------


def _prepared(runtime: PlantRuntime, *, ratio: float = 0.3, cap: float = 500) -> None:
    run(runtime, "water.quota.publish", {"grade": "C30", "recycled_ratio": ratio, "max_litres": cap})
    batch_and_weigh(runtime)  # 6 m3 C30 -> 1050 L batch water, pinned to v1


def test_batch_is_pinned_to_the_quota_current_at_start(runtime: PlantRuntime) -> None:
    _prepared(runtime)
    quota = runtime.quotas.batch_quota("B-001")
    assert quota["ceiling"]["version"] == 1
    assert quota["ceiling"]["limit_l"] == pytest.approx(315.0)
    assert quota["remaining_recycled_l"] == pytest.approx(315.0)


def test_recycled_draw_is_stopped_on_the_spot_and_names_the_limit(
    runtime: PlantRuntime,
) -> None:
    _prepared(runtime)
    run(runtime, "water.draw", {"kind": RECYCLED, "batch_id": "B-001", "litres": 300, "idem": "d1"})
    with pytest.raises(OverLimitError) as blocked:
        run(runtime, "water.draw", {"kind": RECYCLED, "batch_id": "B-001", "litres": 16, "idem": "d2"})
    ctx = blocked.value.context
    assert ctx["bound_kind"] == "ratio"
    assert ctx["limit_l"] == pytest.approx(315.0)
    assert ctx["remaining_l"] == pytest.approx(15.0)
    # refused draw is not recorded: usage stays at 300
    assert runtime.quotas.ledger.usage_for("B-001")[RECYCLED] == pytest.approx(300.0)
    assert runtime.alarms.count() == 1


def test_absolute_ceiling_blocks_and_is_named(runtime: PlantRuntime) -> None:
    _prepared(runtime, ratio=0.9, cap=200)
    with pytest.raises(OverLimitError) as blocked:
        run(runtime, "water.draw", {"kind": RECYCLED, "batch_id": "B-001", "litres": 201, "idem": "d1"})
    assert blocked.value.context["bound_kind"] == "absolute"
    assert blocked.value.context["limit_l"] == 200.0


def test_recycled_and_fresh_water_are_counted_separately(runtime: PlantRuntime) -> None:
    _prepared(runtime)
    run(runtime, "water.draw", {"kind": RECYCLED, "batch_id": "B-001", "litres": 315, "idem": "r"})
    run(runtime, "water.draw", {"kind": FRESH, "batch_id": "B-001", "litres": 735, "idem": "f"})
    usage = runtime.quotas.ledger.usage_for("B-001")
    assert usage[RECYCLED] == pytest.approx(315.0)
    assert usage[FRESH] == pytest.approx(735.0)
    totals = runtime.quotas.ledger.totals()
    assert totals["recycled_l"] == 315.0 and totals["fresh_l"] == 735.0


def test_fresh_water_is_not_quota_limited(runtime: PlantRuntime) -> None:
    _prepared(runtime)
    result = run(runtime, "water.draw", {"kind": FRESH, "batch_id": "B-001", "litres": 5000, "idem": "f"})
    assert result["entry"]["fresh_after_l"] == 5000.0


# ---------------------------------------------------------------------------
# idempotency
# ---------------------------------------------------------------------------


def test_ledger_write_is_idempotent(runtime: PlantRuntime) -> None:
    _prepared(runtime)
    payload = {"kind": RECYCLED, "batch_id": "B-001", "litres": 100, "idem": "same"}
    first = run(runtime, "water.draw", payload)
    usage_records_before = [r for r in runtime.store.committed_records() if r.kind == "water.usage"]
    second = run(runtime, "water.draw", payload)
    usage_records_after = [r for r in runtime.store.committed_records() if r.kind == "water.usage"]
    assert first["seq"] == second["seq"]
    assert len(usage_records_before) == len(usage_records_after)
    assert runtime.quotas.ledger.totals()["recycled_l"] == 100.0
    assert runtime.quotas.ledger.totals()["draws"] == 1


def test_rinse_write_is_idempotent(runtime: PlantRuntime) -> None:
    _prepared(runtime)
    payload = {"litres": 50, "idem": "wash"}
    first = run(runtime, "water.rinse", payload)
    before = [r for r in runtime.store.committed_records() if r.kind == "water.rinse"]
    second = run(runtime, "water.rinse", payload)
    after = [r for r in runtime.store.committed_records() if r.kind == "water.rinse"]
    assert first["seq"] == second["seq"]
    assert len(before) == len(after)
    assert runtime.quotas.ledger.totals()["rinse_l"] == 50.0


def test_idem_key_reused_across_actions_is_rejected(runtime: PlantRuntime) -> None:
    _prepared(runtime)
    run(runtime, "water.draw", {"kind": RECYCLED, "batch_id": "B-001", "litres": 10, "idem": "k"})
    with pytest.raises(DuplicateError):
        run(runtime, "water.rinse", {"litres": 10, "idem": "k"})


# ---------------------------------------------------------------------------
# shutdown rinse sits outside the allowance
# ---------------------------------------------------------------------------


def test_shutdown_rinse_is_metered_but_not_quota_counted(runtime: PlantRuntime) -> None:
    _prepared(runtime)
    run(runtime, "water.draw", {"kind": RECYCLED, "batch_id": "B-001", "litres": 315, "idem": "r"})
    rinse = run(runtime, "water.rinse", {"litres": 400, "idem": "wash"})
    assert rinse["rinse"]["quota_counted"] is False
    totals = runtime.quotas.ledger.totals()
    assert totals["recycled_l"] == 315.0
    assert totals["rinse_l"] == 400.0
    # rinse never consumes the recycled allowance
    assert runtime.quotas.batch_quota("B-001")["used_recycled_l"] == 315.0


# ---------------------------------------------------------------------------
# version migration: running batch finishes on the old table
# ---------------------------------------------------------------------------


def test_published_change_migrates_later_batches_only(runtime: PlantRuntime) -> None:
    _prepared(runtime)
    run(runtime, "water.draw", {"kind": RECYCLED, "batch_id": "B-001", "litres": 315, "idem": "r1"})
    run(runtime, "water.quota.publish", {"grade": "C30", "recycled_ratio": 0.6, "max_litres": 5000})
    # the batch already running stays on v1
    with pytest.raises(OverLimitError):
        run(runtime, "water.draw", {"kind": RECYCLED, "batch_id": "B-001", "litres": 1, "idem": "r2"})
    # a later batch picks up v2
    run(runtime, "batch.declare", {"batch_id": "B-002", "quantity": 6})
    ceiling = runtime.quotas.batch_quota("B-002")["ceiling"]
    assert ceiling["version"] == 2
    assert ceiling["limit_l"] == pytest.approx(630.0)
    run(runtime, "water.draw", {"kind": RECYCLED, "batch_id": "B-002", "litres": 630, "idem": "r3"})
    rows = runtime.quotas.ledger.by_grade_version()
    assert {(row["grade"], row["version"]) for row in rows} == {("C30", 1), ("C30", 2)}


def test_unconfigured_grade_bans_recycled_but_allows_fresh(runtime: PlantRuntime) -> None:
    publish_and_select(runtime)
    run(runtime, "batch.declare", {"batch_id": "B-1", "quantity": 6})
    with pytest.raises(OverLimitError) as blocked:
        run(runtime, "water.draw", {"kind": RECYCLED, "batch_id": "B-1", "litres": 1, "idem": "r"})
    assert blocked.value.context["bound_kind"] == "unconfigured"
    run(runtime, "water.draw", {"kind": FRESH, "batch_id": "B-1", "litres": 1050, "idem": "f"})


# ---------------------------------------------------------------------------
# recipe validation shares the quota gate
# ---------------------------------------------------------------------------


def test_recipe_validation_reports_the_quota_gate(runtime: PlantRuntime) -> None:
    run(runtime, "water.quota.publish", {"grade": "C30", "recycled_ratio": 0.3, "max_litres": 500})
    publish_and_select(runtime)
    recipe = runtime.recipes.current()
    findings = {f.code: f for f in quota_findings(recipe, runtime.quotas.table)}
    assert findings["quota_published"].ok is True
    assert findings["quota_within_water"].ok is True


# ---------------------------------------------------------------------------
# shift is a reporting dimension, never a recomputation basis
# ---------------------------------------------------------------------------


def test_totals_split_per_shift_but_ceiling_is_station_wide(runtime: PlantRuntime) -> None:
    _prepared(runtime)
    run(runtime, "water.draw", {"kind": RECYCLED, "batch_id": "B-001", "litres": 100, "idem": "a", "shift": "day"})
    run(runtime, "water.draw", {"kind": FRESH, "batch_id": "B-001", "litres": 50, "idem": "b", "shift": "night"})
    view = runtime.quotas.quota_view()
    assert view["basis"] == "grade"
    assert view["recomputed_per_shift"] is False
    shifts = {row["shift"]: row for row in view["by_shift"]}
    assert shifts["day"]["recycled_l"] == 100.0
    assert shifts["night"]["fresh_l"] == 50.0


# ---------------------------------------------------------------------------
# audit + restart
# ---------------------------------------------------------------------------


def test_quota_and_usage_reach_the_audit_stream(runtime: PlantRuntime) -> None:
    _prepared(runtime)
    run(runtime, "water.draw", {"kind": RECYCLED, "batch_id": "B-001", "litres": 10, "idem": "r"})
    names = runtime.bus.published()
    assert "water.quota_published" in names
    assert "water.recycled_drawn" in names
    kinds = {record.kind for record in runtime.store.committed_records()}
    assert {"water.quota", "water.pin", "water.usage"} <= kinds


def test_restart_restores_quota_pin_usage_and_idempotency(tmp_path) -> None:
    from mixplant.config import StationConfig

    config = StationConfig(data_dir=tmp_path / "state")
    first = PlantRuntime(config)
    first.bootstrap()
    run(first, "water.quota.publish", {"grade": "C30", "recycled_ratio": 0.3, "max_litres": 500})
    batch_and_weigh(first)
    run(first, "water.draw", {"kind": RECYCLED, "batch_id": "B-001", "litres": 200, "idem": "d1"})
    run(first, "water.rinse", {"litres": 60, "idem": "w1"})
    first.close()

    second = PlantRuntime(config)
    second.bootstrap()
    assert [q.version for q in second.quotas.table.history("C30")] == [1]
    assert second.quotas.ledger.require_pin("B-001").version == 1
    assert second.quotas.ledger.usage_for("B-001")[RECYCLED] == 200.0
    assert second.quotas.ledger.totals()["rinse_l"] == 60.0
    replay = run(second, "water.draw", {"kind": RECYCLED, "batch_id": "B-001", "litres": 200, "idem": "d1"})
    assert replay["entry"]["recycled_after_l"] == 200.0
    assert second.quotas.ledger.totals()["draws"] == 1
    second.close()
