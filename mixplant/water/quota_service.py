"""Command service for grade-scoped recycled-water quotas."""

from __future__ import annotations

from ..service_base import PlantService
from .quota import WaterQuota, WaterQuotaBook

QUOTA_KIND = "water.quota"
SUBJECT = "water-quota"


class WaterQuotaService(PlantService):
    """Publishes immutable quota versions and keeps their history."""

    origin = "water"
    line = "batch"
    subject = ""

    def __init__(self, context, machine=None) -> None:
        super().__init__(context, machine)
        self.book = WaterQuotaBook()

    def publish_quota(self, *, grade: str, recycled_ratio_percent: float) -> dict[str, Any]:
        """Publish the next version for a grade; later batches use it."""

        version = self.book.next_version(grade)
        subject = f"{SUBJECT}:{grade}"
        self.context.versions.generations.adopt(subject, version - 1)
        self.context.versions.bump(subject, tick=self.context.clock.now())
        quota = WaterQuota(
            grade=grade,
            version=version,
            recycled_ratio_percent=round(recycled_ratio_percent, 4),
            published_tick=self.context.clock.now(),
        )
        self.book.publish(quota)
        self.store.publish(QUOTA_KIND, self.origin, quota.version, quota.describe())
        self.emit("water.quota_published", quota.describe())
        return {"quota": quota.describe()}

    def adopt(self, quota: WaterQuota) -> None:
        """Adopt a quota read back during restart recovery."""

        self.book.adopt(quota)
        self.context.versions.generations.adopt(f"{SUBJECT}:{quota.grade}", quota.version)

    def current_for(self, grade: str) -> WaterQuota | None:
        return self.book.current(grade)

    def history(self, grade: str) -> list[dict[str, Any]]:
        return [quota.describe() for quota in self.book.history(grade)]

    def status(self) -> dict[str, Any]:
        return self.book.describe()
