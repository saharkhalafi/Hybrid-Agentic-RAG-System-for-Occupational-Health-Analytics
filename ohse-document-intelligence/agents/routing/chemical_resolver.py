"""Deterministic chemical entity resolution against PostgreSQL chemical_registry."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from agents.routing.chemical_registry_cache import get_accepted_chemicals
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
    en_tokens = set(en.split())
    overlap = len(hints & en_tokens)
    if overlap == 0:
        return 0.0
    if "dimethyl" in hints and "dimethyl" not in en:
        return 0.0
    return overlap / len(hints)


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

    def resolve(self, query: str, *, inherited: dict[str, Any] | None = None) -> ChemicalResolution:
        inherited = inherited or {}
        if inherited.get("chemical_name") and inherited.get("resolution_confidence", 1.0) >= 0.9:
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

        # 3. Persian names — longest consistent match wins (avoid "استامید" ⊂ "دی متیل استامید")
        persian_hits: list[tuple[int, ChemicalRegistry, str]] = []
        for chem in self._all_chemicals():
            if not _modifier_consistent(q, chem):
                continue
            for label in _persian_labels(chem):
                if len(label) >= 3 and label in q:
                    persian_hits.append((len(label), chem, label))
        if persian_hits:
            persian_hits.sort(key=lambda x: x[0], reverse=True)
            top_len, top_chem, top_label = persian_hits[0]
            # Ambiguity when two labels tie on length
            tied = [h for h in persian_hits if h[0] == top_len]
            if len(tied) > 1:
                return ChemicalResolution(
                    None, None, None, None, 0.9, "ambiguous",
                    ambiguous=True,
                    candidates=[
                        {"name": c.english_name, "cas": c.cas, "score": ln / max(len(q), 1)}
                        for ln, c, _ in tied[:3]
                    ],
                )
            return ChemicalResolution(
                str(top_chem.id), top_chem.english_name, top_chem.cas, top_label,
                min(0.98, 0.85 + top_len / max(len(q), 1) * 0.1), "persian_exact",
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

        # Ambiguity: two close matches
        if len(scored) > 1 and scored[1][0] >= top_score - 0.05:
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
