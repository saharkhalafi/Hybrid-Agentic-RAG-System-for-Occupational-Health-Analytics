"""Canonical chemical identity helpers shared by persistence and lookup."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm.attributes import flag_modified
from sqlalchemy.orm import Session

from database.models import ChemicalRegistry, OELChemicalLimit

CAS_PATTERN = re.compile(r"\d{2,7}\s*-\s*\d{2}\s*-\s*\d")
CANONICAL_GOLD_ARTIFACT_PATH = "canonical_evidence_v1"
ACCEPTED_VALIDATION_STATUS = "accepted"

GENERIC_LATIN_TOKENS = frozenset({
    "acid", "acids", "oxide", "oxides", "chloride", "chlorides",
    "alcohol", "alcohols", "ether", "ethers", "ester", "esters",
    "amine", "amines", "anhydride", "hydrate", "solution", "dust",
    "salt", "salts", "as", "and", "the", "isomers", "compounds",
})

GENERIC_PERSIAN_TOKENS = frozenset({
    "اسید", "اکسید", "آمین", "امین", "الکل", "کلرید", "اتر", "استر",
    "نمک", "دی", "و", "ایزومرهای", "ایزومرها", "دمه", "ترکیبات",
    "همه", "به", "صورت", "نوع",
})


@dataclass(frozen=True)
class ExtractedIdentity:
    cas: str | None = None
    english_name: str | None = None
    persian_name: str | None = None


def normalize_cas(value: str | None) -> str | None:
    if not value:
        return None
    match = CAS_PATTERN.search(str(value))
    if not match:
        return None
    return re.sub(r"\s+", "", match.group(0))


def normalize_name(name: str | None) -> str:
    return re.sub(r"\s+", " ", (name or "").strip().lower())


def is_generic_identity(name: str | None) -> bool:
    tokens = [t for t in re.split(r"[\s\-,/]+", normalize_name(name)) if t]
    if not tokens:
        return True
    if len(tokens) == 1:
        token = tokens[0]
        return token in GENERIC_LATIN_TOKENS or token in GENERIC_PERSIAN_TOKENS
    return all(t in GENERIC_LATIN_TOKENS or t in GENERIC_PERSIAN_TOKENS for t in tokens)


def names_match_exact(left: str | None, right: str | None) -> bool:
    if not left or not right:
        return False
    return normalize_name(left) == normalize_name(right)


def _clean_phrase(text: str | None) -> str | None:
    if not text:
        return None
    cleaned = re.sub(r"\s+", " ", text).strip(" \n\t,;:/()[]")
    return cleaned or None


def extract_identity_names(*sources: Any) -> ExtractedIdentity:
    """Extract CAS / Latin / Persian names from stored cell text. Never invent values."""
    cas: str | None = None
    english: str | None = None
    persian: str | None = None
    for raw in sources:
        if raw is None:
            continue
        if isinstance(raw, dict):
            for key in ("value", "normalized_value", "original_value", "chemical_name", "english_name", "persian_name"):
                extracted = extract_identity_names(raw.get(key))
                cas = cas or extracted.cas
                english = english or extracted.english_name
                persian = persian or extracted.persian_name
            continue
        text = str(raw).strip()
        if not text:
            continue
        cas = cas or normalize_cas(text)
        if not english:
            latin_matches = re.findall(r"[A-Za-z][A-Za-z0-9\-,./\s]{1,}[A-Za-z0-9)]", text)
            latin_matches = [_clean_phrase(m) for m in latin_matches if _clean_phrase(m)]
            latin_matches = [m for m in latin_matches if not is_generic_identity(m) and normalize_cas(m) is None]
            if latin_matches:
                english = max(latin_matches, key=len)
            elif re.fullmatch(r"[A-Za-z][A-Za-z0-9\-,./\s]{1,}", text) and not is_generic_identity(text):
                english = _clean_phrase(text)
        if not persian:
            fa_matches = re.findall(r"[\u0600-\u06FF][\u0600-\u06FF\s()\-،0-9]{1,}", text)
            fa_matches = [_clean_phrase(m) for m in fa_matches if _clean_phrase(m)]
            fa_matches = [m for m in fa_matches if not is_generic_identity(m)]
            if fa_matches:
                persian = max(fa_matches, key=len)
    return ExtractedIdentity(cas=cas, english_name=english, persian_name=persian)


def iter_alias_labels(chem: ChemicalRegistry) -> list[tuple[str, str]]:
    labels: list[tuple[str, str]] = []
    aliases = chem.aliases or {}
    if isinstance(aliases, dict):
        for lang in ("en", "fa"):
            for name in aliases.get(lang) or []:
                if isinstance(name, str) and name.strip() and not is_generic_identity(name):
                    labels.append((lang, name.strip()))
    synonyms = chem.synonyms or []
    if isinstance(synonyms, list):
        for name in synonyms:
            if isinstance(name, str) and name.strip() and not is_generic_identity(name):
                labels.append(("syn", name.strip()))
    return labels


def _is_prefix_alias(alias: str, canonical: str | None) -> bool:
    if not alias or not canonical:
        return False
    a, c = normalize_name(alias), normalize_name(canonical)
    return a != c and (c.startswith(a + " ") or a in c.split())


def build_identity_aliases(
    *,
    english_name: str | None,
    persian_name: str | None,
    extra: dict[str, list[str]] | None = None,
) -> dict[str, list[str]] | None:
    merged: dict[str, list[str]] = {"en": [], "fa": []}
    if extra:
        for lang, names in extra.items():
            if lang not in merged:
                merged[lang] = []
            for name in names or []:
                if name:
                    merged[lang].append(str(name))
    if english_name:
        merged["en"].append(english_name)
    if persian_name:
        merged["fa"].append(persian_name)

    cleaned: dict[str, list[str]] = {}
    for lang, names in merged.items():
        seen: set[str] = set()
        kept: list[str] = []
        for name in names:
            phrase = _clean_phrase(name)
            if not phrase or is_generic_identity(phrase):
                continue
            if lang == "en" and _is_prefix_alias(phrase, english_name):
                continue
            if lang == "fa" and _is_prefix_alias(phrase, persian_name):
                continue
            key = normalize_name(phrase)
            if key in seen:
                continue
            seen.add(key)
            kept.append(phrase)
        if kept:
            cleaned[lang] = kept
    return cleaned or None


def apply_extracted_identity(chemical: ChemicalRegistry, extracted: ExtractedIdentity) -> bool:
    changed = False
    if extracted.cas and chemical.cas != extracted.cas:
        chemical.cas = extracted.cas
        changed = True
    current_en = chemical.english_name or ""
    if extracted.english_name and (
        not current_en
        or is_generic_identity(current_en)
        or any(ord(ch) >= 0x0600 for ch in current_en)
        or "[" in current_en
        or "\n" in current_en
    ):
        chemical.english_name = extracted.english_name
        changed = True
    current_fa = chemical.persian_name or ""
    if extracted.persian_name and (not current_fa or is_generic_identity(current_fa)):
        chemical.persian_name = extracted.persian_name
        changed = True
    rebuilt = build_identity_aliases(
        english_name=chemical.english_name,
        persian_name=chemical.persian_name,
        extra=chemical.aliases if isinstance(chemical.aliases, dict) else None,
    )
    if rebuilt != (chemical.aliases or None):
        chemical.aliases = rebuilt
        flag_modified(chemical, "aliases")
        changed = True
    return changed


def _limit_has_value(limit: OELChemicalLimit) -> bool:
    return any(getattr(limit, field) is not None for field in ("twa", "stel", "ceiling"))


def repair_chemical_identity_links(session: Session) -> dict[str, int]:
    """Repair registry names/aliases and link accepted OEL rows to canonical identity."""
    stats = {
        "chemicals_scanned": 0,
        "identities_updated": 0,
        "canonical_links_repaired": 0,
    }
    chemicals = list(session.scalars(select(ChemicalRegistry)).all())
    stats["chemicals_scanned"] = len(chemicals)
    for chemical in chemicals:
        sources: list[Any] = [chemical.cas, chemical.english_name, chemical.persian_name]
        limits = list(session.scalars(
            select(OELChemicalLimit).where(OELChemicalLimit.chemical_id == chemical.id)
        ).all())
        for limit in limits:
            sources.extend([limit.english_name, limit.persian_name, limit.original_values, limit.accepted_values])
        extracted = extract_identity_names(*sources)
        if apply_extracted_identity(chemical, extracted):
            stats["identities_updated"] += 1

        canonical = [
            lim for lim in limits
            if lim.validation_status == ACCEPTED_VALIDATION_STATUS
            and lim.gold_artifact_path == CANONICAL_GOLD_ARTIFACT_PATH
            and _limit_has_value(lim)
        ]
        if canonical:
            continue
        accepted = [
            lim for lim in limits
            if lim.validation_status == ACCEPTED_VALIDATION_STATUS and _limit_has_value(lim)
        ]
        if not accepted:
            continue
        keeper = sorted(
            accepted,
            key=lambda lim: (bool(lim.source_row_key), lim.twa is not None, str(lim.id)),
            reverse=True,
        )[0]
        keeper.gold_artifact_path = CANONICAL_GOLD_ARTIFACT_PATH
        stats["canonical_links_repaired"] += 1
    return stats
