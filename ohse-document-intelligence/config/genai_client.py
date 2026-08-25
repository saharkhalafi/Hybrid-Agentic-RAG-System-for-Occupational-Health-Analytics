"""Shared Google GenAI client factory."""

from __future__ import annotations

from config.settings import Settings, get_settings


def create_genai_client(settings: Settings | None = None):
    """Create a Google GenAI client using GEMINI_API_KEY."""

    from google import genai

    cfg = settings or get_settings()

    api_key = cfg.gemini_api_key

    if not api_key:
        raise RuntimeError(
            "GEMINI_API_KEY is not configured. "
            "Set GEMINI_API_KEY in the project .env file."
        )

    return genai.Client(
        api_key=api_key,
    )