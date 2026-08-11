"""Application configuration via environment variables."""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, PostgresDsn, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = PROJECT_ROOT.parent / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(ENV_FILE) if ENV_FILE.exists() else None,
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Application
    environment: Literal["development", "staging", "production"] = "development"
    log_level: str = "INFO"
    processing_version: str = "0.1.0"

    # Google Cloud
    gcp_project_id: str = Field(..., description="GCP project ID")
    gcp_location: str = "us"
    document_ai_processor_id: str = Field(..., description="Document AI processor ID")
    google_application_credentials: Path | None = None

    # GCS
    gcs_bucket_name: str
    gcs_raw_folder: str = "raw"
    gcs_processed_folder: str = "processed"

    # PostgreSQL
    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_db: str = "ohse_intelligence"
    postgres_user: str = "postgres"
    postgres_password: str = "postgres"
    database_url: PostgresDsn | None = None
    postgres_pool_size: int = 30
    postgres_max_overflow: int = 50
    postgres_pool_timeout: int = 30
    postgres_pool_recycle: int = 1800

    # Cloud Run deployment guidance (Phase E)
    cloud_run_concurrency: int = 80
    cloud_run_max_instances: int = 4

    # Vector
    enable_pgvector: bool = True
    vector_dimension: int = Field(
        ...,
        description="Embedding dimension — must match the configured embedding model",
    )

    # LLM / Embeddings
    google_api_key: str | None = None
    llm_model: str = "gemini-2.5-pro"
    embedding_model: str = "gemini-embedding-001"

    # Document processing
    ocr_language_codes: str = "fa,en"
    ocr_dpi: int = 300
    chunk_size: int = 800
    chunk_overlap: int = 150
    default_page_limit: int = 10
    bbox_confidence_threshold: float = 0.85

    # Goldset generation
    goldset_pipeline_version: str = "1.0.0"
    gold_dir: Path = PROJECT_ROOT / "gold"
    data_intermediate_dir: Path = PROJECT_ROOT / "data" / "intermediate"

    # Human-in-the-loop review
    review_claim_lease_minutes: int = 30
    pdf_source_path: Path | None = None

    # Production security (Phase D)
    api_auth_enabled: bool = False
    api_keys: str = ""  # comma-separated "key_id:sha256_hash" pairs
    rate_limit_rps: float = 10.0
    rate_limit_burst: int = 20

    # Session & cache TTL (Phase D)
    session_ttl_seconds: float = 3600.0
    session_max_turns: int = 50
    embedding_cache_ttl_seconds: float = 7200.0
    embedding_cache_max_size: int = 2048
    query_response_cache_ttl_seconds: float = 300.0
    query_response_cache_max_size: int = 512

    # Domain gate (Phase D)
    domain_gate_enabled: bool = True

    # Request / external service timeouts (seconds)
    query_timeout_seconds: float = 30.0
    embedding_timeout_seconds: float = 15.0

    # Paths
    data_raw_dir: Path = PROJECT_ROOT / "data" / "raw"
    data_processed_dir: Path = PROJECT_ROOT / "data" / "processed"

    @field_validator("ocr_language_codes", mode="before")
    @classmethod
    def parse_language_codes(cls, value: str | list[str]) -> str:
        if isinstance(value, list):
            return ",".join(value)
        return value

    @property
    def language_codes(self) -> list[str]:
        return [code.strip() for code in self.ocr_language_codes.split(",") if code.strip()]

    @property
    def sqlalchemy_database_url(self) -> str:
        if self.database_url:
            return str(self.database_url)
        return (
            f"postgresql+psycopg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def api_key_hashes(self) -> dict[str, str]:
        """Parse api_keys setting into {key_id: sha256_hash}."""
        out: dict[str, str] = {}
        if not self.api_keys:
            return out
        for pair in self.api_keys.split(","):
            pair = pair.strip()
            if ":" in pair:
                kid, h = pair.split(":", 1)
                out[kid.strip()] = h.strip()
        return out

    @property
    def document_ai_processor_name(self) -> str:
        return (
            f"projects/{self.gcp_project_id}/locations/{self.gcp_location}"
            f"/processors/{self.document_ai_processor_id}"
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
