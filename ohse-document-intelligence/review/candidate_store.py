"""Versioned extraction candidate storage — never overwrite prior versions."""

from __future__ import annotations

import copy
import json
import re
import uuid
from collections import Counter
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from config.settings import get_settings
from database.models import CandidateStatus, ExtractionCandidate


class CandidateStore:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.settings = get_settings()

    def get_latest(
        self,
        document_id: uuid.UUID,
        candidate_type: str,
        stable_id: str,
    ) -> ExtractionCandidate | None:
        return self.session.scalar(
            select(ExtractionCandidate)
            .where(
                ExtractionCandidate.document_id == document_id,
                ExtractionCandidate.candidate_type == candidate_type,
                ExtractionCandidate.stable_id == stable_id,
            )
            .order_by(ExtractionCandidate.version.desc())
            .limit(1)
        )

    def get_by_id(self, candidate_id: uuid.UUID) -> ExtractionCandidate | None:
        return self.session.get(ExtractionCandidate, candidate_id)

    def create_from_payload(
        self,
        *,
        document_id: uuid.UUID,
        candidate_type: str,
        stable_id: str,
        payload: dict[str, Any],
        page_number: int | None = None,
        storage_path: str | None = None,
        pipeline_version: str | None = None,
    ) -> ExtractionCandidate:
        existing = self.get_latest(document_id, candidate_type, stable_id)
        if existing and existing.status == CandidateStatus.CANDIDATE:
            return existing

        version = 1
        if existing:
            version = existing.version + 1
            existing.status = CandidateStatus.SUPERSEDED
            self.session.flush()

        candidate = ExtractionCandidate(
            document_id=document_id,
            candidate_type=candidate_type,
            stable_id=stable_id,
            version=version,
            parent_version_id=existing.id if existing else None,
            status=CandidateStatus.CANDIDATE,
            payload=payload,
            storage_path=storage_path,
            pipeline_version=pipeline_version or self.settings.goldset_pipeline_version,
            page_number=page_number,
        )
        self.session.add(candidate)
        self.session.flush()
        return candidate

    def create_from_file(
        self,
        *,
        document_id: uuid.UUID,
        candidate_type: str,
        stable_id: str,
        file_path: Path,
        page_number: int | None = None,
    ) -> ExtractionCandidate:
        payload = json.loads(file_path.read_text(encoding="utf-8"))
        return self.create_from_payload(
            document_id=document_id,
            candidate_type=candidate_type,
            stable_id=stable_id,
            payload=payload,
            page_number=page_number or payload.get("page_number"),
            storage_path=str(file_path),
        )

    def create_corrected_version(
        self,
        parent: ExtractionCandidate,
        *,
        corrected_payload: dict[str, Any],
        decision_id: uuid.UUID,
    ) -> ExtractionCandidate:
        parent.status = CandidateStatus.SUPERSEDED
        self.session.flush()

        next_version = parent.version + 1
        candidate = ExtractionCandidate(
            document_id=parent.document_id,
            candidate_type=parent.candidate_type,
            stable_id=parent.stable_id,
            version=next_version,
            parent_version_id=parent.id,
            status=CandidateStatus.CANDIDATE,
            payload=corrected_payload,
            storage_path=parent.storage_path,
            pipeline_version=parent.pipeline_version,
            page_number=parent.page_number,
            created_by_decision_id=decision_id,
        )
        self.session.add(candidate)
        self.session.flush()
        return candidate

    @staticmethod
    def _set_by_path(container: Any, parts: list[str], value: Any) -> None:
        """Set a value at an arbitrary dotted path, creating dicts/lists as needed.
        Numeric path segments index into lists (e.g. "semantics.reference_values.0.reference_value")."""
        cur = container
        for i, part in enumerate(parts):
            is_last = i == len(parts) - 1
            if isinstance(cur, list):
                if not part.isdigit():
                    return
                idx = int(part)
                while len(cur) <= idx:
                    cur.append({})
                if is_last:
                    cur[idx] = value
                    return
                if not isinstance(cur[idx], (dict, list)):
                    cur[idx] = {}
                cur = cur[idx]
            elif isinstance(cur, dict):
                if is_last:
                    cur[part] = value
                    return
                nxt = cur.get(part)
                if not isinstance(nxt, (dict, list)):
                    nxt = [] if parts[i + 1].isdigit() else {}
                    cur[part] = nxt
                cur = cur[part]
            else:
                return

    def apply_corrections_to_payload(
        self,
        payload: dict[str, Any],
        corrections: list[dict[str, Any]],
    ) -> dict[str, Any]:
        updated = copy.deepcopy(payload)
        for corr in corrections:
            field = corr["field_name"]
            corrected = corr.get("corrected_value")
            ctype = corr.get("correction_type", "field_value")

            if ctype == "structure" and field.startswith("header_mapping."):
                key = field.split(".", 1)[1]
                updated.setdefault("header_mapping", {}).pop(key, None)
            elif ctype == "header_mapping" or field.startswith("header_mapping"):
                key = field.split(".", 1)[-1] if "." in field else field.replace("header_mapping.", "")
                mapping = updated.setdefault("header_mapping", {})
                mapping[key] = corrected
            elif field.startswith("rows."):
                parts = field.split(".")
                if len(parts) >= 4:
                    row_idx = int(parts[1])
                    col_name = parts[2]
                    subfield = parts[3]
                    rows = updated.get("rows", [])
                    if row_idx < len(rows) and col_name in rows[row_idx]:
                        if isinstance(rows[row_idx][col_name], dict):
                            rows[row_idx][col_name][subfield] = corrected
            else:
                self._set_by_path(updated, field.split("."), corrected)
        return updated

    @staticmethod
    def normalize_header_field(field_name: str | None) -> str | None:
        """Map reviewer-facing OEL header labels to canonical schema fields."""
        if field_name is None:
            return None
        compact = field_name.strip()
        aliases = {
            "STEL/C": "STEL",
            "STEL": "STEL",
            "TWA": "TWA",
            "C": "ceiling",
            "CEILING": "ceiling",
        }
        return aliases.get(compact.upper(), compact)

    @staticmethod
    def materialize_table_column(
        payload: dict[str, Any],
        *,
        table_id: str,
        source_column: int,
        target_field: str,
        evidence_cells: list[dict[str, Any]],
    ) -> None:
        """Restore a previously overwritten column from immutable evidence.

        A duplicated header mapping (for example columns 2 and 3 both mapped
        to TWA) causes the first column's row values to be overwritten in the
        dict-based candidate. When a reviewer remaps column 2 to STEL, rebuild
        the missing STEL field from the original evidence cells and preserve
        their cell ids, bboxes, and raw text.
        """
        from goldset_generator.table_gold_generator import _parse_exposure_value

        cell_lookup = {
            (int(cell["row"]), int(cell["column"])): cell
            for cell in evidence_cells
            if cell.get("row") is not None and cell.get("column") is not None
        }
        cell_pattern = re.compile(rf"^cell_{re.escape(table_id)}_(\d+)_(\d+)$")

        for candidate_row in payload.get("rows") or []:
            source_rows: list[int] = []
            for field_data in candidate_row.values():
                if not isinstance(field_data, dict):
                    continue
                match = cell_pattern.match(str(field_data.get("cell_id") or ""))
                if match:
                    source_rows.append(int(match.group(1)))
            if not source_rows:
                continue

            source_row = Counter(source_rows).most_common(1)[0][0]
            cell = cell_lookup.get((source_row, source_column))
            if not cell:
                continue

            raw_text = str(cell.get("text") or "")
            value, unit = _parse_exposure_value(raw_text)
            status = "extracted" if value else ("absent" if not raw_text.strip() else "extraction_uncertain")
            source_reference = copy.deepcopy(cell.get("source_reference") or {})
            source_reference["column_name"] = target_field
            candidate_row[target_field] = {
                "value": value,
                "unit": unit,
                "cell_id": cell.get("cell_id"),
                "bbox": copy.deepcopy(cell.get("bbox")),
                "original_value": raw_text,
                "normalized_value": cell.get("normalized_value"),
                "value_status": status,
                "source_reference": source_reference,
            }

    def mark_accepted(self, candidate: ExtractionCandidate) -> None:
        candidate.status = CandidateStatus.ACCEPTED

    def mark_quarantined(self, candidate: ExtractionCandidate) -> None:
        candidate.status = CandidateStatus.QUARANTINED

    def next_run_number(self, candidate_id: uuid.UUID) -> int:
        from database.models import ValidationRun

        current = self.session.scalar(
            select(func.max(ValidationRun.run_number)).where(ValidationRun.candidate_id == candidate_id)
        )
        return (current or 0) + 1
