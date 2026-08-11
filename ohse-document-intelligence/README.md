# OHSE Document Intelligence Platform

Enterprise-grade hybrid intelligence platform for processing complex Persian/English occupational health exposure documents.

## Architecture Principle

Numerical data, limit values, measurements, formulas, and compliance decisions **must never be hallucinated**.

The system separates:

1. **Structured truth layer** — PostgreSQL tables for OEL limits, formulas, measurements, calculations
2. **Semantic knowledge layer** — pgvector chunks for narrative text, regulations, explanations

```
PDF → Page Triage → Document AI → Universal Extraction → Evidence DB
                                      ↓
                         Domain Classification → Domain Tables
                                      ↓
                         Semantic Chunking → Vector DB
                                      ↓
                         Agent Orchestrator (future)
```

See [ADR-001](docs/ADR-001-cloud-data-residency.md) for cloud data residency decisions.

## Tech Stack

- Python 3.12+, FastAPI, SQLAlchemy 2.x, Alembic, Pydantic Settings
- PostgreSQL 16 + pgvector
- Google Cloud: Document AI, GCS, Vertex AI, Gemini
- LangGraph (planned for agent orchestration)

## Quick Start

### 1. Environment

Copy environment config to the project root (parent directory):

```powershell
copy ohse-document-intelligence\.env.example ..\.env
# Edit ..\.env with your GCP project, processor ID, and credentials
```

### 2. Start PostgreSQL

```powershell
cd ohse-document-intelligence
docker compose up -d
```

PostgreSQL runs on port **5434** (avoids conflict with local PostgreSQL on 5432/5433).

### 3. Install dependencies

```powershell
pip install -r requirements.txt
```

### 4. Run migrations

```powershell
alembic upgrade head
```

### 5. Authenticate with GCP

```powershell
gcloud auth application-default login
gcloud config set project ohs-document-rag
```

### 6. Process a document (first 10 pages)

```powershell
python scripts/process_document.py --file "../OHE6.pdf" --pages 10
```

Triage-only mode (no Document AI call):

```powershell
python scripts/process_document.py --file "../OHE6.pdf" --pages 10 --skip-document-ai
```

### 7. Start API

```powershell
uvicorn api.main:app --reload --app-dir .
```

## Database Layers

| Layer | Tables | Purpose |
|-------|--------|---------|
| Evidence | `documents`, `document_pages` | Immutable raw extraction |
| Universal | `extracted_tables`, `table_cells`, `formulas` | All tables before domain mapping |
| Domain | `chemical_registry`, `oel_chemical_limits`, `vibration_limits`, `noise_limits`, `biological_exposure_limits` | Typed OHSE knowledge |
| Semantic | `document_chunks` | Section-aware vector chunks |
| Quality | `review_queue`, `extraction_validation_reports` | Human review gate |

## Development Order

1. Infrastructure ✅
2. Database ✅
3. Document AI ingestion ✅
4. Universal extraction ✅
5. Evidence storage ✅
6. Validation / review queue ✅
7. Domain normalization (partial)
8. Semantic chunking (partial)
9. Retrieval layer (stub)
10. Agents (later)

## Tests

```powershell
pytest tests/ -q
```

## Security

- Never commit `.env`, `service-account.json`, or any `*.json` credentials
- Low-confidence extractions route to `review_queue` — they do not silently enter domain tables
- SQL analytics uses parameterized query templates only (no free-form LLM SQL)
