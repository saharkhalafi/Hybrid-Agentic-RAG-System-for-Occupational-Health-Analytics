"""Deterministic chemical entity resolution against PostgreSQL chemical_registry."""
#E:\cursor projects\HSE6 AI Agent\ohse-document-intelligence\agents\routing\chemical_resolver.py
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from agents.routing.chemical_registry_cache import get_accepted_chemicals
from agents.routing.entity_signals import extract_persian_entity_tokens, query_has_entity_attempt, query_has_explicit_entity_signal
from agents.routing.normalizer import strip_limit_type_prefix
from database.models import ChemicalRegistry
from persistence.chemical_identity import (
    is_generic_identity,
    iter_alias_labels,
    names_match_exact,
    normalize_cas,
    normalize_name,
)

# Common leading descriptors in OHE6 English names (reversed in user queries)
DESCRIPTOR_PREFIXES = frozenset({
    "acid", "acids", "chloride", "choloride", "chlorides", "cholorides",
    "alcohol", "alcohols", "oxide", "oxides", "amine", "amines",
    "ether", "ethers", "ester", "esters", "salt", "salts",
})

# NOTE: tolerates internal spacing around the hyphens (e.g.
# "100 - 42 - 5" or a pasted "[100-42-5]"), consistent with the
# CAS_PATTERN used throughout the rest of the pipeline
# (goldset_generator.structural_resolver, ingestion.merged_row_splitter,
# document_ai.geometry_resolver, knowledge_pipeline.py). A user query
# containing a spaced or bracketed CAS must resolve to the same
# chemical_registry row as the canonical un-spaced form.
CAS_PATTERN = re.compile(r"\d{2,7}\s*-\s*\d{2}\s*-\s*\d")
NON_CHEMICAL_TOKENS = frozenset({
    "TWA", "STEL", "CAS", "MW", "BEI", "OEL", "CEILING", "CEILING",
    "PPM", "PPB", "AHV", "A(8)", "VDV", "LAeq",
})


def _normalize_cas_match(value: str) -> str:
    """
    Collapse whitespace inside a raw CAS_PATTERN match, e.g.
    "100 - 42 - 5" -> "100-42-5". Keeps this module's CAS handling
    consistent with normalize_cas() in
    goldset_generator.structural_resolver and the CAS_PATTERN used in
    knowledge_pipeline.py.
    """

    return re.sub(r"\s+", "", value)


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
    return normalize_name(name)


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
    return [label for label in labels if len(label) >= 2 and not is_generic_identity(label)]


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


def _clean_latin_phrase(name: str) -> str:
    return name.strip().strip("()[]").strip()


def _is_generic_latin_token(name: str) -> bool:
    tokens = _normalize_name(name).split()
    return len(tokens) == 1 and tokens[0] in DESCRIPTOR_PREFIXES


def _collect_latin_candidates(query: str) -> list[str]:
    """Explicit Latin phrases, longer/reversed registry forms before generic tokens."""
    ordered: list[str] = []
    seen: set[str] = set()

    def add(name: str) -> None:
        cleaned = _clean_latin_phrase(name)
        if not cleaned or cleaned.upper() in NON_CHEMICAL_TOKENS:
            return
        key = _normalize_name(cleaned)
        if key in seen:
            return
        seen.add(key)
        ordered.append(cleaned)

    for variant in _reverse_descriptor_phrase(query):
        add(variant)
    for match in re.finditer(r"\b([A-Za-z][a-zA-Z0-9\-()]+(?:\s+[a-zA-Z]+)+)\b", query):
        phrase = match.group(1)
        add(phrase)
        parts = _clean_latin_phrase(phrase).split()
        if len(parts) == 2:
            add(f"{parts[1]} {parts[0]}")
    for match in re.finditer(r"\b([A-Za-z][a-zA-Z0-9\-()]{2,})\b", query):
        add(match.group(1))
    return ordered


def _is_specific_latin_entity(name: str) -> bool:
    """True for a real Latin chemical phrase, not TWA/acid/formula_id tokens."""
    cleaned = _clean_latin_phrase(name)
    if not cleaned or cleaned.upper() in NON_CHEMICAL_TOKENS:
        return False
    if _is_generic_latin_token(cleaned):
        return False
    if any(ch.isdigit() for ch in cleaned):
        return False
    parts = cleaned.split()
    if len(parts) >= 2:
        return True
    return len(cleaned) >= 5


def _primary_specific_latin_phrase(query: str) -> str | None:
    names = [n for n in _collect_latin_candidates(query) if _is_specific_latin_entity(n)]
    if not names:
        return None
    return max(names, key=lambda n: (len(n.split()), len(n)))


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
        authoritative: bool = False,
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

        q = strip_limit_type_prefix((query or "").strip())
        if not q:
            return ChemicalResolution(None, None, None, None, 0.0, "empty")

        token_overlap_min = 0.65 if authoritative else 0.5
        partial_min_len = 5 if authoritative else 3

        # 1. Exact CAS
        cas_m = CAS_PATTERN.search(q)
        if cas_m:
            cas = normalize_cas(cas_m.group(0)) or _normalize_cas_match(cas_m.group(0))
            chem = next(
                (c for c in self._all_chemicals() if normalize_cas(c.cas) == cas or c.cas == cas),
                None,
            )
            if chem:
                return ChemicalResolution(
                    str(chem.id), chem.english_name, chem.cas, chem.persian_name,
                    1.0, "cas_exact",
                )

        # 2. Exact normalized Latin name
        latin_ambiguous: ChemicalResolution | None = None
        for name in _collect_latin_candidates(q):
            if is_generic_identity(name):
                continue
            res = self._match_exact_latin(name)
            if not res:
                continue
            if res.ambiguous:
                if latin_ambiguous is None:
                    latin_ambiguous = res
                continue
            return res
        if latin_ambiguous is not None:
            return latin_ambiguous

        # 3. Exact normalized Persian name (canonical field only)
        persian_exact = self._match_exact_persian(q)
        if persian_exact:
            return persian_exact

        # 4. Existing aliases / synonyms
        alias_exact = self._match_exact_alias(q)
        if alias_exact:
            return alias_exact

        specific_latin = _primary_specific_latin_phrase(q)
        if specific_latin:
            # Explicit Latin/CAS entity is present. Do not drop it for a Persian-name tie.
            return ChemicalResolution(
                None, specific_latin, None, None, 0.55, "latin_explicit",
            )

        if is_generic_identity(q):
            return ChemicalResolution(None, None, None, None, 0.0, "no_match")

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
                if overlap >= token_overlap_min:
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

        if top_score >= token_overlap_min:
            return ChemicalResolution(
                str(top_chem.id), top_chem.english_name, top_chem.cas, top_chem.persian_name,
                top_score, f"token_{method}",
            )

        return ChemicalResolution(None, None, None, None, top_score, "below_threshold")

    def _resolution(self, chem: ChemicalRegistry, method: str, confidence: float = 1.0) -> ChemicalResolution:
        return ChemicalResolution(
            str(chem.id), chem.english_name, chem.cas, chem.persian_name, confidence, method,
        )

    def _ambiguous(self, chemicals: list[ChemicalRegistry], score: float = 0.9) -> ChemicalResolution:
        return ChemicalResolution(
            None, None, None, None, score, "ambiguous",
            ambiguous=True,
            candidates=[{"name": c.english_name, "cas": c.cas, "score": score} for c in chemicals[:3]],
        )

    def _match_exact_latin(self, name: str) -> ChemicalResolution | None:
        name = name.strip()
        if not name or name.upper() in NON_CHEMICAL_TOKENS or is_generic_identity(name):
            return None
        matches = [
            chem for chem in self._all_chemicals()
            if names_match_exact(chem.english_name, name)
        ]
        if len(matches) == 1:
            return self._resolution(matches[0], "exact")
        if len(matches) > 1:
            return self._ambiguous(matches)
        return None

    def _identity_name_candidates(self, query: str) -> list[str]:
        candidates: list[str] = []
        fa_tokens = extract_persian_entity_tokens(query)
        if fa_tokens:
            candidates.append(" ".join(fa_tokens))
            if len(fa_tokens) == 1:
                candidates.append(fa_tokens[0])
        for name in _collect_latin_candidates(query):
            if not is_generic_identity(name):
                candidates.append(name)
        return candidates

    def _match_exact_persian(self, query: str) -> ChemicalResolution | None:
        candidates = self._identity_name_candidates(query)
        if not candidates:
            return None
        matches: list[ChemicalRegistry] = []
        for chem in self._all_chemicals():
            label = (chem.persian_name or "").strip()
            if len(label) < 2 or is_generic_identity(label):
                continue
            if any(names_match_exact(label, cand) for cand in candidates):
                matches.append(chem)
        if not matches:
            return None
        matches.sort(key=lambda c: len(c.persian_name or ""), reverse=True)
        top = matches[0]
        top_len = len(top.persian_name or "")
        competing = [c for c in matches[1:] if len(c.persian_name or "") == top_len]
        if competing:
            return self._ambiguous([top, *competing])
        return self._resolution(top, "persian_exact", 0.98)

    def _match_exact_alias(self, query: str) -> ChemicalResolution | None:
        candidates = self._identity_name_candidates(query)
        if not candidates:
            return None
        hits: list[tuple[int, ChemicalRegistry]] = []
        for chem in self._all_chemicals():
            for _lang, label in iter_alias_labels(chem):
                if is_generic_identity(label):
                    continue
                if any(names_match_exact(label, cand) for cand in candidates):
                    hits.append((len(label), chem))
                    break
        if not hits:
            return None
        hits.sort(key=lambda x: x[0], reverse=True)
        top_len, top_chem = hits[0]
        competing = [chem for length, chem in hits[1:] if length == top_len and chem.id != top_chem.id]
        if competing:
            return self._ambiguous([top_chem, *competing])
        return self._resolution(top_chem, "alias_exact", 0.95)

    def _match_exact(self, name: str, *, authoritative: bool = False) -> ChemicalResolution | None:
        name = name.strip()
        if not name or name.upper() in NON_CHEMICAL_TOKENS or is_generic_identity(name):
            return None
        latin = self._match_exact_latin(name)
        if latin:
            return latin
        persian = self._match_exact_persian(name)
        if persian:
            return persian
        if normalize_cas(name):
            cas = normalize_cas(name)
            chem = next((c for c in self._all_chemicals() if normalize_cas(c.cas) == cas), None)
            if chem:
                return self._resolution(chem, "cas_exact")
        alias = self._match_exact_alias(name)
        if alias:
            return alias
        chemicals = self._all_chemicals()
        normalized_name = _normalize_name(name)
        partial_matches = [
            chem for chem in chemicals
            if normalized_name in _normalize_name(chem.english_name or "")
        ]
        if len(partial_matches) == 1:
            chem = partial_matches[0]
            if authoritative and len(name) < 5:
                return None
            return self._resolution(chem, "partial_unique", 0.85)
        return None
