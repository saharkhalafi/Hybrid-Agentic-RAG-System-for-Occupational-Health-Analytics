"""Deterministic table classification for the OHSE document pipeline.

The classifier is intentionally rule-based and deterministic.
It must not depend on an LLM or embedding model.

The classifier supports:
- Chemical OEL
- Noise
- Vibration
- Biological exposure
- Regulatory
- Unknown

Both English and Persian document markers are supported.
"""

from __future__ import annotations

import re
from enum import Enum


class ClassifiedTableType(str, Enum):
    CHEMICAL_OEL = "chemical_oel"
    NOISE = "noise"
    VIBRATION = "vibration"
    BIOLOGICAL = "biological_exposure"
    REGULATORY = "regulatory"
    UNKNOWN = "unknown"


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------

_PERSIAN_CHAR_MAP = str.maketrans(
    {
        "ي": "ی",
        "ى": "ی",
        "ك": "ک",
        "ۀ": "ه",
        "ة": "ه",
        "ؤ": "و",
        "إ": "ا",
        "أ": "ا",
        "آ": "ا",
        "ئ": "ی",
    }
)

# Some older extracted PDF/OCR text in this project contains UTF-8
# mojibake such as "Ù†Ø§Ù…" instead of proper Persian.
#
# These replacements MUST be done before str.maketrans because they are
# multi-character sequences.
_MOJIBAKE_REPLACEMENTS = (
    ("ÙŠ", "ی"),
    ("Ù‰", "ی"),
    ("Ùƒ", "ک"),
    ("ÛŒ", "ی"),
    ("Ú©", "ک"),
    ("Û€", "ه"),
    ("Ø©", "ه"),
    ("Ø¤", "و"),
    ("Ø¥", "ا"),
    ("Ø£", "ا"),
    ("Ù±", "ا"),
)


def _normalize(text: str | None) -> str:
    """Normalize Persian, Arabic, English and extracted PDF text."""
    if not text:
        return ""

    value = str(text)

    # ---------------------------------------------------------------
    # 1. Fix known mojibake sequences
    # ---------------------------------------------------------------
    for source, target in _MOJIBAKE_REPLACEMENTS:
        value = value.replace(source, target)

    # ---------------------------------------------------------------
    # 2. Normalize Arabic/Persian characters
    # ---------------------------------------------------------------
    value = value.translate(_PERSIAN_CHAR_MAP)

    # ---------------------------------------------------------------
    # 3. Remove zero-width / BOM characters
    # ---------------------------------------------------------------
    value = value.replace("\u200c", " ")
    value = value.replace("\u200f", " ")
    value = value.replace("\u200e", " ")
    value = value.replace("\ufeff", " ")

    # ---------------------------------------------------------------
    # 4. Normalize common dash characters
    # ---------------------------------------------------------------
    value = value.replace("–", "-")
    value = value.replace("—", "-")
    value = value.replace("−", "-")

    # ---------------------------------------------------------------
    # 5. Normalize whitespace
    # ---------------------------------------------------------------
    value = re.sub(r"\s+", " ", value)

    return value.strip().lower()


# ---------------------------------------------------------------------------
# OEL signatures
# ---------------------------------------------------------------------------

# These are strong Persian header markers that occur in the actual OHSE
# occupational exposure tables.
OEL_REQUIRED_HEADER_TERMS = (
    "ردیف",
    "نام علمی",
    "ماده شیمیایی",
    "وزن مولکولی",
    "نمادها",
    "مبنای تعیین",
)

OEL_EXPOSURE_PARENT_TERMS = (
    "حد مجاز مواجهه شغلی",
    "مواجهه شغلی",
)

OEL_LIMIT_CHILD_TERMS = (
    "twa",
    "stel/c",
)

OEL_CAS_PATTERN = re.compile(
    r"\b\d{2,7}-\d{2}-\d\b"
)


def has_oel_header_signature(text: str | None) -> bool:
    """Detect the strong Persian OEL table-header signature."""
    normalized = _normalize(text)

    if not normalized:
        return False

    required_hits = sum(
        1
        for term in OEL_REQUIRED_HEADER_TERMS
        if _normalize(term) in normalized
    )

    exposure_hit = any(
        _normalize(term) in normalized
        for term in OEL_EXPOSURE_PARENT_TERMS
    )

    twa_hit = "twa" in normalized
    stel_hit = "stel/c" in normalized or "stel" in normalized

    return (
        required_hits >= 3
        and exposure_hit
        and twa_hit
        and stel_hit
    )


def has_chemical_oel_signatures(
    page_text: str | None,
) -> bool:
    """Detect a chemical OEL table using strong domain markers.

    This function intentionally accepts both:
        CAS + TWA + STEL + OEL
    and the richer Persian OHSE table structure.
    """
    normalized = _normalize(page_text)

    if not normalized:
        return False

    # ---------------------------------------------------------------
    # CAS registry number
    # ---------------------------------------------------------------
    has_cas = bool(OEL_CAS_PATTERN.search(normalized))

    # ---------------------------------------------------------------
    # Exposure-limit markers
    # ---------------------------------------------------------------
    has_twa = bool(re.search(r"\btwa\b", normalized))
    has_stel = bool(re.search(r"\bstel(?:/c)?\b", normalized))

    has_exposure_limit_terms = (
        has_twa
        or has_stel
        or bool(re.search(r"\boel\b", normalized))
    )

    # ---------------------------------------------------------------
    # Chemical identity markers
    # ---------------------------------------------------------------
    has_chemical_identity = (
        "cas" in normalized
        or "methanol" in normalized
        or "chemical" in normalized
        or "chemical substance" in normalized
        or "ماده شیمیایی" in normalized
        or "نام علمی" in normalized
    )

    # ---------------------------------------------------------------
    # Strong English/simple OEL signature
    #
    # Example:
    # CAS 67-56-1 Methanol TWA 200 ppm STEL 250 ppm OEL
    # ---------------------------------------------------------------
    simple_english_signature = (
        has_cas
        and has_chemical_identity
        and has_twa
        and has_stel
    )

    if simple_english_signature:
        return True

    # ---------------------------------------------------------------
    # Persian / document-level OEL signature
    # ---------------------------------------------------------------
    has_persian_exposure = any(
        term in normalized
        for term in (
            "حد مجاز مواجهه",
            "مواجهه شغلی",
            "مبنای تعیین",
        )
    )

    return (
        has_cas
        and has_exposure_limit_terms
        and (
            has_chemical_identity
            or has_persian_exposure
        )
    )


# ---------------------------------------------------------------------------
# Noise signatures
# ---------------------------------------------------------------------------

def has_noise_signatures(text: str | None) -> bool:
    """Detect occupational noise tables.

    Supports common acoustic / occupational-noise terminology:
    - LAeq
    - dBA
    - dB(A)
    - dose
    - criterion level
    - exchange rate
    - permissible noise exposure
    - noise
    - Persian noise terminology
    """
    normalized = _normalize(text)

    if not normalized:
        return False

    # ---------------------------------------------------------------
    # Strong acoustic measurement markers
    # ---------------------------------------------------------------
    has_laeq = bool(
        re.search(r"\blaeq\b", normalized)
    )

    has_db = bool(
        re.search(
            r"\bdba\b|\bdb\(a\)\b|\bdb\b",
            normalized,
        )
    )

    has_noise_dose = bool(
        re.search(r"\bdose\b", normalized)
    )

    has_criterion = (
        "criterion level" in normalized
        or "criterion" in normalized
    )

    has_exchange_rate = (
        "exchange rate" in normalized
        or "exchange" in normalized
    )

    has_noise_word = (
        "noise" in normalized
        or "sound level" in normalized
        or "sound pressure" in normalized
    )

    # ---------------------------------------------------------------
    # Persian markers
    # ---------------------------------------------------------------
    has_persian_noise = any(
        term in normalized
        for term in (
            "صدا",
            "آلودگی صوتی",
            "مواجهه با صدا",
            "تراز صدا",
            "حد مجاز صدا",
            "دوز صدا",
        )
    )

    # ---------------------------------------------------------------
    # Strong English occupational-noise signatures
    #
    # Example:
    # LAeq 85 dBA dose 100% criterion level
    # ---------------------------------------------------------------
    if has_laeq and has_db:
        return True

    if has_noise_word and has_db:
        return True

    if has_noise_dose and has_criterion:
        return True

    if has_laeq and has_noise_dose:
        return True

    # ---------------------------------------------------------------
    # Persian noise signature
    # ---------------------------------------------------------------
    if has_persian_noise:
        return True

    return False


# ---------------------------------------------------------------------------
# Vibration signatures
# ---------------------------------------------------------------------------

def has_vibration_signatures(text: str | None) -> bool:
    """Detect occupational vibration tables."""
    normalized = _normalize(text)

    if not normalized:
        return False

    has_vibration_word = (
        "vibration" in normalized
        or "hand-arm vibration" in normalized
        or "whole-body vibration" in normalized
    )

    has_persian_vibration = (
        "ارتعاش" in normalized
        or "لرزش" in normalized
    )

    has_a8 = bool(
        re.search(r"\ba\s*\(\s*8\s*\)", normalized)
    )

    has_vdv = "vdv" in normalized

    has_ms2 = bool(
        re.search(
            r"m/s\s*(?:\^?\s*2|²)",
            normalized,
        )
    )

    # Explicit domain marker
    if has_vibration_word or has_persian_vibration:
        return True

    # Strong occupational vibration measurement combination
    if has_a8 and (has_vdv or has_ms2):
        return True

    return False


# ---------------------------------------------------------------------------
# Biological exposure signatures
# ---------------------------------------------------------------------------

def has_biological_signatures(text: str | None) -> bool:
    """Detect biological exposure tables."""
    normalized = _normalize(text)

    if not normalized:
        return False

    return any(
        term in normalized
        for term in (
            "biological exposure",
            "biological limit",
            "biological monitoring",
            "biological",
            "بیولوژیک",
            "زیستی",
            "مواجهه بیولوژیکی",
            "مواجهه زیستی",
        )
    )


# ---------------------------------------------------------------------------
# Regulatory signatures
# ---------------------------------------------------------------------------

def has_regulatory_signatures(text: str | None) -> bool:
    """Detect regulatory / legal tables."""
    normalized = _normalize(text)

    if not normalized:
        return False

    return any(
        term in normalized
        for term in (
            "regulatory",
            "regulation",
            "regulatory constraint",
            "standard",
            "standards",
            "مقررات",
            "الزامات",
            "استاندارد",
            "حدود قانونی",
        )
    )


# ---------------------------------------------------------------------------
# Main classifier
# ---------------------------------------------------------------------------

def classify_table_text(
    text: str | None,
) -> ClassifiedTableType:
    """Classify a table using deterministic domain rules.

    Classification order is intentional:

        Chemical OEL
            ↓
        Noise
            ↓
        Vibration
            ↓
        Biological
            ↓
        Regulatory
            ↓
        Unknown
    """
    normalized = _normalize(text)

    if not normalized:
        return ClassifiedTableType.UNKNOWN

    # Chemical OEL
    if has_oel_header_signature(normalized):
        return ClassifiedTableType.CHEMICAL_OEL

    if has_chemical_oel_signatures(normalized):
        return ClassifiedTableType.CHEMICAL_OEL

    # Noise
    if has_noise_signatures(normalized):
        return ClassifiedTableType.NOISE

    # Vibration
    if has_vibration_signatures(normalized):
        return ClassifiedTableType.VIBRATION

    # Biological
    if has_biological_signatures(normalized):
        return ClassifiedTableType.BIOLOGICAL

    # Regulatory
    if has_regulatory_signatures(normalized):
        return ClassifiedTableType.REGULATORY

    return ClassifiedTableType.UNKNOWN