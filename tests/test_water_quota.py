"""Grade-scoped, versioned recycled-water quotas and their water ledger."""

from __future__ import annotations

import pytest

from mixplant.errors import DuplicateError, OverLimitError, ValidationError
from mixplant.runtime import PlantRuntime
from mixplant.water.ledger import BATCHING, FLUSH, WaterLedger, WaterLedgerEntry
from mixplant.water.quota import RECLAIMED, WaterQuota, WaterQuotaBook
from tests.support import batch_and_weigh, run, start_mixer


def test_quota_book_keeps_immutable_history() -> None:
    book = WaterQuotaBook()
    first = WaterQuota(grade="C30", version=1, recycled_ratio_percent=20.0, published_tick=1)
    second = WaterQuota(grade="C30", version=2, recycled_ratio_percent=30.0, published_tick=2)
    book.publish(first)
    book.publish(second)

    assert book.require_current("C30").version == 2
    assert book.version("C30", 1).recycled_ratio_percent == 20.0
    with pytest.raises(ValidationError):
        WaterQuota(grade="C30", version=3, recycled_ratio_percent=101.0, published_tick=3)
    with pytest.raises(DuplicateError):
        book.publish(second)


def test_running_batch_keeps_the_quota_version_bound_when_started(runtime: PlantRuntime) -> None:
    run(runtime, "water_quota.publish", {"grade": "C30", "recycled_ratio_percent": 20.0})
    batch_and_weigh(runtime)
    run(runtime, "agg.check", {"actual_total_kg": runtime.agg.describe_targets()["total_kg"] - 1050.0})
    run(runtime, "agg.drop")
    run(runtime, "mix.start")
    run(runtime, "agg.release", {"residual_kg": 3.0})
    run(runtime, "water.open_valve", {"target_l": 1050, "source": RECLAIMED})

    first = run(
        runtime,
        "water.dispense",
        {"litres": 210, "rate_lps": 45, "source": RECLAIMED, "event_id": "w-1", "batch_id": "B-001"},
    )
    assert first["quota_usage"]["quota_version"] == 1
    assert first["quota_usage"]["recycled_limit_l"] == pytest.approx(210.0)

    run(runtime, "water_quota.publish", {"grade": "C30", "recycled_ratio_percent": 40.0})

    with pytest.raises(OverLimitError) as blocked:
        run(
            runtime,
            "water.dispense",
            {"litres": 1, "rate_lps": 45, "source": RECLAIMED, "event_id": "w-2", "batch_id": "B-001"},
        )
    assert blocked.value.context["limit_code"] == "recycled_water_grade_quota"
    assert blocked.value.context["quota_version"] == 1
    assert blocked.value.context["limit_l"] == pytest.approx(210.0)
    assert blocked.value.context["grade"] == "C30"

    run(runtime, "water_quota.publish", {"grade": "C30", "recycled_ratio_percent": 50.0})
    run(runtime, "batch.declare", {"batch_id": "B-002", "quantity": 6.0})
    next_binding = runtime.water.ledger.binding("B-002")
    assert next_binding.quota_version == 3
    assert next_binding.recycled_limit_l == pytest.approx(525.0)

    run(
        runtime,
        "water.dispense",
        {"litres": 840, "rate_lps": 45, "source": "fresh", "event_id": "w-3", "batch_id": "B-001"},
    )
    usage = runtime.water.ledger.batch_usage("B-001")
    assert usage["recycled_l"] == pytest.approx(210.0)
    assert usage["fresh_l"] == pytest.approx(840.0)
    assert usage["scope"] == "grade"


def test_repeated_ledger_write_is_idempotent(runtime: PlantRuntime) -> None:
    run(runtime, "water_quota.publish", {"grade": "C30", "recycled_ratio_percent": 50.0})
    start_mixer(runtime)
    payload = {"litres": 100, "rate_lps": 45, "source": RECLAIMED, "event_id": "same", "batch_id": "B-001"}

    first = run(runtime, "water.dispense", payload)
    second = run(runtime, "water.dispense", payload)
    assert second["replayed"] is True
    assert runtime.water.ledger.batch_usage("B-001")["recycled_l"] == pytest.approx(100.0)
    assert second["ledger"]["event_id"] == first["ledger"]["event_id"]

    with pytest.raises(DuplicateError):
        run(runtime, "water.dispense", {**payload, "litres": 101})


def test_flush_and_prime_do_not_consume_grade_quota(runtime: PlantRuntime) -> None:
    run(runtime, "water_quota.publish", {"grade": "C30", "recycled_ratio_percent": 20.0})
    batch_and_weigh(runtime)
    run(runtime, "agg.check", {"actual_total_kg": runtime.agg.describe_targets()["total_kg"] - 1050.0})
    run(runtime, "agg.drop")
    run(runtime, "mix.start")
    run(runtime, "agg.release", {"residual_kg": 3.0})
    run(
        runtime,
        "water.dispense",
        {"litres": 210, "rate_lps": 45, "source": RECLAIMED, "event_id": "b-1", "batch_id": "B-001"},
    )
    flush = run(
        runtime,
        "water.flush",
        {"litres": 500, "rate_lps": 30, "source": RECLAIMED, "event_id": "f-1"},
    )
    prime = run(runtime, "water.prime", {"litres": 60, "source": RECLAIMED, "event_id": "p-1"})

    assert flush["purpose"] == FLUSH
    assert flush["ledger"]["excluded_from_quota"] is True
    totals = runtime.water.ledger.totals()
    assert totals["batching_reclaimed_l"] == pytest.approx(210.0)
    assert totals["flush_reclaimed_l"] == pytest.approx(500.0)
    assert totals["prime_reclaimed_l"] == pytest.approx(60.0)
    assert prime["ledger"]["excluded_from_quota"] is True


def test_batch_binding_and_usage_survive_restart(tmp_path) -> None:
    from mixplant.config import StationConfig

    config = StationConfig(data_dir=tmp_path / "state", web_dir="/workspace/web")
    first = PlantRuntime(config)
    first.bootstrap()
    run(first, "water_quota.publish", {"grade": "C30", "recycled_ratio_percent": 20.0})
    batch_and_weigh(first)
    run(first, "agg.check", {"actual_total_kg": first.agg.describe_targets()["total_kg"] - 1050.0})
    run(first, "agg.drop")
    run(first, "mix.start")
    run(first, "agg.release", {"residual_kg": 3.0})
    run(
        first,
        "water.dispense",
        {"litres": 210, "rate_lps": 45, "source": RECLAIMED, "event_id": "persist-1", "batch_id": "B-001"},
    )
    first.close()

    second = PlantRuntime(config)
    second.bootstrap()
    usage = second.water.ledger.batch_usage("B-001")
    assert usage["quota_version"] == 1
    assert usage["recycled_limit_l"] == pytest.approx(210.0)
    assert usage["recycled_l"] == pytest.approx(210.0)
    second.close()


def test_console_exposes_grade_scope_and_quota_usage(runtime: PlantRuntime) -> None:
    run(runtime, "water_quota.publish", {"grade": "C30", "recycled_ratio_percent": 20.0})
    batch_and_weigh(runtime)
    water = runtime.state()["subsystems"]["water"]
    assert water["quota_scope"] == "grade"
    assert water["quotas"]["grades"][0]["grade"] == "C30"
    assert water["ledger"]["batches"][0]["batch_id"] == "B-001"
    rollup = run(runtime, "audit.rollup")
    assert rollup["water"]["totals"] == water["ledger"]["totals"]
    assert rollup["water"]["batches"][0]["quota_version"] == 1


def test_recipe_publish_requires_a_current_grade_quota(runtime: PlantRuntime) -> None:
    with pytest.raises(ValidationError):
        run(
            runtime,
            "recipe.publish",
            {
                "name": "mix-a",
                "grade": "C30",
                "factors": {"sand": 760.0, "cement": 330.0, "water": 175.0},
            },
        )


def test_shift_scoped_quota_parameters_are_refused(runtime: PlantRuntime) -> None:
    with pytest.raises(ValidationError):
        run(
            runtime,
            "water_quota.publish",
            {"grade": "C30", "recycled_ratio_percent": 20.0, "shift_id": "S-1"},
        )


def test_ledger_entry_is_append_only() -> None:
    ledger = WaterLedger()
    entry = WaterLedgerEntry(
        event_id="x",
        batch_id="B",
        purpose=BATCHING,
        source=RECLAIMED,
        litres=10,
        tick=1,
    )
    ledger.record(entry)
    stored, reused = ledger.record(entry)
    assert reused is True
    assert stored == entry
