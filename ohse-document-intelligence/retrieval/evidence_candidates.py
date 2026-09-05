"""Multi-source evidence candidates generated BEFORE rerank.

Sources: structured OEL rows, row_knowledge, semantic_text, formula, lexical.
Numerics are copied from stored rows only — never invented.
"""

from __future__ import annotations

import re
from typing import Any

from retrieval.page_identity import retrieval_page_fields
from retrieval.semantic_retrieval import ROW_KNOWLEDGE_SOURCE_TYPE, SEMANTIC_SOURCE_TYPE

CAS_RE = re.compile(r"\b(\d{2,7}-\d{2}-\d)\b")
_LATIN_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9\-]{2,}")
_IDENTITY_SKIP = frozenset(
    {
        "twa",
        "stel",
        "oel",
        "cas",
        "ppm",
        "mw",
        "the",
        "and",
        "for",
        "of",
        "table",
        "limit",
        "exposure",
    }
)
_PERSIAN_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789")

FIELD_TOKEN_GROUPS: dict[str, tuple[str, ...]] = {
    "twa": ("twa", "میانگین", "وزنی"),
    "stel": ("stel", "کوتاه", "مدت"),
    "stel_c": ("stel/c", "stel-c", "stel_c", "stel", "ceiling"),
    "ceiling": ("ceiling", "سقف"),
    "molecular_weight": ("mw", "molecular", "weight", "وزن", "مولکول", "ملکولی"),
    "symbols": ("symbols", "نماد", "notation", "پوست", "bei"),
}

_FORMULA_ID_RE = re.compile(r"فرمول|formula_", re.IGNORECASE)
_FORMULA_SYMBOL_RE = re.compile(r"\b(ipm|ahv|dae|ahw)\b", re.IGNORECASE)


def normalize_query_text(text: str) -> str:
    return (text or "").lower().translate(_PERSIAN_DIGITS)


def query_name_tokens(query: str, slots: dict[str, Any] | None = None) -> list[str]:
    slots = slots or {}
    found: list[str] = []
    seen: set[str] = set()

    def _add(token: str) -> None:
        tok = token.strip().lower()
        if len(tok) < 3 or tok in _IDENTITY_SKIP or tok in seen:
            return
        seen.add(tok)
        found.append(tok)

    for key in ("chemical_name", "english_name"):
        val = slots.get(key)
        if val:
            _add(str(val))
            for part in _LATIN_TOKEN_RE.findall(str(val)):
                _add(part)
    for part in _LATIN_TOKEN_RE.findall(query or ""):
        _add(part)
    return found


_SAFE_IDENTITY_RE = re.compile(r"^[\w\u0600-\u06FF][\w\u0600-\u06FF\s.\-/]{1,79}$", re.UNICODE)


def identity_lookup_patterns(query: str, slots: dict[str, Any] | None = None) -> list[str]:
    """CAS/name strings grounded in the current query text. Inherited slots alone do not count."""
    slots = slots or {}
    q = query or ""
    from_query = extract_cas_values(q)
    if from_query:
        return from_query
    q_names = query_name_tokens(q, {})
    slot_name = str(slots.get("chemical_name") or slots.get("english_name") or "").strip()
    name_in_query = bool(slot_name) and (
        slot_name.lower() in q.lower() or any(tok in slot_name.lower() for tok in q_names)
    )
    if name_in_query:
        slot_cas = extract_cas_values(slots.get("cas"))
        if slot_cas:
            return slot_cas
        if _SAFE_IDENTITY_RE.match(slot_name) and slot_name.lower() not in _IDENTITY_SKIP:
            return [slot_name]
    return [tok for tok in q_names if len(tok) >= 3][:2]


def identity_matches(query: str, item: dict[str, Any], slots: dict[str, Any] | None = None) -> bool:
    """True when the candidate carries a query-grounded CAS or chemical name."""
    patterns = identity_lookup_patterns(query, slots)
    if not patterns:
        return False
    blob = " ".join(
        str(x)
        for x in (
            item.get("content"),
            item.get("cas"),
            item.get("chemical_name"),
            item.get("english_name"),
            item.get("chunk_id"),
        )
        if x
    ).lower()
    return any(str(pattern).lower() in blob for pattern in patterns)


def extract_cas_values(*texts: Any) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    for text in texts:
        if text is None:
            continue
        for cas in CAS_RE.findall(str(text)):
            if cas not in seen:
                seen.add(cas)
                found.append(cas)
    return found


def requested_fields(query: str, slots: dict[str, Any] | None = None) -> list[str]:
    slots = slots or {}
    out: list[str] = []
    oel = str(slots.get("oel_type") or slots.get("requested_field") or "").strip().lower()
    aliases = {
        "twa": "twa",
        "stel": "stel",
        "stel_c": "stel_c",
        "stel/c": "stel_c",
        "stel-c": "stel_c",
        "ceiling": "ceiling",
        "c": "ceiling",
        "mw": "molecular_weight",
        "molecular_weight": "molecular_weight",
        "symbols": "symbols",
        "notation": "symbols",
    }
    if oel in aliases:
        out.append(aliases[oel])
    q = normalize_query_text(query)
    if re.search(r"stel\s*/\s*c|stel-c|stel_c", q):
        out.append("stel_c")
    elif re.search(r"\bstel\b", q):
        out.append("stel")
    if re.search(r"\btwa\b|میانگین\s*وزنی", q):
        out.append("twa")
    if re.search(r"ceiling|سقف", q):
        out.append("ceiling")
    if re.search(r"\bmw\b|molecular\s*weight|وزن\s*مولکول|وزن\s*ملکول", q):
        out.append("molecular_weight")
    if re.search(r"symbols|نماد|notation", q):
        out.append("symbols")
    deduped: list[str] = []
    for field in out:
        if field not in deduped:
            deduped.append(field)
    return deduped


def is_formula_query(query: str, slots: dict[str, Any] | None = None) -> bool:
    """True only for formula-id, formula-symbol, or explicit formula calculation queries."""
    slots = slots or {}
    if slots.get("formula_id") or slots.get("variables"):
        return True
    q = query or ""
    if _FORMULA_ID_RE.search(q):
        return True
    if _FORMULA_SYMBOL_RE.search(q):
        return True
    if re.search(r"رابطه", q) and re.search(r"فرمول|formula|محاسبه", q, re.IGNORECASE):
        return True
    return False


def evidence_id(item: dict[str, Any]) -> str:
    return str(
        item.get("chunk_id")
        or item.get("source_row_key")
        or item.get("formula_id")
        or item.get("record_id")
        or ""
    )


def annotate_candidate(
    item: dict[str, Any],
    *,
    source: str,
    evidence_type: str | None = None,
) -> dict[str, Any]:
    row = dict(item)
    pages = retrieval_page_fields(
        page_number=row.get("page_number"),
        printed_page_number=row.get("printed_page_number"),
    )
    row.update(pages)
    row["candidate_source"] = source
    inferred = evidence_type or row.get("evidence_type") or row.get("source_type") or source
    row["evidence_type"] = inferred
    if inferred == ROW_KNOWLEDGE_SOURCE_TYPE:
        row["source_type"] = ROW_KNOWLEDGE_SOURCE_TYPE
    elif inferred == "formula":
        row["source_type"] = "formula"
    elif inferred == "structured":
        row["source_type"] = "structured"
    elif not row.get("source_type"):
        row["source_type"] = SEMANTIC_SOURCE_TYPE
    if not row.get("chunk_id"):
        row["chunk_id"] = evidence_id(row)
    return row


def _pool_key(item: dict[str, Any]) -> str:
    et = str(item.get("evidence_type") or item.get("source_type") or "")
    return f"{et}:{evidence_id(item)}"


def merge_evidence_candidates(
    pools: list[list[dict[str, Any]]],
    *,
    limit: int = 40,
) -> list[dict[str, Any]]:
    """Union pools in the given order. Dedup identical evidence_type+id only.

    First pool is the ranking backbone: it is never truncated or reordered by score.
    Later pools are appended. A later source cannot replace an earlier one.
    """
    chosen: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for pool in pools:
        for item in pool:
            cid = evidence_id(item)
            if not cid:
                continue
            key = _pool_key(item)
            prev = chosen.get(key)
            if prev is None:
                chosen[key] = item
                order.append(key)
                continue
            alts = list(prev.get("alt_sources") or [])
            src = item.get("candidate_source")
            if src and src not in alts:
                alts.append(src)
            prev["alt_sources"] = alts
    ordered = [chosen[key] for key in order]
    backbone_n = 0
    if pools:
        backbone_keys = {_pool_key(item) for item in pools[0] if evidence_id(item)}
        backbone_n = sum(1 for key in order if key in backbone_keys)
    keep = max(int(limit), backbone_n)
    return ordered[:keep]


def union_additive_candidates(
    backbone: list[dict[str, Any]],
    extra_pools: list[list[dict[str, Any]]],
    *,
    query: str,
    slots: dict[str, Any] | None = None,
    extra_limit: int = 30,
) -> list[dict[str, Any]]:
    """Keep backbone order; append extra pools without replacing backbone evidence."""
    slots = slots or {}
    extras = merge_evidence_candidates(extra_pools, limit=extra_limit)
    return merge_evidence_candidates(
        [backbone, extras],
        limit=len(backbone) + extra_limit,
    )


def compact_pre_rerank(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for rank, item in enumerate(candidates, start=1):
        rows.append(
            {
                "rank": rank,
                "chunk_id": evidence_id(item),
                "content": item.get("content"),
                "score": item.get("score"),
                "page_number": item.get("page_number"),
                "printed_page_number": item.get("printed_page_number"),
                "document_page": item.get("document_page"),
                "evidence_type": item.get("evidence_type") or item.get("source_type"),
                "candidate_source": item.get("candidate_source"),
                "source_row_key": item.get("source_row_key"),
                "formula_id": item.get("formula_id"),
                "cas": item.get("cas"),
                "field": item.get("field"),
                "chemical_name": item.get("chemical_name"),
                "value": item.get("value"),
                "twa": item.get("twa"),
                "stel": item.get("stel"),
                "ceiling": item.get("ceiling"),
            }
        )
    return rows


def candidate_log_rows(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for rank, item in enumerate(candidates, start=1):
        rows.append(
            {
                "rank": rank,
                "source": item.get("candidate_source"),
                "score": item.get("score"),
                "page": item.get("document_page")
                if item.get("document_page") is not None
                else item.get("page_number"),
                "printed_page_number": item.get("printed_page_number"),
                "id": evidence_id(item),
                "evidence_type": item.get("evidence_type") or item.get("source_type"),
            }
        )
    return rows


def structured_row_to_candidate(row: dict[str, Any], *, field: str | None = None) -> dict[str, Any]:
    """Copy stored structured fields into a retrieval candidate. No invented values."""
    parts: list[str] = []
    cas = row.get("cas")
    name = row.get("english_name") or row.get("chemical_name") or row.get("persian_name")
    if cas:
        parts.append(f"CAS: {cas}")
    if name:
        parts.append(f"chemical_name: {name}")
    for key in ("twa", "stel", "ceiling", "unit", "molecular_weight", "symbols", "health_effect"):
        val = row.get(key)
        if val not in (None, ""):
            parts.append(f"{key}: {val}")
    if field and row.get("value") not in (None, ""):
        parts.append(f"{field}: {row.get('value')}")
    content = "\n".join(parts)
    pages = retrieval_page_fields(
        page_number=row.get("page_number"),
        printed_page_number=row.get("printed_page_number"),
    )
    return annotate_candidate(
        {
            "chunk_id": "structured:" + str(row.get("source_row_key") or row.get("record_id") or row.get("id") or ""),
            "content": content,
            "score": 0.0,
            "source_row_key": row.get("source_row_key"),
            "record_id": row.get("id") or row.get("record_id"),
            "cas": cas,
            "chemical_name": name,
            "field": field or row.get("field"),
            "value": row.get("value"),
            "twa": row.get("twa"),
            "stel": row.get("stel"),
            "ceiling": row.get("ceiling"),
            "molecular_weight": row.get("molecular_weight"),
            "symbols": row.get("symbols"),
            "health_effect": row.get("health_effect"),
            "page_number": pages["page_number"],
            "printed_page_number": pages["printed_page_number"],
            "provenance": row.get("provenance"),
        },
        source="structured",
        evidence_type="structured",
    )


def formula_row_to_candidate(row: dict[str, Any]) -> dict[str, Any]:
    parts = [
        row.get("formula_id"),
        row.get("original_expression") or row.get("normalized_expression"),
        row.get("description"),
        row.get("domain"),
    ]
    content = "\n".join(str(p) for p in parts if p)
    return annotate_candidate(
        {
            "chunk_id": str(row.get("formula_id") or ""),
            "content": content,
            "score": 0.0,
            "formula_id": row.get("formula_id"),
            "expression": row.get("original_expression") or row.get("normalized_expression"),
            "page_number": row.get("page_number"),
        },
        source="formula",
        evidence_type="formula",
    )


def query_identity(query: str, slots: dict[str, Any] | None = None) -> dict[str, Any]:
    slots = slots or {}
    cas_list = extract_cas_values(query, slots.get("cas"))
    names: list[str] = []
    for key in ("chemical_name", "english_name"):
        val = slots.get(key)
        if val:
            names.append(str(val))
    return {
        "cas": cas_list,
        "chemical_names": names,
        "fields": requested_fields(query, slots),
        "formula_query": is_formula_query(query, slots),
    }
