"""Deterministic chemical entity resolution against PostgreSQL chemical_registry."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from agents.routing.chemical_registry_cache import get_accepted_chemicals
from agents.routing.entity_signals import extract_persian_entity_tokens, query_has_entity_attempt, query_has_explicit_entity_signal
from database.models import ChemicalRegistry

# Common leading descriptors in OHE6 English names (reversed in user queries)
DESCRIPTOR_PREFIXES = frozenset({
    "acid", "acids", "chloride", "choloride", "chlorides", "cholorides",
    "alcohol", "alcohols", "oxide", "oxides", "amine", "amines",
    "ether", "ethers", "ester", "esters", "salt", "salts",
})

CAS_PATTERN = re.compile(r"\b(\d{2,7}-\d{2}-\d)\b")
NON_CHEMICAL_TOKENS = frozenset({
    "TWA", "STEL", "CAS", "MW", "BEI", "OEL", "CEILING", "CEILING",
    "PPM", "PPB", "AHV", "A(8)", "VDV", "LAeq",
})

# Persian modifier tokens that must align with the English chemical name.
_PERSIAN_MODIFIERS = {
    "دی": ("dimethyl", "di-"),
    "متیل": ("methyl", "dimethyl"),
    "tri": ("tri",),
    "mono": ("mono",),
    "bis": ("bis",),
}

# Persian chemical roots → English tokens for compounds missing full Persian names in registry.
_PERSIAN_ROOT_HINTS: dict[str, str] = {
    "استامید": "acetamide",
    "استات": "acetate",
    "استونیتریل": "acetonitrile",
    "بنزن": "benzene",
    "تولوئن": "toluene",
    "متان": "methane",
    "متانول": "methanol",
    "فرمالدئید": "formaldehyde",
    "آمونیاک": "ammonia",
    "استون": "acetone",
    "فنول": "phenol",
    "اتانول": "ethanol",
    "استایرن": "styrene",
    "سولفید": "sulfide",
    "سیلیکات": "silicate",
    "آلومینیوم": "aluminosilicate",
    "فیبر": "fibre",
    "فیبرهای": "fibre",
    "آمینو": "amino",
    "بوتانول": "butanol",
}


@dataclass
class ChemicalResolution:
    chemical_id: str | None
    canonical_name: str | None
    cas: str | None
    persian_name: str | None
    confidence: float
    method: str
    ambiguous: bool = False
    candidates: list[dict[str, Any]] | None = None

    @property
    def source(self) -> str | None:
        """Provenance category for traces: explicit_query | cas_query | session_context."""
        if self.method == "inherited":
            return "session_context"
        if self.method == "cas_exact":
            return "cas_query"
        if self.method in {"empty", "no_match", "no_tokens", "below_threshold", "ambiguous"}:
            return None
        if self.canonical_name or self.cas:
            return "explicit_query"
        return None

    def to_slot_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if self.canonical_name:
            out["chemical_name"] = self.canonical_name
        if self.cas:
            out["cas"] = self.cas
        if self.chemical_id:
            out["chemical_id"] = self.chemical_id
        out["resolution_confidence"] = self.confidence
        out["resolution_method"] = self.method
        if self.source:
            out["resolution_source"] = self.source
        if self.ambiguous:
            out["ambiguous_chemical"] = True
        return out


def _normalize_name(name: str) -> str:
    return re.sub(r"\s+", " ", name.strip().lower())


def _tokenize(name: str) -> set[str]:
    return {t for t in re.split(r"[\s\-,]+", _normalize_name(name)) if len(t) > 1}


def _persian_labels(chem: ChemicalRegistry) -> list[str]:
    labels: list[str] = []
    if chem.persian_name:
        labels.append(chem.persian_name.strip())
    aliases = chem.aliases or {}
    if isinstance(aliases, dict):
        for fa in aliases.get("fa") or []:
            if fa and isinstance(fa, str):
                labels.append(fa.strip())
    return [label for label in labels if len(label) >= 2]


def _query_modifier_tokens(q: str) -> set[str]:
    tokens = _tokenize(q)
    return {t for t in tokens if t in _PERSIAN_MODIFIERS or t.lower() in _PERSIAN_MODIFIERS}


def _modifier_consistent(query: str, chem: ChemicalRegistry) -> bool:
    """Reject partial Persian matches when query modifiers imply a different compound."""
    q_mods = _query_modifier_tokens(query)
    if not q_mods:
        return True
    en = _normalize_name(chem.english_name or "")
    if "دی" in q_mods and "متیل" in q_mods:
        return "dimethyl" in en
    for mod in q_mods:
        required = _PERSIAN_MODIFIERS.get(mod) or _PERSIAN_MODIFIERS.get(mod.lower(), ())
        if required and not any(req in en for req in required):
            return False
    return True


def _combined_label_text(chem: ChemicalRegistry) -> str:
    parts = [chem.english_name or "", chem.persian_name or ""]
    aliases = chem.aliases or {}
    if isinstance(aliases, dict):
        for fa in aliases.get("fa") or []:
            if fa:
                parts.append(str(fa))
        for en in aliases.get("en") or []:
            if en:
                parts.append(str(en))
    return _normalize_name(" ".join(parts))


def _persian_phrase_hits(query: str, chem: ChemicalRegistry) -> list[tuple[int, str]]:
    """Longest Persian label substring matches in the query."""
    hits: list[tuple[int, str]] = []
    for label in _persian_labels(chem):
        if len(label) >= 3 and label in query:
            hits.append((len(label), label))
    # Also match multi-token Persian phrases from the query against combined labels.
    tokens = extract_persian_entity_tokens(query)
    if len(tokens) >= 2:
        for width in range(len(tokens), 1, -1):
            for i in range(len(tokens) - width + 1):
                phrase = " ".join(tokens[i : i + width])
                blob = _combined_label_text(chem)
                if phrase in blob or phrase.replace(" ", "") in blob.replace(" ", ""):
                    hits.append((len(phrase), phrase))
    return hits


def _distinctive_hint_score(query: str, chem: ChemicalRegistry) -> float:
    """Score how many distinctive Persian/English tokens in the query match this chemical."""
    fa_tokens = extract_persian_entity_tokens(query)
    if not fa_tokens:
        return 0.0
    en_blob = _combined_label_text(chem)
    matched = 0
    for token in fa_tokens:
        if token in (chem.persian_name or "") or token in en_blob:
            matched += 1
            continue
        hint = _PERSIAN_ROOT_HINTS.get(token)
        if hint and hint in en_blob:
            matched += 1
    return matched / len(fa_tokens)


def _persian_english_hint_tokens(query: str) -> set[str]:
    """Map Persian chemical phrases in the query to English name tokens."""
    q_tokens = _tokenize(query)
    hints: set[str] = set()
    for fa, en in _PERSIAN_ROOT_HINTS.items():
        if fa in q_tokens or fa in query:
            hints.add(en)
    if "دی" in q_tokens and "متیل" in q_tokens:
        hints.add("dimethyl")
    elif "متیل" in q_tokens:
        hints.add("methyl")
    return hints


def _hint_token_score(hints: set[str], chem: ChemicalRegistry) -> float:
    if not hints:
        return 0.0
    en = _normalize_name(chem.english_name or "")
    matched = sum(1 for hint in hints if hint in en)
    if matched == 0:
        return 0.0
    if "dimethyl" in hints and "dimethyl" not in en:
        return 0.0
    # Multi-root Persian compounds (e.g. آمینو + بوتانول) must match every hint in English name.
    if len(hints) >= 2 and matched < len(hints):
        return 0.0
    return matched / len(hints)


def _resolve_compound_root_hints(
    query: str,
    chemicals: list[ChemicalRegistry],
) -> ChemicalResolution | None:
    """Resolve multi-token Persian queries via English root hints before OCR partial labels."""
    fa_tokens = extract_persian_entity_tokens(query)
    if len(fa_tokens) < 2:
        return None
    hints = _persian_english_hint_tokens(query)
    if len(hints) < 2:
        return None
    hint_scored: list[tuple[float, ChemicalRegistry]] = []
    for chem in chemicals:
        if not _modifier_consistent(query, chem):
            continue
        score = _hint_token_score(hints, chem)
        if score >= 0.5:
            hint_scored.append((score, chem))
    if not hint_scored:
        return None
    hint_scored.sort(key=lambda x: x[0], reverse=True)
    top_score, top_chem = hint_scored[0]
    if len(hint_scored) > 1 and hint_scored[1][0] >= top_score - 0.05:
        return None
    return ChemicalResolution(
        str(top_chem.id), top_chem.english_name, top_chem.cas, top_chem.persian_name,
        top_score, "persian_compound_hint",
    )


def _reverse_descriptor_phrase(query: str) -> list[str]:
    """Generate candidate names from 'acid Acetic' -> 'Acetic acid'."""
    candidates: list[str] = []
    tokens = query.strip().split()
    if len(tokens) >= 2:
        first, rest = tokens[0].lower(), " ".join(tokens[1:])
        if first in DESCRIPTOR_PREFIXES or first.endswith("ide"):
            candidates.append(f"{rest} {tokens[0]}")
            if first == "choloride":
                candidates.append(f"{rest} chloride")
            candidates.append(f"{rest} {first}")
    # Multi-word Latin capture
    m = re.search(r"\b((?:acid|choloride|chloride)\s+[A-Z][a-zA-Z]+)\b", query, re.I)
    if m:
        parts = m.group(1).split()
        if len(parts) == 2:
            candidates.append(f"{parts[1]} {parts[0]}")
    m2 = re.search(r"\b([A-Z][a-zA-Z]+)\s+(acid|chloride|choloride|alcohol)\b", query, re.I)
    if m2:
        candidates.append(f"{m2.group(1)} {m2.group(2).replace('choloride', 'chloride')}")
    return candidates


class ChemicalResolver:
    """Resolve chemical mentions deterministically — ambiguous → clarify, never guess."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def _all_chemicals(self) -> list[ChemicalRegistry]:
        return get_accepted_chemicals(self.session)

    def resolve(
        self,
        query: str,
        *,
        inherited: dict[str, Any] | None = None,
        raw_query: str | None = None,
    ) -> ChemicalResolution:
        inherited = inherited or {}
        entity_query = (raw_query or query or "").strip()
        has_explicit_entity = query_has_entity_attempt(entity_query)

        if (
            inherited.get("chemical_name")
            and inherited.get("resolution_confidence", 1.0) >= 0.9
            and not has_explicit_entity
        ):
            return ChemicalResolution(
                chemical_id=inherited.get("chemical_id"),
                canonical_name=inherited["chemical_name"],
                cas=inherited.get("cas"),
                persian_name=None,
                confidence=float(inherited.get("resolution_confidence", 1.0)),
                method="inherited",
            )

        q = (query or "").strip()
        if not q:
            return ChemicalResolution(None, None, None, None, 0.0, "empty")

        # 1. CAS exact
        cas_m = CAS_PATTERN.search(q)
        if cas_m:
            cas = cas_m.group(1)
            chem = self.session.scalar(select(ChemicalRegistry).where(ChemicalRegistry.cas == cas))
            if chem:
                return ChemicalResolution(
                    str(chem.id), chem.english_name, chem.cas, chem.persian_name,
                    1.0, "cas_exact",
                )

        # 2. Explicit candidates from query patterns
        candidates_to_try: list[str] = []
        for variant in _reverse_descriptor_phrase(q):
            candidates_to_try.append(variant)
        # Latin multi-word
        for m in re.finditer(r"\b([A-Z][a-zA-Z0-9\-()]+(?:\s+[a-zA-Z]+)+)\b", q):
            token = m.group(1)
            if token.upper() not in NON_CHEMICAL_TOKENS:
                candidates_to_try.append(token)
        for m in re.finditer(r"\b([A-Z][a-zA-Z0-9\-()]{2,})\b", q):
            token = m.group(1)
            if token.upper() not in NON_CHEMICAL_TOKENS:
                candidates_to_try.append(token)

        for name in candidates_to_try:
            res = self._match_exact(name)
            if res:
                return res

        chemicals = self._all_chemicals()
        compound = _resolve_compound_root_hints(q, chemicals)
        if compound:
            return compound

        # 3. Persian names — longest consistent match wins (avoid "استامید" ⊂ "دی متیل استامید")
        persian_hits: list[tuple[int, ChemicalRegistry, str]] = []
        for chem in chemicals:
            if not _modifier_consistent(q, chem):
                continue
            for length, label in _persian_phrase_hits(q, chem):
                persian_hits.append((length, chem, label))
        if persian_hits:
            persian_hits.sort(key=lambda x: x[0], reverse=True)
            top_len, top_chem, top_label = persian_hits[0]
            competing = [h for h in persian_hits if h[0] == top_len and h[1].id != top_chem.id]
            if competing:
                # Tie-break short partial Persian labels using multi-token hints.
                tied_chems = {top_chem.id: top_chem}
                for _, chem, _ in competing:
                    tied_chems[chem.id] = chem
                hint_ranked = sorted(
                    (( _distinctive_hint_score(q, c), c) for c in tied_chems.values()),
                    key=lambda x: x[0],
                    reverse=True,
                )
                if hint_ranked[0][0] >= 0.5 and (
                    len(hint_ranked) == 1 or hint_ranked[0][0] > hint_ranked[1][0] + 0.15
                ):
                    winner = hint_ranked[0][1]
                    return ChemicalResolution(
                        str(winner.id), winner.english_name, winner.cas, winner.persian_name,
                        min(0.95, hint_ranked[0][0]), "persian_exact_tiebreak",
                    )
                return ChemicalResolution(
                    None, None, None, None, 0.9, "ambiguous",
                    ambiguous=True,
                    candidates=[
                        {"name": c.english_name, "cas": c.cas, "score": ln / max(len(q), 1)}
                        for ln, c, _ in persian_hits[:3]
                    ],
                )
            return ChemicalResolution(
                str(top_chem.id), top_chem.english_name, top_chem.cas, top_label,
                min(0.98, 0.85 + top_len / max(len(q), 1) * 0.1), "persian_exact",
            )

        # 3c. Distinctive multi-token Persian → English mapping (multi-word chemicals)
        fa_tokens = extract_persian_entity_tokens(q)
        if len(fa_tokens) >= 2:
            hint_scored_dist: list[tuple[float, ChemicalRegistry]] = []
            for chem in self._all_chemicals():
                score = _distinctive_hint_score(q, chem)
                if score >= 0.5:
                    hint_scored_dist.append((score, chem))
            if hint_scored_dist:
                hint_scored_dist.sort(key=lambda x: x[0], reverse=True)
                top_score, top_chem = hint_scored_dist[0]
                if len(hint_scored_dist) > 1 and hint_scored_dist[1][0] >= top_score - 0.15:
                    return ChemicalResolution(
                        None, None, None, None, top_score, "ambiguous",
                        ambiguous=True,
                        candidates=[
                            {"name": c.english_name, "cas": c.cas, "score": s}
                            for s, c in hint_scored_dist[:3]
                        ],
                    )
                return ChemicalResolution(
                    str(top_chem.id), top_chem.english_name, top_chem.cas, top_chem.persian_name,
                    top_score, "persian_distinctive",
                )

        # 3b. Persian root → English token hints (e.g. دی متیل استامید → dimethyl acetamide)
        hint_tokens = _persian_english_hint_tokens(q)
        if hint_tokens:
            hint_scored: list[tuple[float, ChemicalRegistry]] = []
            for chem in self._all_chemicals():
                score = _hint_token_score(hint_tokens, chem)
                if score >= 0.5:
                    hint_scored.append((score, chem))
            if hint_scored:
                hint_scored.sort(key=lambda x: x[0], reverse=True)
                top_score, top_chem = hint_scored[0]
                if len(hint_scored) > 1 and hint_scored[1][0] >= top_score - 0.05:
                    return ChemicalResolution(
                        None, None, None, None, top_score, "ambiguous",
                        ambiguous=True,
                        candidates=[
                            {"name": c.english_name, "cas": c.cas, "score": s}
                            for s, c in hint_scored[:3]
                        ],
                    )
                return ChemicalResolution(
                    str(top_chem.id), top_chem.english_name, top_chem.cas, top_chem.persian_name,
                    top_score, "persian_root_hint",
                )

        # 4. Token-aware scoring
        q_tokens = _tokenize(q)
        if not q_tokens:
            return ChemicalResolution(None, None, None, None, 0.0, "no_tokens")

        scored: list[tuple[float, ChemicalRegistry, str]] = []
        for chem in self._all_chemicals():
            if not _modifier_consistent(q, chem):
                continue
            for label, name in (
                ("english", chem.english_name),
                ("persian", chem.persian_name),
            ):
                if not name:
                    continue
                c_tokens = _tokenize(name)
                if not c_tokens:
                    continue
                overlap = len(q_tokens & c_tokens) / max(len(q_tokens), 1)
                if overlap >= 0.5:
                    scored.append((overlap, chem, label))

        if not scored:
            return ChemicalResolution(None, None, None, None, 0.0, "no_match")

        scored.sort(key=lambda x: x[0], reverse=True)
        top_score, top_chem, method = scored[0]

        # Ambiguity: close scores only when both are meaningfully high
        if (
            len(scored) > 1
            and scored[1][0] >= top_score - 0.05
            and top_score >= 0.35
            and scored[1][0] >= 0.35
        ):
            return ChemicalResolution(
                None, None, None, None, top_score, "ambiguous",
                ambiguous=True,
                candidates=[
                    {"name": c.english_name, "cas": c.cas, "score": s}
                    for s, c, _ in scored[:3]
                ],
            )

        if top_score >= 0.5:
            return ChemicalResolution(
                str(top_chem.id), top_chem.english_name, top_chem.cas, top_chem.persian_name,
                top_score, f"token_{method}",
            )

        return ChemicalResolution(None, None, None, None, top_score, "below_threshold")

    def _match_exact(self, name: str) -> ChemicalResolution | None:
        name = name.strip()
        if not name or name.upper() in NON_CHEMICAL_TOKENS:
            return None
        chem = self.session.scalar(
            select(ChemicalRegistry).where(
                or_(
                    ChemicalRegistry.english_name.ilike(name),
                    ChemicalRegistry.persian_name.ilike(name),
                    ChemicalRegistry.cas == name,
                )
            )
        )
        if chem:
            return ChemicalResolution(
                str(chem.id), chem.english_name, chem.cas, chem.persian_name, 1.0, "exact",
            )
        # ilike contains — only if unique
        matches = list(
            self.session.scalars(
                select(ChemicalRegistry).where(ChemicalRegistry.english_name.ilike(f"%{name}%")).limit(3)
            ).all()
        )
        if len(matches) == 1:
            c = matches[0]
            return ChemicalResolution(str(c.id), c.english_name, c.cas, c.persian_name, 0.85, "partial_unique")
        return None
