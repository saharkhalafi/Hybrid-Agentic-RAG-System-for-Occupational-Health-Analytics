"""Shared google-genai client factory for Vertex AI."""

from __future__ import annotations

from config.settings import Settings, get_settings


def create_genai_client(settings: Settings | None = None):
    """Create a google-genai Client configured for Vertex AI."""
    from google import genai

    cfg = settings or get_settings()
    return genai.Client(
        vertexai=True,
        project=cfg.gcp_project_id,
        location=cfg.gcp_location,
    )
