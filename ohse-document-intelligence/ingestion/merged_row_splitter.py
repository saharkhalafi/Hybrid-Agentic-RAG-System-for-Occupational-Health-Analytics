#merged_row_splitter.py
from __future__ import annotations

import copy
import math
import re
from dataclasses import dataclass
from typing import Any

from pipeline_contracts.numeric_integrity import parse_slash_decimal


# ============================================================================
# CAS
# ============================================================================

CAS_RE = re.compile(
    r"\b\d{2,7}-\d{2}-\d\b"
)

CAS_BRACKET_RE = re.compile(
    r"\[\s*\d{2,7}-\d{2}-\d\s*\]"
)


@dataclass(frozen=True)
class CasGeometry:
    cas: str
    y: float
    y1: float
    # x0/x1 of the matched word on the page. These are needed to do a
    # *real* horizontal-overlap check against a source cell's bbox in
    # _cas_positions_for_cell() below. Before this field existed, that
    # check was a structural no-op (it always evaluated True) because
    # there was nothing here to compare against.
    x0: float = 0.0
    x1: float = 0.0


# ============================================================================
# NORMALIZATION
# ============================================================================

def _normalize_cas(value: str | None) -> str:
    if not value:
        return ""

    return (
        str(value)
        .strip()
        .strip("[]")
        .replace(" ", "")
    )


def _normalize_word(value: str | None) -> str:
    if not value:
        return ""

    return (
        str(value)
        .replace("\u200c", "")
        .replace("\u200f", "")
        .replace("\u200e", "")
        .strip()
    )


# ============================================================================
# EXTRACT CAS GEOMETRY FROM PDF
# ============================================================================

def extract_cas_geometry(page) -> list[CasGeometry]:
    """
    Extract every CAS occurrence from the PDF together with its physical
    bbox (x0, y0, x1, y1).

    PyMuPDF words:

        x0, y0, x1, y1, text, block_no, line_no, word_no

    IMPORTANT:
        Do not collapse duplicate CAS occurrences here.

    The same CAS can theoretically occur more than once on a page.
    Physical occurrence must remain available to the resolver.
    """

    result: list[CasGeometry] = []

    if page is None:
        return result

    for word in page.get_text("words"):

        if len(word) < 5:
            continue

        x0, y0, x1, y1, text, *_ = word

        text = str(text or "")

        for match in CAS_RE.finditer(text):

            result.append(
                CasGeometry(
                    cas=_normalize_cas(match.group(0)),
                    y=float(y0),
                    y1=float(y1),
                    x0=float(x0),
                    x1=float(x1),
                )
            )

    result.sort(
        key=lambda item: (
            item.y,
            item.y1,
            item.cas,
        )
    )

    return result


# ============================================================================
# CAS POSITION LOOKUP
# ============================================================================

def _all_cas_positions(
    cas: str,
    cas_geometry: list[CasGeometry],
) -> list[CasGeometry]:

    normalized = _normalize_cas(cas)

    return [
        geometry
        for geometry in cas_geometry
        if _normalize_cas(geometry.cas) == normalized
    ]


def cas_positions_for_row(
    cas_values: list[str],
    cas_geometry: list[CasGeometry],
) -> list[tuple[str, float]]:
    """
    Return one physical Y per CAS identity.

    This function is intentionally conservative.

    If the same CAS occurs multiple times on the page, we cannot safely
    choose an arbitrary occurrence. For the current table architecture
    the CAS identity is expected to be unique inside the relevant table
    region, but duplicate occurrences are handled deterministically.

    The first occurrence is only a fallback. The actual row splitter
    (_split_row_by_cas_geometry) prefers geometry resolved against the
    source-cell bbox via _cas_positions_for_cell() and only falls back
    to this function when that bbox-aware lookup finds nothing.
    """

    result: list[tuple[str, float]] = []

    seen: set[str] = set()

    for raw_cas in cas_values:

        cas = _normalize_cas(raw_cas)

        if not cas or cas in seen:
            continue

        matches = _all_cas_positions(
            cas,
            cas_geometry,
        )

        if not matches:
            continue

        result.append(
            (
                cas,
                matches[0].y,
            )
        )

        seen.add(cas)

    return sorted(
        result,
        key=lambda item: item[1],
    )


# ============================================================================
# SOURCE-CELL-AWARE CAS GEOMETRY
# ============================================================================

def _cell_bbox(
    cell: Any,
) -> tuple[float, float, float, float] | None:
    """
    Convert supported bbox representations into:

        x0, y0, x1, y1
    """

    bbox = getattr(
        cell,
        "bbox",
        None,
    )

    if not bbox:
        return None

    if isinstance(bbox, dict):

        x0 = bbox.get("x")
        y0 = bbox.get("y")
        width = bbox.get("width")
        height = bbox.get("height")

        if (
            x0 is None
            or y0 is None
            or width is None
            or height is None
        ):
            return None

        return (
            float(x0),
            float(y0),
            float(x0) + float(width),
            float(y0) + float(height),
        )

    if isinstance(bbox, (list, tuple)) and len(bbox) >= 4:

        return (
            float(bbox[0]),
            float(bbox[1]),
            float(bbox[2]),
            float(bbox[3]),
        )

    return None


def _cas_positions_for_cell(
    cell: Any,
    cas_values: list[str],
    cas_geometry: list[CasGeometry],
    *,
    page=None,
    y_tolerance: float = 8.0,
    x_tolerance: float = 20.0,
) -> list[tuple[str, float]]:
    """
    Resolve each CAS in cas_values to a physical Y, anchored against the
    ACTUAL source cell's bbox rather than "first occurrence on the
    page". This is what disambiguates a CAS that legitimately occurs
    more than once elsewhere on the page (e.g. repeated in a footnote,
    or the same chemical listed twice).

    `page` is currently unused directly (geometry is already supplied
    via cas_geometry), but is accepted so callers can pass it through
    uniformly and so a future page-level fallback lookup can be added
    here without changing the call signature everywhere else.
    """

    bbox = _cell_bbox(cell)

    wanted = {
        _normalize_cas(cas)
        for cas in cas_values
    }

    candidates: list[CasGeometry] = []

    for geometry in cas_geometry:

        if _normalize_cas(geometry.cas) not in wanted:
            continue

        if bbox is None:
            candidates.append(geometry)
            continue

        x0, _, x1, _ = bbox

        # Real horizontal overlap check: the CAS word's own [x0, x1]
        # must intersect the source cell's [x0, x1] (with tolerance),
        # not just "not have a bogus negative Y" as before.
        horizontal_overlap = not (
            geometry.x1 < x0 - x_tolerance
            or geometry.x0 > x1 + x_tolerance
        )

        if not horizontal_overlap:
            continue

        candidates.append(geometry)

    if not candidates:
        return []

    # If source bbox is available, prefer occurrences vertically close to it.
    if bbox is not None:

        _, cell_y0, _, cell_y1 = bbox

        nearby = [
            geometry
            for geometry in candidates
            if (
                geometry.y1 >= cell_y0 - y_tolerance
                and geometry.y <= cell_y1 + y_tolerance
            )
        ]

        if nearby:
            candidates = nearby

    result: list[tuple[str, float]] = []

    for raw_cas in cas_values:

        cas = _normalize_cas(raw_cas)

        matches = [
            geometry
            for geometry in candidates
            if _normalize_cas(geometry.cas) == cas
        ]

        if not matches:
            continue

        # If there are several candidates, choose the one closest to
        # the vertical center of the source cell.
        if bbox is not None:

            _, cell_y0, _, cell_y1 = bbox
            center = (
                cell_y0 + cell_y1
            ) / 2.0

            selected = min(
                matches,
                key=lambda geometry: abs(
                    (
                        geometry.y
                        + geometry.y1
                    ) / 2.0
                    - center
                ),
            )

        else:
            selected = matches[0]

        result.append(
            (
                cas,
                selected.y,
            )
        )

    return sorted(
        result,
        key=lambda item: item[1],
    )


# ============================================================================
# SPLIT DETECTION
# ============================================================================

def split_required(
    cas_values: list[str],
    cas_geometry: list[CasGeometry],
    y_threshold: float = 8.0,
) -> bool:
    """
    Detect whether CAS values extracted into one logical row actually
    belong to multiple physical PDF rows.

    This function only ever DECIDES. It never mutates rows. Mutation
    happens exclusively in _split_row_by_cas_geometry(), which is only
    called once this returns True.
    """

    positions = cas_positions_for_row(
        cas_values,
        cas_geometry,
    )

    if len(positions) <= 1:
        return False

    ys = [
        y
        for _, y in positions
    ]

    return any(
        abs(current - previous)
        > y_threshold
        for previous, current in zip(
            ys,
            ys[1:],
        )
    )


# ============================================================================
# GROUP CAS BY PHYSICAL PDF ROW
# ============================================================================

def group_cas_by_pdf_row(
    cas_values: list[str],
    cas_geometry: list[CasGeometry],
    y_threshold: float = 8.0,
) -> list[list[str]]:
    """
    Group CAS identities by their physical Y position.

    Example:

        A -> 132
        B -> 134
        C -> 163

    =>

        [
            [A, B],
            [C],
        ]
    """

    positions = cas_positions_for_row(
        cas_values,
        cas_geometry,
    )

    if not positions:
        return [
            list(cas_values)
        ]

    groups: list[list[str]] = []

    current_group: list[str] = []
    current_y: float | None = None

    for cas, y in positions:

        if current_y is None:

            current_group = [
                cas
            ]

            current_y = y

            continue

        if abs(
            y - current_y
        ) <= y_threshold:

            current_group.append(
                cas
            )

            # Keep the group center stable when multiple CAS values
            # belong to the same physical row.
            current_y = (
                sum(
                    _cas_y
                    for _cas, _cas_y in positions
                    if _cas in current_group
                )
                / len(current_group)
            )

        else:

            groups.append(
                current_group
            )

            current_group = [
                cas
            ]

            current_y = y

    if current_group:
        groups.append(
            current_group
        )

    return groups


# ============================================================================
# ROW CAS EXTRACTION
# ============================================================================

def _extract_cas_from_text(
    text: str | None,
) -> list[str]:

    if not text:
        return []

    return [
        _normalize_cas(match.group(0))
        for match in CAS_RE.finditer(
            str(text)
        )
    ]


def _extract_row_cas(
    row: list[Any],
) -> list[str]:
    """
    Extract CAS values from all cells while preserving order and uniqueness.
    """

    result: list[str] = []
    seen: set[str] = set()

    for cell in row:

        text = getattr(
            cell,
            "text",
            "",
        )

        for cas in _extract_cas_from_text(
            text
        ):

            if cas in seen:
                continue

            seen.add(cas)
            result.append(cas)

    return result


# ============================================================================
# CAS -> PHYSICAL ROW ASSIGNMENT
# ============================================================================

def _cas_y_map(
    cas_values: list[str],
    cas_geometry: list[CasGeometry],
) -> dict[str, float]:

    positions = cas_positions_for_row(
        cas_values,
        cas_geometry,
    )

    return {
        cas: y
        for cas, y in positions
    }


def _closest_physical_group(
    cas: str,
    cas_y: dict[str, float],
    groups: list[list[str]],
) -> int | None:

    if cas not in cas_y:
        return None

    target_y = cas_y[cas]

    best_index: int | None = None
    best_distance: float | None = None

    for index, group in enumerate(groups):

        ys = [
            cas_y[item]
            for item in group
            if item in cas_y
        ]

        if not ys:
            continue

        distance = min(
            abs(target_y - y)
            for y in ys
        )

        if (
            best_distance is None
            or distance < best_distance
        ):

            best_distance = distance
            best_index = index

    return best_index


# ============================================================================
# CELL TEXT SPLITTING
# ============================================================================

def _split_text_by_cas(
    text: str,
    cas_groups: list[list[str]],
) -> list[str]:
    """
    Split chemical identity text by physical CAS group.

    Crucially, the split boundary is based on CAS occurrence order.
    """

    if not text:
        return [
            ""
            for _ in cas_groups
        ]

    cas_matches = list(
        CAS_RE.finditer(
            text
        )
    )

    if not cas_matches:

        if len(cas_groups) == 1:
            return [text]

        return [
            text
        ] + [
            ""
            for _ in range(
                len(cas_groups) - 1
            )
        ]

    normalized_groups = [
        {
            _normalize_cas(cas)
            for cas in group
        }
        for group in cas_groups
    ]

    def _token_end(match: re.Match[str]) -> int:
        end = match.end()
        while end < len(text) and text[end] in " \t":
            end += 1
        if end < len(text) and text[end] == "]":
            end += 1
        return end

    # Each CAS owns the exact source span from the previous CAS token
    # (including a trailing ']') through this CAS token. Names that
    # precede a CAS stay with that identity; the closing bracket is
    # not leaked into the next chemical.
    token_ends = [
        _token_end(match)
        for match in cas_matches
    ]

    fragments: list[list[str]] = [
        []
        for _ in cas_groups
    ]

    for match_index, match in enumerate(
        cas_matches
    ):
        cas = _normalize_cas(
            match.group(0)
        )
        start = (
            0
            if match_index == 0
            else token_ends[match_index - 1]
        )
        end = (
            len(text)
            if match_index == len(cas_matches) - 1
            else token_ends[match_index]
        )
        fragment = text[start:end]

        for group_index, group_set in enumerate(
            normalized_groups
        ):
            if cas in group_set:
                fragments[group_index].append(
                    fragment
                )
                break

    return [
        "".join(parts).strip()
        for parts in fragments
    ]


# ============================================================================
# WORDS BY PHYSICAL Y
# ============================================================================

def _group_words_by_physical_y(
    page,
    *,
    x0: float,
    x1: float,
    groups: list[list[str]],
    cas_y: dict[str, float],
    y_threshold: float = 8.0,
    y0: float | None = None,
    y1: float | None = None,
) -> dict[int, list[tuple[float, str]]]:
    """
    Assign words inside a source cell to the nearest physical row.

    A word is copied only when its Y is sufficiently close to a physical
    row center.

    This function never fabricates a value.

    NOTE: this function is only ever reached with a real `page` object
    when the caller chain (structural_resolver._split_oel_multi_cas_rows
    -> split_table_rows_by_cas_geometry -> _split_row_by_cas_geometry ->
    _split_nonchemical_cell_by_geometry) actually forwards it. If `page`
    arrives as None here, this returns empty groups for every physical
    row and every non-chemical cell in a split row gets cleared instead
    of geometry-assigned — see the `page=None` early return below.
    """

    result: dict[
        int,
        list[tuple[float, str]]
    ] = {
        index: []
        for index in range(
            len(groups)
        )
    }

    if page is None:
        return result

    physical_y: list[float] = []

    for group in groups:

        ys = [
            cas_y[cas]
            for cas in group
            if cas in cas_y
        ]

        if not ys:
            physical_y.append(
                float("nan")
            )
        else:
            physical_y.append(
                sum(ys) / len(ys)
            )

    for word in page.get_text("words"):

        if len(word) < 5:
            continue

        wx0, wy0, wx1, wy1, text, *_ = word

        text = _normalize_word(text)

        if not text:
            continue

        # Horizontal overlap with source cell.
        if wx1 < x0 or wx0 > x1:
            continue

        # Vertical overlap with the source cell. Mega-cells span more
        # than one physical chemical row; words outside this bbox belong
        # to other table rows and must not be pulled in.
        if y0 is not None and y1 is not None:
            if float(wy1) < y0 or float(wy0) > y1:
                continue

        word_y = (
            float(wy0)
            + float(wy1)
        ) / 2.0

        valid_distances = [
            (
                index,
                abs(
                    word_y - row_y,
                ),
            )
            for index, row_y in enumerate(
                physical_y
            )
            if not math.isnan(row_y)
        ]

        if not valid_distances:
            continue

        group_index, distance = min(
            valid_distances,
            key=lambda item: item[1],
        )

        # Inside the source cell, assign every overlapping word to the
        # nearest CAS row. An 8px cutoff dropped MW/STEL/TWA that sit
        # on the same logical row but not on the CAS baseline.
        result[
            group_index
        ].append(
            (
                word_y,
                float(wx0),
                text,
            )
        )

    return result


def _words_to_text(
    words: list[tuple],
) -> str:

    if not words:
        return ""

    if len(words[0]) >= 3:
        ordered = sorted(
            words,
            key=lambda item: (item[1], item[0]),
        )
        tokens = [item[2] for item in ordered]
    else:
        ordered = sorted(
            words,
            key=lambda item: item[0],
        )
        tokens = [item[1] for item in ordered]

    packed: list[str] = []
    for token in tokens:
        if packed and "mg/m" in packed[-1].lower() and _is_unit_exponent_token(
            token
        ):
            packed[-1] = f"{packed[-1]}{token}"
        else:
            packed.append(token)

    return " ".join(packed).strip()


# ============================================================================
# CELL OWNERSHIP
# ============================================================================

def _bbox_usable_for_word_split(
    bbox: tuple[float, float, float, float] | None,
) -> bool:
    if bbox is None:
        return False
    x0, y0, x1, y1 = bbox
    return (x1 - x0) >= 20.0 and (y1 - y0) >= 10.0


_PERSIAN_TO_ASCII = str.maketrans(
    "۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩",
    "01234567890123456789",
)

_UNIT_EXPONENT_RE = re.compile(
    r"^3(?:\([^)]+\))?$",
    re.IGNORECASE,
)

_X_CLUSTER_GAP = 24.0


def _ascii_digits(token: str) -> str:
    return str(token or "").translate(_PERSIAN_TO_ASCII)


def _is_limit_unit_token(token: str) -> bool:
    lowered = token.lower().replace(" ", "")
    return (
        "mg/m" in lowered
        or "ppm" in lowered
        or "ppb" in lowered
        or "f/ml" in lowered
    )


def _is_unit_exponent_token(token: str) -> bool:
    text = str(token or "").strip()
    if text == "³":
        return True
    return bool(_UNIT_EXPONENT_RE.match(text))


def _is_unit_qualifier_token(token: str) -> bool:
    return bool(re.fullmatch(r"\([^)]+\)", str(token or "").strip()))


def _looks_like_latex_or_number(token: str) -> bool:
    text = str(token or "").strip()
    if not text:
        return False
    if _is_unit_exponent_token(text):
        return False
    if re.search(r"\d|[۰-۹٠-٩]", text):
        return True
    return "\\cdot" in text or text.startswith("\\")


def _looks_like_measured_value(text: str) -> bool:
    return bool(
        re.search(r"\d|[۰-۹٠-٩]", text)
        or "\\cdot" in text
        or _is_limit_unit_token(text)
    )


def _pack_source_values(text: str) -> list[str]:
    """
    Group source tokens into values without inventing new text.

    Units such as mg/m^{3} contain a digit but belong to the preceding
    number. After a unit, the next number/latex token starts a new value.
    """

    packed: list[str] = []

    for token in str(text).split():
        if not packed:
            packed.append(token)
            continue

        prev_last = packed[-1].split()[-1]

        if _is_limit_unit_token(token):
            packed[-1] = f"{packed[-1]} {token}"
            continue

        if packed and "mg/m" in packed[-1].lower() and _is_unit_exponent_token(
            token
        ):
            packed[-1] = f"{packed[-1]}{token}"
            continue

        if _is_limit_unit_token(prev_last) and _looks_like_latex_or_number(
            token
        ):
            packed.append(token)
            continue

        if not _looks_like_latex_or_number(token):
            packed[-1] = f"{packed[-1]} {token}"
            continue

        packed.append(token)

    return packed


def _assign_source_values_to_groups(
    text: str,
    count: int,
) -> list[str | None]:
    """
    Map packed source values onto physical CAS groups in visual order.

    Numeric leftovers are aligned to the last groups (leading chemicals
    often already have MW/limits on the previous visual row). A single
    undifferentiated narrative blob is kept with the first group — it
    is not assumed to belong to the last chemical.
    """

    values = _pack_source_values(text)
    if not values or count <= 0:
        return [None for _ in range(count)]

    if len(values) > count:
        return [None for _ in range(count)]

    if len(values) == count:
        return values

    pad = count - len(values)
    if all(_looks_like_measured_value(value) for value in values):
        return [None] * pad + values

    return values + [None] * pad


def _cell_looks_like_limit(cell: Any) -> bool:
    text = str(getattr(cell, "text", "") or "")
    lowered = text.lower()
    return (
        "mg/m" in lowered
        or "ppm" in lowered
        or "ppb" in lowered
        or "\\cdot" in text
        or "stel" in lowered
        or "twa" in lowered
    )


def _da_limit_text_is_corrupted(text: str) -> bool:
    return "\\cdot" in (text or "") or "\\pi" in (text or "")


def _numeric_from_fragments(fragments: list[str]) -> str | None:
    toks = [
        _ascii_digits(token).strip()
        for token in fragments
        if str(token).strip()
    ]
    if not toks:
        return None
    if all(token in {"-", "—", "–"} for token in toks):
        return None
    if len(toks) == 1:
        token = toks[0]
        if token.isdigit():
            return token
        if "/" in token:
            left, right = token.split("/", 1)
            return parse_slash_decimal(left or "0", right)
        return None
    if len(toks) > 2:
        return None
    first, second = toks[0], toks[1]
    if first.startswith("/") and second.isdigit():
        left = first[1:] or "0"
        return parse_slash_decimal(left, second)
    if first.isdigit() and second.startswith("/"):
        return parse_slash_decimal(first, second[1:] or "0")
    return None


def _unit_from_tokens(tokens: list[str]) -> str:
    unit = ""
    exponent = ""
    for token in tokens:
        lowered = token.lower().replace(" ", "")
        if "mg/m" in lowered:
            unit = "mg/m"
        elif lowered in {"ppm", "ppb"} or "f/ml" in lowered:
            return token
        elif _is_unit_exponent_token(token):
            exponent = "3" if token.strip() == "³" else token.strip()
        elif unit and _is_unit_qualifier_token(token):
            mark = token.strip()
            if exponent.lower().startswith("3") and "(" not in exponent:
                exponent = f"{exponent}{mark}"
            elif not exponent:
                exponent = f"3{mark}"
    if not unit:
        return ""
    if exponent:
        suffix = exponent[1:] if exponent.lower().startswith("3") else exponent
        return f"mg/m3{suffix}"
    return "mg/m"


def reconstruct_limit_expression_from_pdf_words(
    words: list[tuple[float, float, float, float, str]],
) -> str | None:
    """
    Rebuild one STEL/TWA expression from PDF words in one x-cluster.

    Evidence only: does not map LaTeX artifacts to guessed digits.
    """

    if not words:
        return None

    ordered = sorted(words, key=lambda item: (item[0], item[1]))
    tokens = [_normalize_word(item[4]) for item in ordered]
    tokens = [token for token in tokens if token]
    if not tokens:
        return None

    numeric_tokens: list[str] = []
    unit_tokens: list[str] = []
    for token in tokens:
        if (
            _is_limit_unit_token(token)
            or _is_unit_qualifier_token(token)
            or (
                _is_unit_exponent_token(token)
                and any("mg/m" in item.lower() for item in unit_tokens)
            )
        ):
            unit_tokens.append(token)
        else:
            numeric_tokens.append(token)

    number = _numeric_from_fragments(numeric_tokens)
    unit = _unit_from_tokens(unit_tokens)
    if not number:
        return None
    if unit:
        return f"{number} {unit}"
    return number


def _cluster_has_complete_limit(
    cluster: list[tuple[float, float, float, float, str]],
) -> bool:
    tokens = [word[4] for word in cluster]
    has_unit = any(
        _is_limit_unit_token(token) or _is_unit_exponent_token(token)
        for token in tokens
    )
    has_number = _numeric_from_fragments(
        [
            token
            for token in tokens
            if not (
                _is_limit_unit_token(token)
                or _is_unit_exponent_token(token)
                or _is_unit_qualifier_token(token)
            )
        ]
    )
    return bool(has_unit and has_number)


def _cluster_words_by_x(
    words: list[tuple[float, float, float, float, str]],
) -> list[list[tuple[float, float, float, float, str]]]:
    if not words:
        return []
    ordered = sorted(words, key=lambda item: item[0])
    clusters: list[list[tuple[float, float, float, float, str]]] = [
        [ordered[0]]
    ]
    for word in ordered[1:]:
        prev = clusters[-1][-1]
        gap = word[0] - prev[2]
        token = word[4]
        starts_new_number = (
            token.isdigit()
            and not _is_unit_exponent_token(token)
            and not token.startswith("/")
        )
        if gap >= _X_CLUSTER_GAP or (
            gap > 12
            and starts_new_number
            and _cluster_has_complete_limit(clusters[-1])
        ):
            clusters.append([word])
        else:
            clusters[-1].append(word)
    return clusters


def _group_y_bands(
    groups: list[list[str]],
    cas_y: dict[str, float],
    fallback: float = 16.0,
) -> list[tuple[float, float] | None]:
    centers: list[float | None] = []
    for group in groups:
        ys = [cas_y[cas] for cas in group if cas in cas_y]
        centers.append(sum(ys) / len(ys) if ys else None)
    bands: list[tuple[float, float] | None] = []
    for index, center in enumerate(centers):
        if center is None:
            bands.append(None)
            continue
        previous = [
            centers[j]
            for j in range(index)
            if centers[j] is not None
        ]
        following = [
            centers[j]
            for j in range(index + 1, len(centers))
            if centers[j] is not None
        ]
        y0 = (center + max(previous)) / 2.0 if previous else center - fallback
        y1 = (center + min(following)) / 2.0 if following else center + fallback
        bands.append((y0, y1))
    return bands


def _pdf_words(page) -> list[tuple[float, float, float, float, str]]:
    result: list[tuple[float, float, float, float, str]] = []
    if page is None:
        return result
    for word in page.get_text("words"):
        if len(word) < 5:
            continue
        x0, y0, x1, y1, text, *_ = word
        token = _normalize_word(text)
        if not token:
            continue
        if CAS_RE.search(token):
            continue
        result.append(
            (float(x0), float(y0), float(x1), float(y1), token)
        )
    return result


def _split_limit_cell_from_pdf(
    page,
    groups: list[list[str]],
    cas_y: dict[str, float],
    *,
    limit_slot: int,
    limit_slot_count: int,
) -> list[str | None] | None:
    """
    Primary STEL/TWA source: PDF words in each CAS Y-band, clustered by X.

    limit_slot 0 is the left (STEL/C) cluster, 1 is the right (TWA) cluster.
    """

    if page is None or not groups or not cas_y:
        return None
    if limit_slot < 0:
        return None

    bands = _group_y_bands(groups, cas_y)
    page_words = _pdf_words(page)
    if not page_words:
        return None

    values: list[str | None] = []
    found_any = False
    for band in bands:
        if band is None:
            values.append(None)
            continue
        y0, y1 = band
        in_band = []
        for word in page_words:
            word_y = (word[1] + word[3]) / 2.0
            if word_y < y0 or word_y > y1:
                continue
            token = word[4]
            if (
                _looks_like_latex_or_number(token)
                or _is_limit_unit_token(token)
                or _is_unit_exponent_token(token)
                or _is_unit_qualifier_token(token)
                or token.startswith("/")
            ):
                in_band.append(word)
        clusters = _cluster_words_by_x(in_band)
        cluster: list[tuple[float, float, float, float, str]] | None = None
        if not clusters:
            values.append(None)
            continue
        if limit_slot_count <= 1:
            cluster = clusters[0] if len(clusters) == 1 else None
            if cluster is None and 0 <= limit_slot < len(clusters):
                cluster = clusters[limit_slot]
        elif limit_slot < len(clusters):
            cluster = clusters[limit_slot]
        reconstructed = (
            reconstruct_limit_expression_from_pdf_words(cluster)
            if cluster
            else None
        )
        if reconstructed:
            found_any = True
        values.append(reconstructed)

    return values if found_any else None


def _split_nonchemical_cell_by_geometry(
    cell: Any,
    groups: list[list[str]],
    cas_y: dict[str, float],
    *,
    page=None,
    y_threshold: float = 8.0,
    limit_slot: int | None = None,
    limit_slot_count: int = 0,
) -> list[str | None]:
    """
    Determine which physical row owns a non-chemical cell.

    For STEL/C and TWA, PDF words are the primary numeric source when
    a page object is available. Document AI text is fallback context
    only when it is not LaTeX-corrupted.
    """

    count = len(groups)

    if count == 0:
        return []

    bbox = _cell_bbox(
        cell
    )

    original_text = getattr(
        cell,
        "text",
        None,
    )

    original_text = (
        str(original_text)
        if original_text is not None
        else ""
    )

    prefer_pdf = (
        page is not None
        and limit_slot is not None
        and _cell_looks_like_limit(cell)
        and (
            count > 1
            or _da_limit_text_is_corrupted(original_text)
        )
    )

    if prefer_pdf:
        pdf_values = _split_limit_cell_from_pdf(
            page,
            groups,
            cas_y,
            limit_slot=limit_slot,
            limit_slot_count=limit_slot_count,
        )
        if pdf_values is not None:
            if len(pdf_values) < count:
                pdf_values = pdf_values + [None] * (
                    count - len(pdf_values)
                )
            return pdf_values[:count]
        if _da_limit_text_is_corrupted(original_text):
            return [None for _ in range(count)]

    if (
        page is not None
        and _bbox_usable_for_word_split(bbox)
    ):

        x0, y0, x1, y1 = bbox

        grouped_words = (
            _group_words_by_physical_y(
                page,
                x0=x0,
                x1=x1,
                y0=y0,
                y1=y1,
                groups=groups,
                cas_y=cas_y,
                y_threshold=y_threshold,
            )
        )

        texts = [
            _words_to_text(
                grouped_words[index]
            )
            for index in range(count)
        ]

        non_empty = [
            index
            for index, text in enumerate(
                texts
            )
            if text
        ]

        if non_empty:
            packed = _pack_source_values(original_text)
            if packed and len(packed) != count:
                return _assign_source_values_to_groups(
                    original_text,
                    count,
                )
            if packed and len(non_empty) != len(packed):
                return _assign_source_values_to_groups(
                    original_text,
                    count,
                )
            return [
                texts[index]
                if index in non_empty
                else None
                for index in range(count)
            ]

    if original_text.strip() and count > 1:
        return _assign_source_values_to_groups(
            original_text,
            count,
        )

    # ------------------------------------------------------------------
    # No reliable geometry.
    #
    # Do NOT copy the value to all rows.
    # ------------------------------------------------------------------

    if count == 1:
        return [
            original_text
        ]

    return [
        None
        for _ in range(count)
    ]


# ============================================================================
# GENERIC CELL CLONE
# ============================================================================

def _clone_cell(
    cell: Any,
    *,
    row_index: int,
    text: str | None = None,
):
    """
    Clone an extracted cell while preserving evidence/provenance.
    """

    new_cell = copy.deepcopy(
        cell
    )

    new_cell.row = row_index

    if text is not None:
        new_cell.text = text
        # The original cell's normalized_value is the pre-split mega-cell
        # string. Leaving it unchanged copies every sibling CAS / limit
        # onto every physical row. The split substring is source text,
        # not a newly inferred value.
        if hasattr(new_cell, "normalized_value"):
            new_cell.normalized_value = text
        if hasattr(new_cell, "normalized_text"):
            new_cell.normalized_text = text

    new_cell.source_reference = {
        **(
            getattr(
                new_cell,
                "source_reference",
                None,
            )
            or {}
        ),
        "multi_cas_visual_row_split": True,
        "split_basis": (
            "pymupdf_cas_y_geometry"
        ),
    }

    return new_cell


def _clear_cell_value(
    cell: Any,
) -> None:
    """
    Clear value/evidence when physical ownership cannot be established.

    This is deliberately conservative: an unknown numeric value is safer
    than a duplicated numeric value.
    """

    if hasattr(
        cell,
        "text",
    ):
        cell.text = None

    if hasattr(
        cell,
        "normalized_value",
    ):
        cell.normalized_value = None

    if hasattr(
        cell,
        "normalized_text",
    ):
        cell.normalized_text = None

    if hasattr(
        cell,
        "bbox",
    ):
        cell.bbox = None

    if hasattr(
        cell,
        "bbox_confidence",
    ):
        cell.bbox_confidence = None

    if hasattr(
        cell,
        "bbox_source",
    ):
        cell.bbox_source = None


# ============================================================================
# SPLIT ONE ROW
# ============================================================================

def _split_row_by_cas_geometry(
    row: list[Any],
    cas_geometry: list[CasGeometry],
    *,
    page=None,
    y_threshold: float = 8.0,
) -> list[list[Any]]:

    row_cas = _extract_row_cas(
        row
    )

    if not row_cas:
        return [row]

    # ------------------------------------------------------------------
    # Identify the physical CAS groups.
    # ------------------------------------------------------------------

    groups = (
        group_cas_by_pdf_row(
            row_cas,
            cas_geometry,
            y_threshold=y_threshold,
        )
        if len(row_cas) > 1
        else [list(row_cas)]
    )

    # ------------------------------------------------------------------
    # Find the cell containing the CAS values (needed both to resolve
    # cas_y against its own bbox and to split its text by CAS group).
    # ------------------------------------------------------------------

    cas_column_index: int | None = None
    best_cas_count = 0

    for index, cell in enumerate(row):

        found = _extract_cas_from_text(
            getattr(cell, "text", "") or ""
        )
        if len(found) > best_cas_count:
            best_cas_count = len(found)
            cas_column_index = index

    if cas_column_index is None:
        return [row]

    chemical_cell = row[
        cas_column_index
    ]

    # ------------------------------------------------------------------
    # Resolve each CAS's physical Y against the ACTUAL source cell's
    # bbox. This is what disambiguates a CAS that occurs more than once
    # elsewhere on the page. Fall back to the page-wide "first
    # occurrence" lookup (_cas_y_map) only when the bbox-aware lookup
    # finds nothing (e.g. the source cell has no bbox at all).
    #
    # Previously _cas_positions_for_cell() was written but never
    # called from here, so this disambiguation never actually happened
    # in production despite the docstring on cas_positions_for_row()
    # saying it should.
    # ------------------------------------------------------------------

    cell_positions = _cas_positions_for_cell(
        chemical_cell,
        row_cas,
        cas_geometry,
        page=page,
        y_tolerance=y_threshold,
    )

    cas_y = _cas_y_map(
        row_cas,
        cas_geometry,
    )
    if cell_positions:
        cas_y.update(dict(cell_positions))

    chemical_text = (
        getattr(
            chemical_cell,
            "text",
            "",
        )
        or ""
    )

    chemical_segments = (
        _split_text_by_cas(
            chemical_text,
            groups,
        )
    )

    limit_indices = [
        index
        for index, cell in enumerate(row)
        if index != cas_column_index and _cell_looks_like_limit(cell)
    ]

    # ------------------------------------------------------------------
    # Build physical rows.
    # ------------------------------------------------------------------

    result: list[
        list[Any]
    ] = []

    for group_index, group in enumerate(
        groups
    ):

        new_row: list[Any] = []

        for column_index, cell in enumerate(
            row
        ):

            # ==========================================================
            # CHEMICAL / CAS COLUMN
            # ==========================================================

            if (
                column_index
                == cas_column_index
            ):

                text = (
                    chemical_segments[
                        group_index
                    ]
                    if group_index
                    < len(
                        chemical_segments
                    )
                    else ""
                )

                new_cell = _clone_cell(
                    cell,
                    row_index=group_index,
                    text=text,
                )

                if not text:
                    _clear_cell_value(
                        new_cell
                    )

                new_cell.source_reference = {
                    **(
                        getattr(
                            new_cell,
                            "source_reference",
                            None,
                        )
                        or {}
                    ),
                    "physical_group_index": (
                        group_index
                    ),
                    "physical_group_cas": (
                        group
                    ),
                    "physical_row_y": (
                        cas_y.get(
                            group[0]
                        )
                    ),
                }

                new_row.append(
                    new_cell
                )

                continue

            # ==========================================================
            # OTHER COLUMNS
            # ==========================================================

            limit_slot = (
                limit_indices.index(column_index)
                if column_index in limit_indices
                else None
            )

            split_values = (
                _split_nonchemical_cell_by_geometry(
                    cell,
                    groups,
                    cas_y,
                    page=page,
                    y_threshold=y_threshold,
                    limit_slot=limit_slot,
                    limit_slot_count=len(limit_indices),
                )
            )

            value = (
                split_values[
                    group_index
                ]
                if group_index
                < len(split_values)
                else None
            )

            new_cell = _clone_cell(
                cell,
                row_index=group_index,
                text=value,
            )

            original_da_text = getattr(cell, "text", None)

            new_cell.source_reference = {
                **(
                    getattr(
                        new_cell,
                        "source_reference",
                        None,
                    )
                    or {}
                ),
                "physical_group_index": (
                    group_index
                ),
                "physical_group_cas": (
                    group
                ),
                "physical_row_y": (
                    cas_y.get(
                        group[0]
                    )
                ),
                "da_original_text": original_da_text,
            }

            if (
                value
                and original_da_text
                and "\\cdot" in str(original_da_text)
                and "\\cdot" not in str(value)
            ):
                new_cell.source_reference["numeric_source"] = (
                    "pymupdf_words"
                )

            if value is None:

                _clear_cell_value(
                    new_cell
                )

                new_cell.source_reference[
                    "physical_value_unresolved"
                ] = True

                new_cell.source_reference[
                    "resolution_reason"
                ] = (
                    "numeric_or_cell_value_"
                    "could_not_be_assigned_"
                    "safely_to_physical_row"
                )

            new_row.append(
                new_cell
            )

        result.append(
            new_row
        )

    return result


# ============================================================================
# TABLE ROW SPLITTER
# ============================================================================

def split_table_rows_by_cas_geometry(
    rows: list[list[Any]],
    cas_geometry: list[CasGeometry],
    y_threshold: float = 8.0,
    *,
    page=None,
) -> tuple[
    list[list[Any]],
    int,
]:
    """
    Split Document AI rows whose CAS values belong to different physical
    PDF rows.

    Returns:

        corrected_rows,
        split_count

    split_count = number of additional physical rows created.

    IMPORTANT:
        Numeric values are never blindly duplicated.

        `page` MUST be forwarded by the caller for non-chemical column
        values to be assigned by real PDF word geometry instead of
        being cleared (see _group_words_by_physical_y and
        _split_nonchemical_cell_by_geometry above). The structural
        resolver in goldset_generator.structural_resolver is
        responsible for passing its already-open PyMuPDF page object
        here as `page=pdf_page`.
    """

    if not rows:
        return [], 0

    corrected_rows: list[
        list[Any]
    ] = []

    split_count = 0

    for row in rows:

        row_cas = _extract_row_cas(
            row
        )

        if not row_cas:
            corrected_rows.append(row)
            continue

        split_rows = (
            _split_row_by_cas_geometry(
                row,
                cas_geometry,
                page=page,
                y_threshold=y_threshold,
            )
        )

        if (
            len(row_cas) > 1
            and split_required(
                row_cas,
                cas_geometry,
                y_threshold=y_threshold,
            )
            and len(split_rows) > 1
        ):
            split_count += len(split_rows) - 1
            corrected_rows.extend(split_rows)
            continue

        corrected_rows.append(
            split_rows[0] if split_rows else row
        )

    # ------------------------------------------------------------------
    # Global row re-indexing.
    #
    # Do NOT modify semantic row_number here. This layer owns the
    # structural row index only — the semantic "ردیف" label lives
    # inside cell text/provenance and is untouched by this loop.
    # ------------------------------------------------------------------

    reindexed_rows: list[
        list[Any]
    ] = []

    for row_index, row in enumerate(
        corrected_rows
    ):

        reindexed_row: list[Any] = []

        for cell in row:

            new_cell = copy.deepcopy(
                cell
            )

            new_cell.row = row_index

            new_cell.source_reference = {
                **(
                    getattr(
                        new_cell,
                        "source_reference",
                        None,
                    )
                    or {}
                ),
            }

            reindexed_row.append(
                new_cell
            )

        reindexed_rows.append(
            reindexed_row
        )

    return (
        reindexed_rows,
        split_count,
    )