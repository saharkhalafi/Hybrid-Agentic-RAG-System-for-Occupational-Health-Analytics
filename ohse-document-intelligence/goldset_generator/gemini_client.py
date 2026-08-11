"""Vertex AI Gemini client for semantic annotation only — never for numerical values."""

from __future__ import annotations

import json
import re
from typing import Any

from config.genai_client import create_genai_client
from config.logging import get_logger
from config.settings import get_settings

logger = get_logger(__name__)

NO_NUMBERS_INSTRUCTION = """
CRITICAL RULES:
- You MUST NOT create, infer, or output any numerical values (numbers, concentrations, limits, CAS digits, ppm, mg/m3, etc.).
- For any numeric data, output ONLY a reference: cell_id, table_id, or field name.
- Output valid JSON only, no markdown fences.
- Use Persian for user-facing question text when requested.
"""


class GeminiClient:
    """Thin wrapper around google-genai (Vertex AI) for semantic tasks."""

    def __init__(self) -> None:
        self.settings = get_settings()
        self.model_name = self.settings.llm_model
        self._client = None

    def _get_client(self):
        if self._client is not None:
            return self._client
        try:
            self._client = create_genai_client(self.settings)
            return self._client
        except Exception as exc:
            logger.warning("genai_client_init_failed", error=str(exc))
            return None

    def available(self) -> bool:
        return self._get_client() is not None

    def generate_ocr_repair_json(self, prompt: str, *, temperature: float = 0.0) -> dict[str, Any] | None:
        """Structured OCR word repair — NOT general text generation."""
        client = self._get_client()
        if client is None:
            return None

        from google.genai import types

        ocr_rules = (
            "Return JSON only. Repair broken Persian OCR words ONLY. "
            "Do NOT rewrite sentences. Do NOT change numbers, units, CAS, or English tokens."
        )
        try:
            response = client.models.generate_content(
                model=self.model_name,
                contents=f"{ocr_rules}\n\n{prompt}",
                config=types.GenerateContentConfig(
                    temperature=temperature,
                    response_mime_type="application/json",
                ),
            )
            text = (response.text or "").strip()
            text = re.sub(r"^```json\s*", "", text)
            text = re.sub(r"\s*```$", "", text)
            payload = json.loads(text)
            return payload if isinstance(payload, dict) else None
        except Exception as exc:
            logger.warning("gemini_ocr_repair_failed", error=str(exc))
            return None

    def generate_json(self, prompt: str, *, temperature: float = 0.1) -> dict[str, Any] | list[Any] | None:
        client = self._get_client()
        if client is None:
            return None

        from google.genai import types

        full_prompt = f"{NO_NUMBERS_INSTRUCTION}\n\n{prompt}"
        try:
            response = client.models.generate_content(
                model=self.model_name,
                contents=full_prompt,
                config=types.GenerateContentConfig(
                    temperature=temperature,
                    response_mime_type="application/json",
                ),
            )
            text = (response.text or "").strip()
            text = re.sub(r"^```json\s*", "", text)
            text = re.sub(r"\s*```$", "", text)
            return json.loads(text)
        except Exception as exc:
            logger.warning("gemini_generate_failed", error=str(exc))
            return None
