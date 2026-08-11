# Architecture Overview

## Hybrid Intelligence Model

The platform enforces a strict separation between **structured truth** and **semantic knowledge**.

| Layer | Storage | Used For | Hallucination Risk |
|-------|---------|----------|-------------------|
| Evidence | `document_pages`, raw JSON | Audit trail, reprocessing | None — immutable |
| Universal extraction | `extracted_tables`, `table_cells` | All tables before typing | Low — sourced from Document AI |
| Domain | `oel_chemical_limits`, etc. | Compliance queries | None — rule-validated + reviewed |
| Semantic | `document_chunks` + pgvector | Explanations, narrative | Controlled — no numeric decisions |
| Formulas | `formulas` | Calculations | None — deterministic engine |

## Processing Pipeline

```
PDF
 └─ pdf_loader (PyMuPDF)
     └─ page_classifier (triage)
         └─ preprocessing (scanned pages, 300 DPI)
             └─ Document AI (layout + OCR)
                 └─ parser → extracted_tables / table_cells
                     └─ table_classifier (rules first, LLM fallback later)
                         └─ validation + review_queue
                             └─ domain normalization (when confident)
                                 └─ semantic chunking → embeddings
```

## Design Decisions (Pragmatic)

1. **Domain tables are populated only after classification + review** — not directly from Document AI output.
2. **Vision recovery is stubbed** — wired in architecture but implemented in the next phase when recovery gates are needed.
3. **Embeddings are stubbed** — dimension comes from `VECTOR_DIMENSION` in config, not hardcoded.
4. **First milestone processes 10 pages** — configurable via `--pages`.
5. **Query templates for SQL** — not implemented yet; agents come in phase 10.

## Module Map

| Module | Responsibility |
|--------|----------------|
| `ingestion/` | PDF load, page triage, image preprocessing |
| `document_ai/` | GCP Document AI client, parser, processor |
| `extraction/` | Formulas, tables, text (partial) |
| `normalization/` | Persian text, units, chemicals (partial) |
| `knowledge/` | Table classification, ontology (partial) |
| `validation/` | Confidence scoring |
| `review/` | Human review queue |
| `database/` | SQLAlchemy models, Alembic migrations |
| `retrieval/` | Embeddings, vector store (stub) |
| `api/` | FastAPI health endpoint |
