# ADR-001: Cloud Data Residency and GCP Region Selection

## Status
Accepted

## Context
The OHSE Document Intelligence Platform processes occupational health exposure documents containing sensitive workplace measurement data, chemical exposure limits, and regulatory compliance information. The platform uses:

- Google Cloud Storage for raw/processed documents
- Google Document AI for OCR and layout extraction
- Vertex AI for embeddings and future agent orchestration
- Cloud SQL (PostgreSQL + pgvector) for structured and semantic knowledge

Stakeholders require clarity on where document data is stored and processed, particularly for Persian/English regulatory documents that may fall under organizational data governance policies.

## Decision
1. **Primary GCP region:** `us` (Document AI processor location) with project `ohs-document-rag`.
2. **All document artifacts** (raw PDFs, page images, Document AI JSON) are stored in a dedicated GCS bucket within the same GCP project.
3. **PostgreSQL** runs in the same geographic region as production Cloud SQL when deployed (currently local Docker for development).
4. **Immutable evidence layer** — raw Document AI JSON is always persisted to `data/processed/` and `document_pages.ocr_json` before any normalization or domain mapping.
5. **No cross-region inference** — Vertex AI embedding and LLM calls use the configured `GCP_LOCATION` to avoid silent data egress.
6. **Service account keys are disallowed** by org policy; authentication uses Application Default Credentials (ADC) and optional service account impersonation.

## Consequences

### Positive
- Single-project isolation simplifies IAM auditing.
- Immutable raw extraction supports compliance audits and human review.
- ADC avoids long-lived JSON key exposure.

### Negative
- `us` region may add latency for users in Iran/Middle East — acceptable for batch ingestion; API latency to be evaluated in production.
- Org policy blocking SA keys requires developers to use `gcloud auth application-default login`.

## Alternatives Considered
| Option | Rejected Because |
|--------|------------------|
| Multi-region GCS | Adds complexity; no current replication requirement |
| On-prem OCR only | Cannot handle complex table/layout extraction at required quality |
| EU region | Document AI processor already provisioned in `us` |

## Review Date
2026-08-03 — Revisit when production Cloud SQL region is finalized.
