"""PostgreSQL structured knowledge store."""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from agents.routing.chemical_registry_cache import get_accepted_chemicals
from agents.structured.no_data_reason import NoDataReason
from database.models import ChemicalRegistry, OELChemicalLimit
from goldset_generator.oel_row_parser import parse_molecular_weight
from persistence.chemical_identity import (
    is_generic_identity,
    iter_alias_labels,
    names_match_exact,
    normalize_cas,
)
from pipeline_contracts.numeric_integrity import try_normalize

CANONICAL_GOLD_ARTIFACT_PATH = "canonical_evidence_v1"
ACCEPTED_VALIDATION_STATUS = "accepted"
_GENERIC_ENGLISH_NAME_TOKENS = frozenset({
    "acid",
    "oxide",
    "chloride",
    "alcohol",
    "ether",
    "ester",
    "amine",
    "anhydride",
    "hydrate",
    "solution",
    "dust",
})
_CANONICAL_TEXT_FIELDS = ("symbols", "health_effect")


def english_name_tokens(name: str) -> frozenset[str]:
    cleaned = re.sub(r"[^\w\s\-]", " ", (name or "").strip().lower())
    return frozenset(t for t in cleaned.split() if t)


def token_sets_match(query_name: str, registry_name: str) -> bool:
    query_tokens = english_name_tokens(query_name)
    registry_tokens = english_name_tokens(registry_name)
    if len(query_tokens) < 2 or not registry_tokens:
        return False
    if query_tokens <= _GENERIC_ENGLISH_NAME_TOKENS:
        return False
    return query_tokens == registry_tokens


def canonical_row_text_field(*sources: Any, key: str) -> str | None:
    """Return a stored canonical-row text field; never invent a value."""
    for src in sources:
        if not isinstance(src, dict):
            continue
        raw = src.get(key)
        if raw is None or raw == "":
            continue
        if isinstance(raw, dict):
            raw = raw.get("value")
            if raw is None or raw == "":
                continue
        text = str(raw).strip()
        if text:
            return text
    return None


class PostgresStructuredStore:
    """Authoritative structured lookups — numbers from PostgreSQL only."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def get_chemical_by_alias(self, name: str) -> dict[str, Any] | None:
        name = (name or "").strip()
        if not name or is_generic_identity(name):
            return None
        cas = normalize_cas(name)
        if cas:
            chem = self.session.scalar(select(ChemicalRegistry).where(ChemicalRegistry.cas == cas))
            if chem:
                return self._chem_dict(chem)
        stmt = select(ChemicalRegistry).where(
            or_(
                ChemicalRegistry.english_name.ilike(name),
                ChemicalRegistry.persian_name.ilike(name),
                ChemicalRegistry.cas == name,
            )
        )
        chem = self.session.scalar(stmt)
        if chem:
            return self._chem_dict(chem)
        alias_match = self._match_registry_alias(name)
        if alias_match:
            return self._chem_dict(alias_match)
        token_match = self._match_english_token_set(name)
        if token_match:
            return self._chem_dict(token_match)
        # partial match — only when unique
        matches = list(
            self.session.scalars(
                select(ChemicalRegistry).where(
                    ChemicalRegistry.english_name.ilike(f"%{name}%")
                ).limit(3)
            ).all()
        )
        if len(matches) == 1 and not is_generic_identity(name):
            return self._chem_dict(matches[0])
        return None

    def _match_registry_alias(self, name: str) -> ChemicalRegistry | None:
        if is_generic_identity(name):
            return None
        matches = [
            chem for chem in get_accepted_chemicals(self.session)
            if any(names_match_exact(label, name) for _lang, label in iter_alias_labels(chem))
        ]
        if len(matches) == 1:
            return matches[0]
        return None

    def _match_english_token_set(self, name: str) -> ChemicalRegistry | None:
        query_tokens = english_name_tokens(name)
        if len(query_tokens) < 2 or query_tokens <= _GENERIC_ENGLISH_NAME_TOKENS:
            return None
        distinctive = [
            tok for tok in query_tokens
            if tok not in _GENERIC_ENGLISH_NAME_TOKENS and len(tok) > 2
        ]
        if not distinctive:
            return None
        candidates = list(
            self.session.scalars(
                select(ChemicalRegistry).where(
                    or_(*[ChemicalRegistry.english_name.ilike(f"%{tok}%") for tok in distinctive])
                )
            ).all()
        )
        matches = [
            chem for chem in candidates
            if token_sets_match(name, chem.english_name or "")
        ]
        if len(matches) == 1:
            return matches[0]
        return None

    def get_oel_by_chemical_id(self, chemical_id: str) -> list[dict[str, Any]]:
        chem = self.session.get(ChemicalRegistry, chemical_id)
        if not chem:
            return []
        return self._limits_for_chemical(chem)

    def get_oel_by_cas(self, cas: str) -> list[dict[str, Any]]:
        normalized = normalize_cas(cas) or (cas or "").strip()
        chem = self.session.scalar(select(ChemicalRegistry).where(ChemicalRegistry.cas == normalized))
        if not chem:
            return []
        return self._limits_for_chemical(chem)

    def get_oel_by_chemical(self, name: str, *, oel_type: str | None = None) -> list[dict[str, Any]]:
        chem = self.get_chemical_by_alias(name)
        if not chem:
            return []
        chem_obj = self.session.get(ChemicalRegistry, chem["id"])
        if not chem_obj:
            return []
        return self._limits_for_chemical(chem_obj)

    def _limits_for_chemical(self, chem: ChemicalRegistry) -> list[dict[str, Any]]:
        limits = self.session.scalars(
            select(OELChemicalLimit).where(
                OELChemicalLimit.chemical_id == chem.id,
                OELChemicalLimit.validation_status == ACCEPTED_VALIDATION_STATUS,
                OELChemicalLimit.gold_artifact_path == CANONICAL_GOLD_ARTIFACT_PATH,
            ).order_by(OELChemicalLimit.source_row_key.asc().nulls_last())
        ).all()
        if not limits:
            return []

        results = []
        for lim in limits:
            prov = lim.source_cell_provenance or {}
            row = {
                "id": str(lim.id),
                "chemical_id": str(chem.id),
                "cas": chem.cas,
                "english_name": lim.english_name or chem.english_name,
                "persian_name": lim.persian_name or chem.persian_name,
                "twa": lim.twa,
                "stel": lim.stel,
                "ceiling": lim.ceiling,
                "unit": lim.unit,
                "source_row_key": lim.source_row_key,
                "page_number": lim.page_number,
                "gold_artifact_path": lim.gold_artifact_path,
                "provenance": prov,
                "original_values": lim.original_values,
                "accepted_values": lim.accepted_values,
                "validation_status": ACCEPTED_VALIDATION_STATUS,
                "molecular_weight": chem.molecular_weight,
            }
            for key in _CANONICAL_TEXT_FIELDS:
                text = canonical_row_text_field(
                    lim.accepted_values, lim.original_values, lim.knowledge_metadata, key=key
                )
                if text is not None:
                    row[key] = text
            results.append(row)
        return results

    @staticmethod
    def _chem_dict(chem: ChemicalRegistry) -> dict[str, Any]:
        return {
            "id": str(chem.id),
            "cas": chem.cas,
            "english_name": chem.english_name,
            "persian_name": chem.persian_name,
            "chemical_name": chem.english_name or chem.persian_name,
            "molecular_weight": chem.molecular_weight,
            "aliases": chem.aliases,
        }

    def resolve_molecular_weight(
        self,
        *,
        chemical_name: str | None = None,
        cas: str | None = None,
    ) -> dict[str, Any] | None:
        """Return authoritative MW — prefer OEL source cell when registry value is OCR-corrupted."""
        chem_row = None
        if cas:
            chem_row = self.session.scalar(
                select(ChemicalRegistry).where(ChemicalRegistry.cas == (normalize_cas(cas) or cas))
            )
        elif chemical_name:
            chem_data = self.get_chemical_by_alias(chemical_name)
            if chem_data:
                chem_row = self.session.get(ChemicalRegistry, chem_data["id"])
        if not chem_row:
            return None

        limits = self.session.scalars(
            select(OELChemicalLimit).where(
                OELChemicalLimit.chemical_id == chem_row.id,
                OELChemicalLimit.validation_status == ACCEPTED_VALIDATION_STATUS,
                OELChemicalLimit.gold_artifact_path == CANONICAL_GOLD_ARTIFACT_PATH,
            )
        ).all()

        parsed_display: str | None = None
        parsed_value: float | str | None = None
        source_page: int | None = None
        source_row_key: str | None = None
        for lim in limits:
            raw = (lim.original_values or {}).get("molecular_weight")
            if not raw:
                continue
            raw_text = str(raw).strip()
            parsed_display = parse_molecular_weight(raw_text)
            if parsed_display:
                try:
                    parsed_value = float(parsed_display)
                except ValueError:
                    parsed_value = parsed_display
                source_page = lim.page_number
                source_row_key = lim.source_row_key
                break
            compact = raw_text.replace(" ", "")
            normalized, _method = try_normalize(compact, field_type="molecular_weight")
            if normalized:
                parsed_display = normalized
                try:
                    parsed_value = float(normalized)
                except ValueError:
                    parsed_value = normalized
                source_page = lim.page_number
                source_row_key = lim.source_row_key
                break

        registry_mw = chem_row.molecular_weight
        if parsed_value is not None:
            return {
                "molecular_weight": parsed_value,
                "molecular_weight_display": parsed_display or str(parsed_value),
                "chemical_name": chem_row.english_name,
                "cas": chem_row.cas,
                "source": "oel_original_values",
                "source_row_key": source_row_key,
                "page_number": source_page,
            }

        if registry_mw is not None:
            return {
                "molecular_weight": registry_mw,
                "molecular_weight_display": str(registry_mw),
                "chemical_name": chem_row.english_name,
                "cas": chem_row.cas,
                "source": "chemical_registry",
            }
        return None

    def classify_no_data_reason(
        self,
        *,
        chemical_name: str | None = None,
        cas: str | None = None,
        chemical_id: str | None = None,
    ) -> NoDataReason:
        chem_row: ChemicalRegistry | None = None
        if chemical_id:
            chem_row = self.session.get(ChemicalRegistry, chemical_id)
        elif cas:
            chem_row = self.session.scalar(
                select(ChemicalRegistry).where(ChemicalRegistry.cas == (normalize_cas(cas) or cas))
            )
        elif chemical_name:
            chem_data = self.get_chemical_by_alias(chemical_name)
            if chem_data:
                chem_row = self.session.get(ChemicalRegistry, chem_data["id"])
        if not chem_row:
            return NoDataReason.UNKNOWN_CHEMICAL

        canonical_count = self.session.scalar(
            select(func.count())
            .select_from(OELChemicalLimit)
            .where(
                OELChemicalLimit.chemical_id == chem_row.id,
                OELChemicalLimit.validation_status == ACCEPTED_VALIDATION_STATUS,
                OELChemicalLimit.gold_artifact_path == CANONICAL_GOLD_ARTIFACT_PATH,
            )
        ) or 0
        if canonical_count == 0:
            legacy_count = self.session.scalar(
                select(func.count())
                .select_from(OELChemicalLimit)
                .where(
                    OELChemicalLimit.chemical_id == chem_row.id,
                    OELChemicalLimit.validation_status == "legacy_reference",
                )
            ) or 0
            if legacy_count > 0:
                return NoDataReason.PENDING_PROMOTION
        return NoDataReason.UNKNOWN_CHEMICAL

    def lookup_oel_field(
        self,
        *,
        chemical_name: str | None = None,
        cas: str | None = None,
        chemical_id: str | None = None,
        oel_type: str = "TWA",
    ) -> dict[str, Any] | None:
        rows: list[dict[str, Any]] = []
        if cas:
            rows = self.get_oel_by_cas(cas)
        elif chemical_id:
            rows = self.get_oel_by_chemical_id(chemical_id)
        elif chemical_name:
            rows = self.get_oel_by_chemical(chemical_name, oel_type=oel_type)
        field = oel_type.upper().replace("-", "_").replace("/", "_")
        if not rows:
            if field in {"MW", "MOLECULAR_WEIGHT"}:
                mw = self.resolve_molecular_weight(chemical_name=chemical_name, cas=cas)
                if not mw:
                    return None
                return {
                    "field": "MOLECULAR_WEIGHT",
                    "value": mw.get("molecular_weight"),
                    "molecular_weight": mw.get("molecular_weight"),
                    "unit": None,
                    "normalized_value": mw.get("molecular_weight"),
                    "source_table": mw.get("source") or "chemical_registry",
                    "source_row_key": mw.get("source_row_key"),
                    "page_number": mw.get("page_number"),
                    "cell_id": None,
                    "bbox": None,
                    "original_value": mw.get("molecular_weight_display"),
                    "cas": mw.get("cas"),
                    "chemical_id": None,
                    "chemical_name": mw.get("chemical_name"),
                    "record_id": None,
                }
            return None
        if field in {"STEL_C", "STELC"}:
            row = next(
                (r for r in rows if r.get("stel") is not None or r.get("ceiling") is not None),
                rows[0],
            )
            key = "stel" if row.get("stel") is not None else "ceiling"
            field = "STEL" if key == "stel" else "CEILING"
        elif field in {"SYMBOLS", "NOTATION"}:
            row = next((r for r in rows if r.get("symbols") not in (None, "")), rows[0])
            key = "symbols"
            field = "SYMBOLS"
        elif field in {"MW", "MOLECULAR_WEIGHT"}:
            mw = self.resolve_molecular_weight(
                chemical_name=chemical_name,
                cas=cas,
            )
            if not mw:
                return None
            return {
                "field": "MOLECULAR_WEIGHT",
                "value": mw.get("molecular_weight"),
                "molecular_weight": mw.get("molecular_weight"),
                "unit": None,
                "normalized_value": mw.get("molecular_weight"),
                "source_table": mw.get("source") or "chemical_registry",
                "source_row_key": mw.get("source_row_key"),
                "page_number": mw.get("page_number"),
                "cell_id": None,
                "bbox": None,
                "original_value": mw.get("molecular_weight_display"),
                "cas": mw.get("cas"),
                "chemical_id": None,
                "chemical_name": mw.get("chemical_name"),
                "record_id": None,
            }
        else:
            field_map = {"TWA": "twa", "STEL": "stel", "CEILING": "ceiling", "C": "ceiling"}
            key = field_map.get(field, "twa")
            row = next((r for r in rows if r.get(key) is not None), rows[0])
        value = row.get(key)
        prov = (row.get("provenance") or {}).get(field) or (row.get("provenance") or {}).get(key)
        cell_id = None
        bbox = None
        original = None
        if isinstance(prov, dict):
            cell_id = prov.get("cell_id")
            bbox = prov.get("bbox")
            original = prov.get("original_value") or prov.get("value")
        return {
            "field": field,
            "value": value,
            "unit": row.get("unit"),
            "normalized_value": value,
            "source_table": "oel_chemical_limits",
            "source_row_key": row.get("source_row_key"),
            "page_number": row.get("page_number"),
            "cell_id": cell_id,
            "bbox": bbox,
            "original_value": original,
            "cas": row.get("cas"),
            "chemical_id": row.get("chemical_id"),
            "chemical_name": row.get("english_name") or row.get("persian_name"),
            "record_id": row.get("id"),
            "gold_artifact_path": row.get("gold_artifact_path"),
            "validation_status": row.get("validation_status"),
            "symbols": row.get("symbols"),
            "health_effect": row.get("health_effect"),
        }
