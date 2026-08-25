from __future__ import annotations

import copy
import re
from typing import Any


# ============================================================================
# PATTERNS
# ============================================================================

CAS_PATTERN = re.compile(
    r"\[\d{2,7}-\d{2}-\d\]"
)

ENGLISH_TOKEN_PATTERN = re.compile(
    r"[A-Za-z]{4,}"
)

LIMIT_PATTERN = re.compile(
    r"\b(?:ppm|ppb|mg/m3|mg/m³|TWA|STEL|Ceiling|C)\b",
    re.IGNORECASE,
)

SYMBOL_PATTERN = re.compile(
    r"\b(?:A[1-5]|BEI|I|IFV|C|Skin)\b",
    re.IGNORECASE,
)


# ============================================================================
# NORMALIZATION
# ============================================================================

def _normalize_header_text(text: str) -> str:
    """
    Normalize header text only for schema detection.

    IMPORTANT:
    This function does NOT modify stored cell content.

    It exists only because the project currently contains both:
      1. valid Persian UTF-8 text
      2. mojibake / incorrectly decoded Persian text
    """

    if not text:
        return ""

    value = str(text)

    # Normalize whitespace.
    value = re.sub(
        r"\s+",
        " ",
        value,
    ).strip()

    return value


def _header_contains(
    joined: str,
    *terms: str,
) -> bool:
    """
    Return True when at least one semantic header term exists.

    Supports both:
      - correct Persian UTF-8
      - current mojibake representation
    """

    for term in terms:
        if term in joined:
            return True

    return False


# ============================================================================
# BASIC HELPERS
# ============================================================================

def _cell_text(
    cell: dict[str, Any],
) -> str:

    texts: list[str] = []

    for block in cell.get("blocks") or []:

        text_block = (
            block.get("textBlock")
            or {}
        )

        text = text_block.get(
            "text",
            "",
        )

        if text:
            texts.append(
                str(text)
            )

    return " ".join(texts).strip()


def _row_cells(
    row: dict[str, Any],
) -> list[dict[str, Any]]:

    return row.get("cells") or []


def _row_text(
    row: dict[str, Any],
) -> list[str]:

    return [
        _cell_text(cell)
        for cell in _row_cells(row)
    ]


def _is_empty_row(
    row: dict[str, Any],
) -> bool:

    return not any(
        text.strip()
        for text in _row_text(row)
    )


def _column_text(
    row: dict[str, Any],
    column: int,
) -> str:

    cells = _row_cells(row)

    if column >= len(cells):
        return ""

    return _cell_text(
        cells[column]
    )


# ============================================================================
# CAS HELPERS
# ============================================================================

def _cas_values(
    text: str,
) -> list[str]:

    return CAS_PATTERN.findall(
        text or ""
    )


def _row_cas_values(
    row: dict[str, Any],
) -> list[str]:

    return _cas_values(
        " ".join(
            _row_text(row)
        )
    )


def _row_has_cas(
    row: dict[str, Any],
) -> bool:

    return bool(
        _row_cas_values(row)
    )


# ============================================================================
# OEL SCHEMA DETECTION
# ============================================================================

def looks_like_oel_six_column_header(
    row: dict[str, Any],
) -> bool:
    """
    Detect the six-column OEL extraction schema.

    IMPORTANT:

    Layer 1 / tests may represent the header in either:

        valid Persian:
            نام علمی ماده شیمیایی
            وزن ملکولی
            حد مجاز مواجهه شغلی
            نمادها

    or mojibake:

            Ù†Ø§Ù… ...
            ÙˆØ²Ù† ...
            Ø­Ø¯ ...
            Ù†Ù…Ø§Ø¯Ù‡Ø§

    Therefore schema detection MUST support both.

    This function only detects the six-column extraction representation.
    The later structural resolver may expand the limit group into
    the final seven-column physical schema.
    """

    cells = _row_cells(row)

    if len(cells) < 6:
        return False

    texts = _row_text(row)

    joined = _normalize_header_text(
        " ".join(texts)
    )

    # ------------------------------------------------------------------
    # Chemical name header
    # ------------------------------------------------------------------

    has_chemical_header = _header_contains(
        joined,

        # Correct Persian
        "نام علمی ماده شیمیایی",
        "نام علمی",
        "ماده شیمیایی",

        # Mojibake variants currently present
        "Ù†Ø§Ù… Ø¹Ù„Ù…ÛŒ Ù…Ø§Ø¯Ù‡",
        "Ù†Ø§Ù… Ø¹Ù„Ù…ÛŒ",
        "Ù…Ø§Ø¯Ù‡ Ø´ÛŒÙ…ÛŒØ§ÛŒÛŒ",
    )

    # ------------------------------------------------------------------
    # Molecular weight
    # ------------------------------------------------------------------

    has_molecular_weight = _header_contains(
        joined,

        # Correct Persian
        "وزن ملکولی",
        "وزن مولکولی",

        # Mojibake
        "ÙˆØ²Ù† Ù…Ù„Ú©ÙˆÙ„ÛŒ",
        "ÙˆØ²Ù† Ù…ÙˆÙ„Ú©ÙˆÙ„ÛŒ",
    )

    # ------------------------------------------------------------------
    # Exposure limit
    # ------------------------------------------------------------------

    has_exposure_limit = _header_contains(
        joined,

        # Correct Persian
        "حد مجاز مواجهه",
        "حد مجاز مواجهه شغلی",

        # Mojibake
        "Ø­Ø¯ Ù…Ø¬Ø§Ø² Ù…ÙˆØ§Ø¬Ù‡Ù‡",
        "Ø­Ø¯ Ù…Ø¬Ø§Ø² Ù…ÙˆØ§Ø¬Ù‡Ù‡ Ø´ØºÙ„ÛŒ",
    )

    # Also accept a compact extracted header such as:
    #
    #   حد مجاز مواجهه شغلی TWA STEL/C
    #
    # where the Persian portion may vary slightly.
    if not has_exposure_limit:

        has_exposure_limit = (
            "TWA" in joined
            and (
                "STEL" in joined
                or "/C" in joined
            )
        )

    # ------------------------------------------------------------------
    # Symbols
    # ------------------------------------------------------------------

    has_symbol_header = _header_contains(
        joined,

        # Correct Persian
        "نمادها",
        "نماد",

        # Mojibake
        "Ù†Ù…Ø§Ø¯Ù‡Ø§",
        "Ù†Ù…Ø§Ø¯",
    )

    return (
        has_chemical_header
        and has_molecular_weight
        and has_exposure_limit
        and has_symbol_header
    )


def looks_like_oel_six_column_row(
    row: dict[str, Any],
) -> bool:

    cells = _row_cells(row)

    if len(cells) < 5:
        return False

    chemical_text = _column_text(
        row,
        4,
    )

    if not chemical_text:
        return False

    # CAS is the strongest chemical identity signal.
    if CAS_PATTERN.search(
        chemical_text
    ):
        return True

    # English chemical name is a useful fallback.
    if ENGLISH_TOKEN_PATTERN.search(
        chemical_text
    ):
        return True

    return False


def is_oel_six_column_table(
    rows: list[dict[str, Any]],
) -> bool:

    if not rows:
        return False

    if not looks_like_oel_six_column_header(
        rows[0]
    ):
        return False

    return any(
        looks_like_oel_six_column_row(
            row
        )
        for row in rows[1:]
    )


# ============================================================================
# ROW CLASSIFICATION
# ============================================================================

def _looks_like_continuation_row(
    row: dict[str, Any],
) -> bool:
    """
    Conservative continuation detector.

    A continuation row is a visual row that contains a fragment of
    information belonging to a nearby chemical record.

    Examples:

        col0 = "health fragment"
        col1..5 = empty

    or:

        col0 = "some rationale"
        col1 = "A4"
        col2 = "0.05 mg/m3"

    HARD RULE:

        A row containing a CAS is NEVER itself a continuation row.
    """

    if not _row_cells(row):
        return False

    # ------------------------------------------------------------------
    # CAS = chemical identity anchor.
    # ------------------------------------------------------------------

    if _row_has_cas(row):
        return False

    texts = _row_text(row)

    non_empty = [
        index
        for index, text in enumerate(texts)
        if text.strip()
    ]

    # Completely empty row.
    if not non_empty:
        return True

    # Only first column populated.
    #
    # This is the exact pattern from the failing regression:
    #
    #     health fragment
    #     ""
    #     ""
    #     ""
    #     ""
    #     ""
    #
    if non_empty == [0]:
        return True

    # A fragment occupying only the first few semantic columns
    # can also be a continuation.
    if max(non_empty) <= 2:
        return True

    return False


# ============================================================================
# MERGING
# ============================================================================

def _merge_cell_text(
    upper: dict[str, Any],
    lower: dict[str, Any],
) -> dict[str, Any]:

    merged = copy.deepcopy(
        upper
    )

    upper_blocks = merged.setdefault(
        "blocks",
        [],
    )

    lower_blocks = copy.deepcopy(
        lower.get("blocks") or []
    )

    upper_text = _cell_text(
        merged
    )

    lower_text = _cell_text(
        lower
    )

    if not lower_text:
        return merged

    if not upper_text:

        merged["blocks"] = (
            lower_blocks
        )

        return merged

    # Preserve both pieces of evidence.
    upper_blocks.extend(
        lower_blocks
    )

    return merged


def _merge_rows(
    upper: dict[str, Any],
    lower: dict[str, Any],
) -> dict[str, Any]:

    upper_cells = _row_cells(
        upper
    )

    lower_cells = _row_cells(
        lower
    )

    max_columns = max(
        len(upper_cells),
        len(lower_cells),
    )

    merged_cells: list[
        dict[str, Any]
    ] = []

    for column in range(
        max_columns
    ):

        if column >= len(
            upper_cells
        ):

            merged_cells.append(
                copy.deepcopy(
                    lower_cells[column]
                )
            )

            continue

        if column >= len(
            lower_cells
        ):

            merged_cells.append(
                copy.deepcopy(
                    upper_cells[column]
                )
            )

            continue

        merged_cells.append(
            _merge_cell_text(
                upper_cells[column],
                lower_cells[column],
            )
        )

    merged = copy.deepcopy(
        upper
    )

    merged["cells"] = (
        merged_cells
    )

    return merged


# ============================================================================
# LOGICAL ROW RECONSTRUCTION
# ============================================================================

def reconstruct_oel_logical_rows(
    rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """
    Reconstruct logical OEL rows from visual rows.

    Example:

        visual row N:

            health fragment
            ""
            ""
            ""
            ""
            ""

        visual row N+1:

            ""
            A4
            0.05 mg/m3
            222.68
            Acetamiprid [135410-20-7]
            4

    becomes ONE logical row:

            health fragment
            A4
            0.05 mg/m3
            222.68
            Acetamiprid [135410-20-7]
            4

    The CAS row remains the identity anchor.

    No numeric values are generated.
    No values are moved between unrelated columns.
    """

    if not rows:
        return []

    source_rows = copy.deepcopy(
        rows
    )

    # Header remains untouched.
    result: list[
        dict[str, Any]
    ] = [
        source_rows[0]
    ]

    pending_continuations: list[
        dict[str, Any]
    ] = []

    for row in source_rows[1:]:

        # ================================================================
        # EMPTY VISUAL ROW
        # ================================================================

        if _is_empty_row(row):

            pending_continuations.append(
                row
            )

            continue

        # ================================================================
        # CAS-BEARING ROW
        # ================================================================

        if _row_has_cas(row):

            if pending_continuations:

                mergeable = True

                for pending in (
                    pending_continuations
                ):

                    # A pending CAS would mean another identity.
                    if _row_has_cas(
                        pending
                    ):
                        mergeable = False
                        break

                    # Only genuine continuation fragments
                    # may be attached backward.
                    if not _looks_like_continuation_row(
                        pending
                    ):
                        mergeable = False
                        break

                if mergeable:

                    # Start from the CAS-bearing row.
                    merged_row = copy.deepcopy(
                        row
                    )

                    # Apply older continuation fragments first.
                    #
                    # We want:
                    #
                    # continuation_1
                    # continuation_2
                    # CAS row
                    #
                    # so the textual evidence order is preserved.
                    for continuation in reversed(
                        pending_continuations
                    ):

                        merged_row = _merge_rows(
                            continuation,
                            merged_row,
                        )

                    result.append(
                        merged_row
                    )

                    pending_continuations.clear()

                    continue

                # Unsafe pending evidence.
                result.extend(
                    pending_continuations
                )

                pending_continuations.clear()

            # Normal CAS-bearing chemical row.
            result.append(
                row
            )

            continue

        # ================================================================
        # CONTINUATION ROW
        # ================================================================

        if _looks_like_continuation_row(
            row
        ):

            pending_continuations.append(
                row
            )

            continue

        # ================================================================
        # UNKNOWN / NORMAL ROW
        # ================================================================

        # We cannot safely associate pending fragments with this row.
        result.extend(
            pending_continuations
        )

        pending_continuations.clear()

        result.append(
            row
        )

    # ====================================================================
    # DANGLING CONTINUATIONS
    # ====================================================================

    result.extend(
        pending_continuations
    )

    return result


# ============================================================================
# PUBLIC API
# ============================================================================

def reconstruct_logical_rows(
    rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:

    if not rows:
        return []

    # ------------------------------------------------------------------
    # IMPORTANT:
    #
    # The schema gate is intentionally kept here.
    #
    # It prevents this generic reconstruction module from modifying
    # arbitrary tables.
    # ------------------------------------------------------------------

    if not is_oel_six_column_table(
        rows
    ):

        return copy.deepcopy(
            rows
        )

    return reconstruct_oel_logical_rows(
        rows
    )