"""Recycled-water quotas, the pinned water ledger and their enforcement."""

from __future__ import annotations

from .ledger import BatchPin, RinseEntry, WaterEntry, WaterLedger
from .quota import (
    FRESH,
    RECYCLED,
    WATER_KINDS,
    QuotaCeiling,
    QuotaTable,
    WaterQuota,
    current_or_closed,
    forbid_recycled,
)
from .service import QuotaService
from .validation import quota_findings, require_quota_consistent

__all__ = [
    "FRESH",
    "RECYCLED",
    "WATER_KINDS",
    "BatchPin",
    "QuotaCeiling",
    "QuotaService",
    "QuotaTable",
    "RinseEntry",
    "WaterEntry",
    "WaterLedger",
    "WaterQuota",
    "current_or_closed",
    "forbid_recycled",
    "quota_findings",
    "require_quota_consistent",
]
