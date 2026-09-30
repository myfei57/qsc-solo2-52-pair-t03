"""Structural checks a recipe must pass before it can be published."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..config import Limits
from .recipe import Recipe


@dataclass(frozen=True, slots=True)
class ValidationFinding:
    """One reason a recipe was accepted or refused."""

    code: str
    ok: bool
    detail: str

    def describe(self) -> dict[str, Any]:
        return {"code": self.code, "ok": self.ok, "detail": self.detail}


def validate_recipe(
    recipe: Recipe,
    limits: Limits,
    quota: Any | None = None,
    *,
    quota_required: bool = False,
) -> list[ValidationFinding]:
    """Return every structural finding for ``recipe``."""

    quota_exists = quota is not None
    quota_version = 0 if quota is None else int(quota.version)
    quota_ratio = 0.0 if quota is None else float(quota.recycled_ratio_percent)
    quota_ok = (quota_exists or not quota_required) and 0.0 <= quota_ratio <= 100.0
    findings = [
        ValidationFinding(
            code="materials",
            ok=bool(recipe.factors),
            detail=f"{len(recipe.factors)} material factors declared",
        ),
        ValidationFinding(
            code="mix_time",
            ok=recipe.mix_time_ticks >= 1,
            detail=f"mixing window is {recipe.mix_time_ticks} ticks",
        ),
        ValidationFinding(
            code="water_factor",
            ok=recipe.factor_or_zero("water") > 0,
            detail=f"mixing water factor is {recipe.factor_or_zero('water')} L/m3",
        ),
        ValidationFinding(
            code="moisture_reference",
            ok=recipe.moisture_ref_percent <= limits.moisture_max_percent,
            detail=f"moisture reference {recipe.moisture_ref_percent}% against {limits.moisture_max_percent}%",
        ),
        ValidationFinding(
            code="water_quota",
            ok=quota_ok and recipe.factor_or_zero("water") > 0,
            detail=(
                f"grade quota v{quota_version} allows {quota_ratio}% recycled mixing water"
                if quota_exists
                else "no current grade quota is available"
            ),
        ),
    ]
    return findings


def require_valid(findings: list[ValidationFinding]) -> None:
    """Raise when any finding failed."""

    from ..errors import ValidationError

    failed = [finding for finding in findings if not finding.ok]
    if failed:
        raise ValidationError(
            "recipe failed validation",
            findings=[finding.describe() for finding in failed],
        )
