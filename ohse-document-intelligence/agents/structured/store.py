"""PostgreSQL structured knowledge store."""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from database.models import ChemicalRegistry, OELChemicalLimit
from goldset_generator.oel_row_parser import parse_molecular_weight
from pipeline_contracts.numeric_integrity import try_normalize


class PostgresStructuredStore:
    """Authoritative structured lookups — numbers from PostgreSQL only."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def get_chemical_by_alias(self, name: str) -> dict[str, Any] | None:
        name = (name or "").strip()
        if not name:
            return None
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
        # partial match — only when unique
        matches = list(
            self.session.scalars(
                select(ChemicalRegistry).where(
                    ChemicalRegistry.english_name.ilike(f"%{name}%")
                ).limit(3)
            ).all()
        )
        if len(matches) == 1:
            return self._chem_dict(matches[0])
        return None

    def get_oel_by_chemical_id(self, chemical_id: str) -> list[dict[str, Any]]:
        chem = self.session.get(ChemicalRegistry, chemical_id)
        if not chem:
            return []
        return self._limits_for_chemical(chem)

    def get_oel_by_cas(self, cas: str) -> list[dict[str, Any]]:
        chem = self.session.scalar(select(ChemicalRegistry).where(ChemicalRegistry.cas == cas))
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
        for status in ("accepted", "legacy_reference"):
            limits = self.session.scalars(
                select(OELChemicalLimit).where(
                    OELChemicalLimit.chemical_id == chem.id,
                    OELChemicalLimit.validation_status == status,
                ).order_by(OELChemicalLimit.source_row_key.asc().nulls_last())
            ).all()
            if not limits:
                continue
            results = []
            for lim in limits:
                prov = lim.source_cell_provenance or {}
                results.append({
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
                    "validation_status": status,
                    "molecular_weight": chem.molecular_weight,
                })
            return results
        return []

    @staticmethod
    def _chem_dict(chem: ChemicalRegistry) -> dict[str, Any]:
        return {
            "id": str(chem.id),
            "cas": chem.cas,
            "english_name": chem.english_name,
            "persian_name": chem.persian_name,
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
            chem_row = self.session.scalar(select(ChemicalRegistry).where(ChemicalRegistry.cas == cas))
        elif chemical_name:
            chem_data = self.get_chemical_by_alias(chemical_name)
            if chem_data:
                chem_row = self.session.get(ChemicalRegistry, chem_data["id"])
        if not chem_row:
            return None

        limits = self.session.scalars(
            select(OELChemicalLimit).where(
                OELChemicalLimit.chemical_id == chem_row.id,
                OELChemicalLimit.validation_status == "accepted",
            )
        ).all()

        parsed_display: str | None = None
        parsed_value: float | str | None = None
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
                break
            compact = raw_text.replace(" ", "")
            normalized, _method = try_normalize(compact, field_type="molecular_weight")
            if normalized:
                parsed_display = normalized
                try:
                    parsed_value = float(normalized)
                except ValueError:
                    parsed_value = normalized
                break

        registry_mw = chem_row.molecular_weight
        if parsed_value is not None:
            return {
                "molecular_weight": parsed_value,
                "molecular_weight_display": parsed_display or str(parsed_value),
                "chemical_name": chem_row.english_name,
                "cas": chem_row.cas,
                "source": "oel_original_values",
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
        if not rows:
            return None
        field = oel_type.upper()
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
            "chemical_name": row.get("english_name") or row.get("persian_name"),
            "record_id": row.get("id"),
        }
