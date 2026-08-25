"""Structured no-data reason codes for UX and HITL prioritization."""

from __future__ import annotations

from enum import Enum


class NoDataReason(str, Enum):
    UNKNOWN_CHEMICAL = "unknown_chemical"
    PENDING_PROMOTION = "legacy_only_pending_promotion"


NO_DATA_MESSAGE_FA = {
    NoDataReason.UNKNOWN_CHEMICAL: (
        "اطلاعات کافی برای پاسخ قطعی در داده‌های موجود پیدا نشد."
    ),
    NoDataReason.PENDING_PROMOTION: (
        "داده‌ی تأییدشده برای این ماده هنوز در دسترس نیست."
    ),
}


def parse_no_data_reason(raw: str | None) -> NoDataReason | None:
    if not raw:
        return None
    if raw.startswith("no_data:"):
        raw = raw.split(":", 1)[1]
    try:
        return NoDataReason(raw)
    except ValueError:
        return None
