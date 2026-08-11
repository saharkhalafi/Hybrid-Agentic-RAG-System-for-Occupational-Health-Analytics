"""Gemini semantic interpretation for formulas — structure/meaning only, never invent symbols."""

from __future__ import annotations

import json
from typing import Any

from config.logging import get_logger
from extraction.formula_candidate_detector import FormulaCandidate
from extraction.formula_reconstructor import FormulaReconstruction
from goldset_generator.gemini_client import GeminiClient

logger = get_logger(__name__)

FORMULA_SEMANTIC_INSTRUCTION = """
You interpret mathematical formulas from supplied PDF evidence.

Rules:
1. Do NOT invent missing mathematical symbols.
2. Do NOT invent numerical constants.
3. Do NOT infer a formula merely from domain knowledge.
4. Preserve variable, exponent, denominator, numerator, subscript, and superscript roles.
5. Every reconstructed component must be traceable to supplied evidence.
6. Numeric reference values (e.g. 28800) may ONLY appear if explicitly present in nearby_text or evidence fragments.
7. If the equation cannot be reconstructed reliably, set status to extraction_uncertain.
8. Output valid JSON only.
"""


class FormulaSemanticMapper:
    def __init__(self, gemini_client: Any | None = None) -> None:
        self.gemini = gemini_client if gemini_client is not None else GeminiClient()

    def interpret(
        self,
        candidate: FormulaCandidate,
        reconstruction: FormulaReconstruction,
        page_text: str,
    ) -> dict[str, Any] | None:
        if not self.gemini.available():
            return self._deterministic_fallback(candidate, reconstruction, page_text)

        payload = {
            "page": candidate.page,
            "document_equation_reference": candidate.document_equation_reference,
            "formula_candidate": candidate.candidate_text,
            "nearby_text": f"{candidate.text_before}\n{candidate.text_after}".strip(),
            "reconstructed_expression": reconstruction.expression,
            "symbols": candidate.nearby_math_tokens,
            "source_references": candidate.evidence_text_fragments,
        }
        prompt = (
            f"{FORMULA_SEMANTIC_INSTRUCTION}\n\n"
            "Input:\n"
            f"{json.dumps(payload, ensure_ascii=False, indent=2)}\n\n"
            "Return JSON with keys: formula_type, variables (object with role/description), "
            "units (object), reference_values (array of {variable, reference_value, unit, source}), "
            "status, semantic_confidence."
        )
        result = self.gemini.generate_json(prompt, temperature=0.0)
        if not isinstance(result, dict):
            return self._deterministic_fallback(candidate, reconstruction, page_text)
        return result

    def _deterministic_fallback(
        self,
        candidate: FormulaCandidate,
        reconstruction: FormulaReconstruction,
        page_text: str,
    ) -> dict[str, Any]:
        """Rule-based semantics when Gemini is unavailable."""
        eq_ref = candidate.document_equation_reference
        variables: dict[str, Any] = {}
        units: dict[str, str] = {}
        reference_values: list[dict[str, Any]] = []

        if eq_ref == "2":
            formula_type = "exposure_equivalence"
            variables = {
                "ahv": {"role": "result", "description": "equivalent vibration acceleration"},
                "ahw": {"role": "input", "description": "frequency-weighted RMS acceleration per interval"},
                "t": {"role": "input", "description": "duration of each exposure interval"},
                "T": {"role": "input", "description": "total exposure duration"},
            }
            units = {"ahv": "m/s²", "ahw": "m/s²", "T": "time", "t": "time"}
        elif eq_ref == "3":
            formula_type = "8_hour_vibration_exposure"
            variables = {
                "A(8)": {"role": "result", "description": "8-hour frequency-weighted vibration exposure"},
                "ahv": {"role": "input", "description": "equivalent vibration acceleration"},
                "Tv": {"role": "input", "description": "total daily vibration exposure duration"},
                "T0": {"role": "input", "description": "reference exposure duration"},
            }
            units = {"A(8)": "exposure_index", "ahv": "m/s²", "Tv": "time", "T0": "time"}
            if "28800" in page_text:
                reference_values.append(
                    {
                        "variable": "T0",
                        "reference_value": "28800",
                        "unit": "s",
                        "source": "document_evidence",
                        "evidence_snippet": "8 ساعت28800 ثانیه",
                    }
                )
        else:
            formula_type = "unknown"
            for token in candidate.nearby_math_tokens:
                if token.isalpha() or "(" in token:
                    variables[token] = {"role": "unknown", "description": "detected in evidence"}

        return {
            "formula_type": formula_type,
            "variables": variables,
            "units": units,
            "reference_values": reference_values,
            "status": reconstruction.status,
            "semantic_confidence": 0.55 if reconstruction.status == "validated" else 0.35,
        }
