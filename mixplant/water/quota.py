"""Versioned recycled-water quotas keyed by concrete grade.

The quota scope is intentionally the grade for the whole station.  A shift is
only an accounting period, so it never changes the ceiling that protects a
recipe.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from ..errors import DuplicateError, NotFoundError, ValidationError
from ..recipe.grade import grade_for

QUOTA_SCOPE = "grade"
FRESH = "fresh"
RECLAIMED = "reclaimed"
WATER_KINDS = (FRESH, RECLAIMED)


@dataclass(frozen=True, slots=True)
class WaterQuota:
    """One immutable version of a grade's recycled-water ceiling."""

    grade: str
    version: int
    recycled_ratio_percent: float
    published_tick: int

    def __post_init__(self) -> None:
        grade_for(self.grade)
        if self.version < 1:
            raise ValidationError("water quota version must be positive", grade=self.grade, version=self.version)
        if not 0.0 <= self.recycled_ratio_percent <= 100.0:
            raise ValidationError(
                "recycled-water ratio must be a percentage",
                grade=self.grade,
                ratio_percent=self.recycled_ratio_percent,
            )
        if self.published_tick < 0:
            raise ValidationError("quota publication tick must not be negative", grade=self.grade)

    def limit_for(self, total_water_l: float) -> float:
        """Return the recycled-water ceiling for one planned water volume."""

        if total_water_l <= 0:
            raise ValidationError("planned water volume must be positive", total_water_l=total_water_l)
        return round(total_water_l * self.recycled_ratio_percent / 100.0, 4)

    def describe(self) -> dict[str, Any]:
        return {
            "active": True,
            "grade": self.grade,
            "version": self.version,
            "scope": QUOTA_SCOPE,
            "recycled_ratio_percent": self.recycled_ratio_percent,
            "published_tick": self.published_tick,
        }

    def to_document(self) -> dict[str, Any]:
        return dict(self.describe())

    @classmethod
    def from_document(cls, document: Mapping[str, Any]) -> "WaterQuota":
        return cls(
            grade=str(document["grade"]),
            version=int(document["version"]),
            recycled_ratio_percent=float(document["recycled_ratio_percent"]),
            published_tick=int(document.get("published_tick", 0)),
        )


class WaterQuotaBook:
    """Stores every quota version and resolves current or historical grades."""

    __slots__ = ("_versions",)

    def __init__(self) -> None:
        self._versions: dict[str, list[WaterQuota]] = {}

    def publish(self, quota: WaterQuota) -> WaterQuota:
        history = self._versions.setdefault(quota.grade, [])
        if any(existing.version == quota.version for existing in history):
            raise DuplicateError(
                "water quota version was published twice",
                grade=quota.grade,
                version=quota.version,
            )
        history.append(quota)
        return quota

    def adopt(self, quota: WaterQuota) -> WaterQuota:
        """Adopt a quota read back from the journal without rejecting repeats."""

        history = self._versions.setdefault(quota.grade, [])
        if not any(existing.version == quota.version for existing in history):
            history.append(quota)
        return quota

    def next_version(self, grade: str) -> int:
        return self.current_version(grade) + 1

    def current_version(self, grade: str) -> int:
        history = self._versions.get(grade)
        return 0 if not history else history[-1].version

    def current(self, grade: str) -> WaterQuota | None:
        history = self._versions.get(grade)
        return None if not history else history[-1]

    def require_current(self, grade: str) -> WaterQuota:
        quota = self.current(grade)
        if quota is None:
            raise NotFoundError("no recycled-water quota is published for grade", grade=grade)
        return quota

    def version(self, grade: str, version: int) -> WaterQuota:
        for candidate in self._versions.get(grade, ()):
            if candidate.version == version:
                return candidate
        raise NotFoundError("water quota version was never published", grade=grade, version=version)

    def history(self, grade: str) -> tuple[WaterQuota, ...]:
        return tuple(self._versions.get(grade, ()))

    def grades(self) -> tuple[str, ...]:
        return tuple(sorted(self._versions))

    def describe(self) -> dict[str, Any]:
        return {
            "scope": QUOTA_SCOPE,
            "grades": [self.require_current(grade).describe() for grade in self.grades()],
        }
