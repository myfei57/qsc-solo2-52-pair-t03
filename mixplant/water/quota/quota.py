"""Versioned recycled-water quotas, one ceiling per concrete grade.

A quota answers exactly one question for a grade: how much recycled water a
batch may carry before it must be topped up with fresh water.  Quotas are
versioned by grade: publishing a change appends a new immutable version and
never rewrites an old one.  A running batch is pinned to the version that was
current when the batch started, so a change of the table is a migration: the
batch already in the mixer finishes under the old table while later batches
pick up the new one.

The ceiling is deliberately *not* recomputed per shift.  A grade carries one
station-wide table; the shift is only a reporting dimension on the ledger.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from ...errors import ValidationError
from ...recipe.grade import grade_for

RECYCLED = "recycled"
FRESH = "fresh"
WATER_KINDS: tuple[str, ...] = (RECYCLED, FRESH)

BOUND_RATIO = "ratio"
BOUND_ABSOLUTE = "absolute"
BOUND_UNCONFIGURED = "unconfigured"


@dataclass(frozen=True, slots=True)
class WaterQuota:
    """One immutable recycled-water ceiling for one grade.

    ``recycled_ratio`` bounds the recycled share of the batch water as a
    fraction in ``[0, 1]``; ``max_litres`` is an absolute per-batch ceiling.
    The enforced limit is the smaller of the two derived bounds.
    """

    grade: str
    version: int
    recycled_ratio: float
    max_litres: float

    def __post_init__(self) -> None:
        grade_for(self.grade)
        if self.version < 1:
            raise ValidationError("quota version starts at one", grade=self.grade, version=self.version)
        if not 0.0 <= self.recycled_ratio <= 1.0:
            raise ValidationError(
                "recycled ratio must be a fraction", grade=self.grade, ratio=self.recycled_ratio
            )
        if self.max_litres < 0:
            raise ValidationError(
                "quota ceiling must not be negative", grade=self.grade, max_litres=self.max_litres
            )

    @classmethod
    def closed(cls, grade: str) -> "WaterQuota":
        """The zero allowance a grade carries until its first quota is published."""

        quota = object.__new__(cls)
        object.__setattr__(quota, "grade", grade_for(grade).code)
        object.__setattr__(quota, "version", 0)
        object.__setattr__(quota, "recycled_ratio", 0.0)
        object.__setattr__(quota, "max_litres", 0.0)
        return quota

    def ceiling_for(self, batch_water_l: float) -> "QuotaCeiling":
        """Return the bound this version imposes on one batch.

        The ratio bound and the absolute bound are both reported so a refusal
        can say *which* upper limit stopped the draw.
        """

        if batch_water_l < 0:
            raise ValidationError(
                "batch water must not be negative", grade=self.grade, batch_water_l=batch_water_l
            )
        if self.version == 0:
            return QuotaCeiling(
                grade=self.grade,
                version=0,
                batch_water_l=round(batch_water_l, 4),
                ratio=0.0,
                ratio_bound_l=0.0,
                absolute_bound_l=0.0,
                limit_l=0.0,
                bound_kind=BOUND_UNCONFIGURED,
            )
        ratio_bound = round(self.recycled_ratio * batch_water_l, 4)
        absolute_bound = round(self.max_litres, 4)
        if ratio_bound <= absolute_bound:
            bound = ratio_bound
            bound_kind = BOUND_RATIO
        else:
            bound = absolute_bound
            bound_kind = BOUND_ABSOLUTE
        return QuotaCeiling(
            grade=self.grade,
            version=self.version,
            batch_water_l=round(batch_water_l, 4),
            ratio=self.recycled_ratio,
            ratio_bound_l=ratio_bound,
            absolute_bound_l=absolute_bound,
            limit_l=bound,
            bound_kind=bound_kind,
        )

    def describe(self) -> dict[str, Any]:
        return {
            "grade": self.grade,
            "version": self.version,
            "recycled_ratio": self.recycled_ratio,
            "max_litres": self.max_litres,
        }

    def to_document(self) -> dict[str, Any]:
        return dict(self.describe())

    @classmethod
    def from_document(cls, document: Mapping[str, Any]) -> "WaterQuota":
        return cls(
            grade=str(document["grade"]),
            version=int(document["version"]),
            recycled_ratio=float(document["recycled_ratio"]),
            max_litres=float(document["max_litres"]),
        )


@dataclass(frozen=True, slots=True)
class QuotaCeiling:
    """The enforced limit for one batch under one quota version."""

    grade: str
    version: int
    batch_water_l: float
    ratio: float
    ratio_bound_l: float
    absolute_bound_l: float
    limit_l: float
    bound_kind: str

    def describe(self) -> dict[str, Any]:
        return {
            "grade": self.grade,
            "version": self.version,
            "batch_water_l": self.batch_water_l,
            "recycled_ratio": self.ratio,
            "ratio_bound_l": self.ratio_bound_l,
            "absolute_bound_l": self.absolute_bound_l,
            "limit_l": self.limit_l,
            "bound_kind": self.bound_kind,
        }


class QuotaTable:
    """Every published quota version, kept per grade in publish order."""

    __slots__ = ("_by_grade",)

    def __init__(self) -> None:
        self._by_grade: dict[str, list[WaterQuota]] = {}

    def publish(self, grade: str, *, recycled_ratio: float, max_litres: float) -> WaterQuota:
        """Append a new version for ``grade``; versions are dense per grade."""

        code = grade_for(grade).code
        history = self._by_grade.setdefault(code, [])
        quota = WaterQuota(
            grade=code,
            version=len(history) + 1,
            recycled_ratio=recycled_ratio,
            max_litres=max_litres,
        )
        history.append(quota)
        return quota

    def adopt(self, quota: WaterQuota) -> WaterQuota:
        """Adopt a version read back from the stream after a restart."""

        history = self._by_grade.setdefault(quota.grade, [])
        if any(existing.version == quota.version for existing in history):
            return quota
        history.append(quota)
        history.sort(key=lambda item: item.version)
        return quota

    def with_candidate(self, candidate: WaterQuota) -> "QuotaTable":
        """Return a copy that presents ``candidate`` as the grade's current.

        Used to validate a quota before it is committed, without mutating the
        live table.  An existing version may not be re-candidate.
        """

        projected = QuotaTable()
        for grade, history in self._by_grade.items():
            projected._by_grade[grade] = list(history)
        target = projected._by_grade.setdefault(candidate.grade, [])
        if any(existing.version == candidate.version for existing in target):
            raise ValidationError(
                "quota version already exists", grade=candidate.grade, version=candidate.version
            )
        target.append(candidate)
        return projected

    def current(self, grade: str) -> WaterQuota:
        """Return the newest published version or refuse when none exists.

        A grade without a published quota is treated as "recycled water not
        permitted" by the caller, never as an implicit unlimited allowance.
        """

        from ...errors import NotFoundError

        history = self._by_grade.get(grade_for(grade).code)
        if not history:
            raise NotFoundError("grade has no recycled-water quota", grade=grade)
        return history[-1]

    def version_of(self, grade: str, version: int) -> WaterQuota:
        from ...errors import NotFoundError

        for quota in self._by_grade.get(grade_for(grade).code, ()):
            if quota.version == version:
                return quota
        raise NotFoundError("quota version was never published", grade=grade, version=version)

    def has(self, grade: str) -> bool:
        return bool(self._by_grade.get(grade_for(grade).code))

    def grades(self) -> tuple[str, ...]:
        return tuple(sorted(self._by_grade))

    def history(self, grade: str) -> tuple[WaterQuota, ...]:
        return tuple(self._by_grade.get(grade_for(grade).code, ()))

    def describe(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for grade in self.grades():
            history = self._by_grade[grade]
            rows.append(
                {
                    "grade": grade,
                    "current_version": history[-1].version,
                    "current": history[-1].describe(),
                    "versions": [quota.version for quota in history],
                }
            )
        return rows


def forbid_recycled(grade: str, *, batch_water_l: float) -> QuotaCeiling:
    """Build the zero ceiling used when a grade has no published quota."""

    return WaterQuota.closed(grade).ceiling_for(batch_water_l)


def current_or_closed(table: QuotaTable, grade: str, *, batch_water_l: float) -> QuotaCeiling:
    """Resolve the current ceiling, closing the grade when unconfigured."""

    if table.has(grade):
        return table.current(grade).ceiling_for(batch_water_l)
    return forbid_recycled(grade, batch_water_l=batch_water_l)
