"""Keep recipe validation, the water ledger and the quota table consistent.

A recipe is accepted against a grade only when that grade's current quota can
be explained on top of the recipe's own water factor.  This module is the
single shared check so publishing a recipe, drawing water and reading the
audit trail never disagree about whether a grade is fit for recycled water.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ...errors import ValidationError
from ...recipe.recipe import Recipe
from .quota import QuotaTable


@dataclass(frozen=True, slots=True)
class QuotaFinding:
    """One reason a recipe/quota pairing was accepted or refused."""

    code: str
    ok: bool
    detail: str

    def describe(self) -> dict[str, Any]:
        return {"code": self.code, "ok": self.ok, "detail": self.detail}


def quota_findings(recipe: Recipe, table: QuotaTable) -> list[QuotaFinding]:
    """Return every finding for using ``recipe`` under ``table``.

    A grade without a quota is a valid *closed* state (recycled water banned),
    so ``quota_published`` is reported as information rather than a failure.
    Only a configured quota that contradicts the recipe's own water factor
    blocks publication, keeping recipe validation and the ledger in step.
    """

    grade = recipe.grade.code
    water_factor = float(recipe.factor("water"))
    configured = table.has(grade)
    findings = [
        QuotaFinding(
            code="quota_published",
            ok=True,
            detail=(
                f"grade {grade} carries a published recycled-water quota"
                if configured
                else f"grade {grade} has no quota; recycled water is closed"
            ),
        ),
        QuotaFinding(
            code="recipe_water_factor",
            ok=water_factor > 0,
            detail=f"recipe water factor is {water_factor} L per m3",
        ),
    ]
    if configured:
        quota = table.current(grade)
        ceiling = quota.ceiling_for(water_factor)
        findings.append(
            QuotaFinding(
                code="quota_within_water",
                ok=ceiling.limit_l <= water_factor + 1e-9,
                detail=(
                    f"v{quota.version} caps recycled water at {ceiling.limit_l} L "
                    f"of {water_factor} L per m3 ({ceiling.bound_kind} bound)"
                ),
            )
        )
    return findings


def require_quota_consistent(recipe: Recipe, table: QuotaTable) -> None:
    """Raise when the recipe cannot be batched under the current quota."""

    failed = [finding for finding in quota_findings(recipe, table) if not finding.ok]
    if failed:
        raise ValidationError(
            "recipe is inconsistent with the recycled-water quota",
            grade=recipe.grade.code,
            findings=[finding.describe() for finding in failed],
        )
