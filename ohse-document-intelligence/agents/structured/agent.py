"""Structured Agent — authoritative PostgreSQL OEL lookups."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agents.structured.authority import StructuredAuthorityError, validate_authoritative_oel_row
from agents.structured.no_data_reason import NoDataReason
from agents.structured.store import PostgresStructuredStore


@dataclass
class StructuredAgentResult:
    success: bool
    data: dict[str, Any] = field(default_factory=dict)
    citations: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
    no_data_reason: str | None = None
    latency_ms: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "data": self.data,
            "citations": self.citations,
            "error": self.error,
            "no_data_reason": self.no_data_reason,
            "latency_ms": self.latency_ms,
        }


class StructuredAgent:
    def __init__(self, store: PostgresStructuredStore) -> None:
        self.store = store

    def execute(self, intent: str, slots: dict[str, Any]) -> StructuredAgentResult:
        import time

        t0 = time.perf_counter()
        try:
            if intent == "STRUCTURED.CHEMICAL.BY_NAME":
                return self._chemical_lookup(slots, t0)
            if intent == "STRUCTURED.CHEMICAL.MOLECULAR_WEIGHT":
                return self._mw_lookup(slots, t0)
            if intent == "STRUCTURED.OEL.PROVENANCE":
                return self._provenance(slots, t0)
            if intent in {
                "STRUCTURED.OEL.TWA_LOOKUP",
                "STRUCTURED.OEL.STEL_LOOKUP",
                "STRUCTURED.OEL.CEILING_LOOKUP",
                "STRUCTURED.OEL.BY_CAS",
            }:
                oel_type = slots.get("oel_type") or slots.get("requested_field") or self._oel_from_intent(intent)
                return self._oel_lookup(slots, oel_type, t0)
            if intent == "STRUCTURED.OEL.ALL_LIMITS_LOOKUP":
                return self._all_limits(slots, t0)
            return StructuredAgentResult(success=False, error=f"unsupported_structured_intent:{intent}", latency_ms=_ms(t0))
        except Exception as exc:
            return StructuredAgentResult(success=False, error=str(exc), latency_ms=_ms(t0))

    def _oel_from_intent(self, intent: str) -> str:
        if "STEL" in intent:
            return "STEL"
        if "CEILING" in intent:
            return "CEILING"
        if "MOLECULAR" in intent:
            return "MOLECULAR_WEIGHT"
        return "TWA"

    def _oel_lookup(self, slots: dict[str, Any], oel_type: str, t0: float) -> StructuredAgentResult:
        result = self.store.lookup_oel_field(
            chemical_name=slots.get("chemical_name"),
            cas=slots.get("cas"),
            chemical_id=slots.get("chemical_id"),
            oel_type=oel_type,
        )
        if not result or result.get("value") is None:
            reason = self.store.classify_no_data_reason(
                chemical_name=slots.get("chemical_name"),
                cas=slots.get("cas"),
                chemical_id=slots.get("chemical_id"),
            )
            return StructuredAgentResult(
                success=False,
                error="no_data",
                no_data_reason=reason.value,
                latency_ms=_ms(t0),
            )
        try:
            validate_authoritative_oel_row(
                result,
                expected_chemical_id=slots.get("chemical_id"),
                expected_cas=slots.get("cas"),
            )
        except StructuredAuthorityError as exc:
            return StructuredAgentResult(success=False, error=f"authority_violation:{exc}", latency_ms=_ms(t0))
        citation = {
            "source_type": "structured",
            "table": "oel_chemical_limits",
            "source_row_key": result.get("source_row_key"),
            "page_number": result.get("page_number"),
            "cell_id": result.get("cell_id"),
            "authority": "postgresql",
        }
        return StructuredAgentResult(
            success=True,
            data=result,
            citations=[citation],
            latency_ms=_ms(t0),
        )

    def _all_limits(self, slots: dict[str, Any], t0: float) -> StructuredAgentResult:
        rows = []
        if slots.get("cas"):
            rows = self.store.get_oel_by_cas(slots["cas"])
        elif slots.get("chemical_id"):
            rows = self.store.get_oel_by_chemical_id(slots["chemical_id"])
        elif slots.get("chemical_name"):
            rows = self.store.get_oel_by_chemical(slots["chemical_name"])
        if not rows:
            reason = self.store.classify_no_data_reason(
                chemical_name=slots.get("chemical_name"),
                cas=slots.get("cas"),
                chemical_id=slots.get("chemical_id"),
            )
            return StructuredAgentResult(
                success=False,
                error="no_data",
                no_data_reason=reason.value,
                latency_ms=_ms(t0),
            )
        row = rows[0]
        try:
            validate_authoritative_oel_row(
                row,
                expected_chemical_id=slots.get("chemical_id"),
                expected_cas=slots.get("cas"),
            )
        except StructuredAuthorityError as exc:
            return StructuredAgentResult(success=False, error=f"authority_violation:{exc}", latency_ms=_ms(t0))
        data = {
            "chemical_name": row.get("english_name"),
            "cas": row.get("cas"),
            "twa": row.get("twa"),
            "stel": row.get("stel"),
            "ceiling": row.get("ceiling"),
            "unit": row.get("unit"),
            "source_row_key": row.get("source_row_key"),
            "page_number": row.get("page_number"),
        }
        citation = {
            "source_type": "structured",
            "table": "oel_chemical_limits",
            "source_row_key": row.get("source_row_key"),
            "page_number": row.get("page_number"),
            "authority": "postgresql",
        }
        return StructuredAgentResult(success=True, data=data, citations=[citation], latency_ms=_ms(t0))

    def _chemical_lookup(self, slots: dict[str, Any], t0: float) -> StructuredAgentResult:
        name = slots.get("chemical_name") or slots.get("cas")
        chem = self.store.get_chemical_by_alias(str(name)) if name else None
        if not chem:
            return StructuredAgentResult(success=False, error="no_data", latency_ms=_ms(t0))
        return StructuredAgentResult(success=True, data=chem, latency_ms=_ms(t0))

    def _mw_lookup(self, slots: dict[str, Any], t0: float) -> StructuredAgentResult:
        mw = self.store.resolve_molecular_weight(
            chemical_name=slots.get("chemical_name"),
            cas=slots.get("cas"),
        )
        if not mw or mw.get("molecular_weight") is None:
            return StructuredAgentResult(success=False, error="no_data", latency_ms=_ms(t0))
        citations: list[dict[str, Any]] = []
        if mw.get("source") == "oel_original_values":
            citations = [{
                "source_type": "structured",
                "table": "oel_chemical_limits",
                "source_row_key": mw.get("source_row_key"),
                "page_number": mw.get("page_number"),
                "authority": "postgresql",
            }]
        return StructuredAgentResult(
            success=True,
            data=mw,
            citations=citations,
            latency_ms=_ms(t0),
        )

    def _provenance(self, slots: dict[str, Any], t0: float) -> StructuredAgentResult:
        res = self._oel_lookup(slots, "TWA", t0)
        if not res.success:
            return res
        data = {
            "source_row_key": res.data.get("source_row_key"),
            "page_number": res.data.get("page_number"),
            "cell_id": res.data.get("cell_id"),
            "cas": res.data.get("cas"),
            "chemical_name": res.data.get("chemical_name"),
        }
        for key in ("symbols", "health_effect"):
            if res.data.get(key) not in (None, ""):
                data[key] = res.data.get(key)
        notation = " ".join(
            str(data[key]) for key in ("symbols", "health_effect") if data.get(key)
        )
        if notation:
            data["original_value"] = notation
        return StructuredAgentResult(
            success=True,
            data=data,
            citations=res.citations,
            latency_ms=_ms(t0),
        )


def _ms(t0: float) -> float:
    import time

    return (time.perf_counter() - t0) * 1000
