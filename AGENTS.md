# HSE6 AI Agent — Agent Instructions

## 1. NON-NEGOTIABLE AGENT RULES

### Scope
- Work only on the task explicitly requested by the user.
- Do not scan the entire repository unless explicitly requested.
- Inspect the smallest possible set of relevant files.
- Do not investigate unrelated modules.

### Changes
- Make the smallest change that solves the demonstrated problem.
- Do not refactor unrelated code.
- Do not rename unrelated functions/classes.
- Do not perform formatting-only changes.
- Do not change architecture unless explicitly requested.
- Do not modify database schema or migrations unless explicitly requested.
- Do not modify Gold artifacts unless explicitly requested.
- Do not modify semantic/RAG/retrieval code when working on extraction/table mapping.

### Testing
- Run the smallest relevant test first.
- Do not run the full test suite unless explicitly requested.
- Do not regenerate large artifacts just to make a test pass.
- A passing test alone is not proof of correctness when an independent source/reference is available.

### Numeric Integrity — CRITICAL
Numerical values must NEVER be invented, inferred, guessed, or silently repaired.

This includes:
- OEL values
- STEL/C
- TWA
- CAS numbers
- measurements
- formulas
- units
- limits

If the source value cannot be reliably determined:
STOP and report the ambiguity.

Never modify a value simply to make a test pass.

### Gold
Gold artifacts are validation references.
- Do not modify Gold to make tests pass.
- Do not regenerate Gold unless explicitly requested.
- If pipeline output conflicts with Gold, investigate the pipeline first.
- If Gold appears inconsistent with the source document, report the discrepancy.

## Document AI — STRICT PROHIBITION

Document AI must NEVER be called by the agent unless the user explicitly and directly authorizes a new Document AI call.

The repository already contains previously generated Document AI/intermediate artifacts under the `data/` directory.

When table structure, geometry, extraction, or layout information is needed:

1. First look for and reuse the existing artifacts under `data/`.
2. Treat these cached/intermediate artifacts as the available upstream extraction data.
3. Do NOT call the Document AI API to regenerate them.
4. Do NOT create new Document AI processors or requests.
5. Do NOT modify Document AI authentication, GCP configuration, processor configuration, or client code.
6. Do NOT regenerate existing intermediate artifacts through Document AI.

If the required information cannot be found in the existing `data/` artifacts:

STOP.

Report:
- what information is missing
- which artifact(s) were checked
- what additional artifact is required

Do NOT call Document AI as a fallback.

Only proceed with a new Document AI call if the user explicitly authorizes it.

### Context / Token Efficiency
- Read only the files required for the current task.
- Prefer targeted searches over reading entire files.
- Do not print large JSON objects.
- Do not print entire logs.
- Do not repeatedly inspect the same file.
- Summarize command output instead of dumping it.
- Do not rerun commands when the relevant result is already known.

### Before Editing
Before modifying code, identify:
1. Root cause
2. Files to change
3. Functions/classes to change
4. Why each change is necessary
5. Targeted test that will validate the change

If the requested change cannot be safely isolated:
STOP and explain what additional information is required.


## ۱. نمای کلی (دو لایه)

HSE6 AI Agent/ (repo root)Runtime entryStructured truth (PostgreSQL)Semantic layer (pgvector).env — secrets & DB/GCP config.gitignoreimg/ — screenshotsohse-document-intelligence/ — main platformapi/main.py → FastAPIagents/orchestrator/pipeline.pyagents/structured/store.pydatabase/models.pyretrieval/pipeline.pypersistence/semantic\_store.py

---

## ۲. درخت پوشه‌ها (سطح بالا)

HSE6 AI Agent/

├── .env                          ← تنظیمات محیط (Postgres 5434, GCP, …)

├── .gitignore

├── img/

└── ohse-document-intelligence/   ← بدنهٔ اصلی سیستم

    ├── api/                      ← FastAPI (HTTP)

    ├── agents/                   ← orchestrator + agents + routing

    ├── retrieval/                ← vector + lexical + rerank pipeline

    ├── persistence/              ← write/read DB, Phase 7, embeddings

    ├── database/                 ← SQLAlchemy models + Alembic

    ├── ingestion/                ← PDF load, page triage

    ├── document\_ai/              ← GCP Document AI + geometry

    ├── extraction/               ← tables, formulas

    ├── goldset\_generator/        ← Gold corpus builders

    ├── pipeline\_contracts/       ← table/OEL contracts & validation

    ├── knowledge/                ← classification, calculations

    ├── validation/               ← confidence scoring

    ├── validation\_engine/        ← rule engine

    ├── review/                   ← HITL review + ui/

    ├── security/                 ← domain gate, auth

    ├── observability/            ← metrics, no\_data\_events

    ├── cache/                    ← embedding/query cache

    ├── config/                   ← settings, logging

    ├── intent/                   ← intent taxonomy

    ├── schema\_registry/          ← JSON schemas (OEL v1, …)

    ├── gold/                     ← production gold artifacts (jsonl)

    ├── data/                     ← eval sets, benchmark results

    ├── docs/                     ← architecture, reports, ADRs

    ├── scripts/                  ← CLI: ingest, Phase7, benchmarks

    ├── tests/                    ← pytest suite

    ├── docker-compose.yml

    ├── alembic.ini

    ├── requirements.txt

    └── README.md

---

## ۳. `agents/` — مغز Q&A

agents/

├── orchestrator/

│   ├── pipeline.py      ← QueryOrchestrator (مسیر اصلی هر query)

│   └── trace.py         ← trace\_id, slots, no\_data\_reason

├── routing/

│   ├── query\_understanding.py

│   ├── normalizer.py

│   ├── chemical\_resolver.py   ← entity resolution (CAS/name)

│   ├── classifier.py        ← intent

│   ├── router.py            ← agent selection

│   └── slots.py

├── structured/          ← OEL عددی (PostgreSQL-authoritative)

│   ├── store.py         ← canonical-only lookup (P0)

│   ├── agent.py

│   ├── authority.py     ← wrong-chemical invariant (P1)

│   ├── integrity.py     ← duplicate limit-type check

│   └── no\_data\_reason.py

├── semantic/            ← narrative retrieval

│   └── agent.py         ← default: VECTOR\_METADATA

├── formula/

│   └── agent.py

├── hybrid/

│   └── agent.py

├── guardrails/

│   └── gate.py

├── session/

│   ├── store.py         ← session isolation

│   └── context.py

├── synthesis/

│   └── answer.py        ← پاسخ نهایی فارسی

└── evaluation/

    ├── p5\_benchmark.py

    ├── phase\_c3\_eval.py

    └── harness.py

---

## ۴. `api/` — لایه HTTP

api/

├── main.py              ← FastAPI app, /health, /metrics

├── routers/

│   ├── query\_router.py  ← POST /query

│   ├── query.py         ← execute\_query + timeout

│   └── review\.py        ← HITL /review/\*

├── middleware/

│   └── security.py      ← rate limit, API key

├── schemas/

│   └── response.py

└── exception\_handlers.py

**اجرا:**

cd ohse-document-intelligence

uvicorn api.main\:app --app-dir . --port 8001

---

## ۵. `retrieval/` + `persistence/` — semantic

retrieval/

├── pipeline.py          ← ProductionRetrievalPipeline (VECTOR\_METADATA default)

├── scope\_audit.py       ← filter parity audit tool

├── lexical\_index.py     ← BM25 Persian

├── reranker.py

├── embeddings.py

├── semantic\_baseline\_eval.py

└── eval\_\*.py            ← eval builders/metrics

persistence/

├── semantic\_store.py    ← embed + pgvector search

├── phase7\_chunk\_rebuild.py

├── phase7\_embed\_preflight.py

├── domain\_reconciliation.py

├── evidence\_pipeline.py

└── canonical\_chunk\_store.py

---

## ۶. Pipeline داده (PDF → پاسخ)

structuredsemanticOHE6.pdfingestion/document\_ai/extraction/goldset\_generator/ + gold/persistence/PostgreSQL + pgvectorUser queryapi/orchestrator/routing/structured/semantic/ → retrieval/synthesis/answerAnswer + citations

---

## ۷. `database/` — لایه‌های داده

database/

├── models.py            ← ChemicalRegistry, OELChemicalLimit, DocumentChunk, …

├── session.py

└── migrations/versions/

    ├── 003\_evidence\_pipeline.py

    ├── 004\_hitl\_review\_system.py

    ├── 005\_phase\_b\_knowledge.py

    └── 006\_no\_data\_events.py

| **لایه DBجداول کلیدینقش** |                                            |                   |
| ------------------------- | ------------------------------------------ | ----------------- |
| Evidence                  | `documents`, `document_pages`              | audit trail       |
| Universal                 | `extracted_tables`, `table_cells`          | raw extraction    |
| Domain                    | `chemical_registry`, `oel_chemical_limits` | OEL authoritative |
| Semantic                  | `document_chunks` + embedding              | RAG               |
| HITL                      | `review_queue`, `no_data_events`           | human feedback    |

---

## ۸. `scripts/` و `tests/` (عملیاتی)

scripts/

├── process\_document.py           ← ingest PDF

├── run\_phase7\_chunk\_rebuild.py   ← embed corpus

├── run\_p5\_benchmark.py           ← correctness gate

├── validate\_local\_api.py         ← تست localhost

└── diagnose\_hybrid\_rerank.py

tests/

├── test\_structured\_authority.py  ← 44-case correctness matrix

├── test\_query\_correctness.py

├── test\_entity\_session\_isolation.py

├── test\_hybrid\_rerank\_page\_scope.py

└── test\_semantic\_retrieval.py

---

## ۹. `docs/` — مستندات مهم

docs/

├── architecture.md

├── hitl.md

└── reports/

    ├── CORRECTNESS\_CLOSURE\_REPORT.md   ← F0 closure

    ├── F0\_FAILURE\_TRIAGE.md

    └── phase\_\* / p5\_results …

---

## ۱۰. جریان یک query واقعی

POST /query  {"query": "TWA benzene"}

    │

    ▼

api/routers/query.py

    │

    ▼

QueryOrchestrator.handle()

    ├─ security/domain\_gate

    ├─ routing/query\_understanding → chemical\_resolver → classifier

    ├─ structured/agent → store.lookup (canonical\_evidence\_v1 only)

    ├─ guardrails/gate

    └─ synthesis/answer

    │

    ▼

QueryResponse (answer, intent, citations, trace)

\# OHSE Document Intelligence Platform

Enterprise-grade hybrid intelligence platform for processing complex Persian/English occupational health exposure documents.

\## Architecture Principle

Numerical data, limit values, measurements, formulas, and compliance decisions \*\*must never be hallucinated\*\*.

The system separates:

1\. \*\*Structured truth layer\*\* — PostgreSQL tables for OEL limits, formulas, measurements, calculations

2\. \*\*Semantic knowledge layer\*\* — pgvector chunks for narrative text, regulations, explanations

\`\`\`

PDF → Page Triage → Document AI → Universal Extraction → Evidence DB

                                      ↓

                         Domain Classification → Domain Tables

                                      ↓

                         Semantic Chunking → Vector DB

                                      ↓

                         Agent Orchestrator (future)

\`\`\`

See [ADR-001]\(docs/ADR-001-cloud-data-residency.md) for cloud data residency decisions.

\## Tech Stack

\- Python 3.12+, FastAPI, SQLAlchemy 2.x, Alembic, Pydantic Settings

\- PostgreSQL 16 + pgvector

\- Google Cloud: Document AI, GCS, Vertex AI, Gemini

\- LangGraph (planned for agent orchestration)

\## Quick Start

\### 1. Environment

Copy environment config to the project root (parent directory):

\`\`\`powershell

copy ohse-document-intelligence\\.env.example ..\\.env

*# Edit ..\\.env with your GCP project, processor ID, and credentials*

\`\`\`

\### 2. Start PostgreSQL

\`\`\`powershell

cd ohse-document-intelligence

docker compose up -d

\`\`\`

PostgreSQL runs on port \*\*5434\*\* (avoids conflict with local PostgreSQL on 5432/5433).

\### 3. Install dependencies

\`\`\`powershell

pip install -r requirements.txt

\`\`\`

\### 4. Run migrations

\`\`\`powershell

alembic upgrade head

\`\`\`

\### 5. Authenticate with GCP

\`\`\`powershell

gcloud auth application-default login

gcloud config set project ohs-document-rag

\`\`\`

\### 6. Process a document (first 10 pages)

\`\`\`powershell

python scripts/process\_document.py --file "../OHE6.pdf" --pages 10

\`\`\`

Triage-only mode (no Document AI call):

\`\`\`powershell

python scripts/process\_document.py --file "../OHE6.pdf" --pages 10 --skip-document-ai

\`\`\`

\### 7. Start API

\`\`\`powershell

uvicorn api.main\:app --reload --app-dir .

\`\`\`

\## Database Layers

\| Layer | Tables | Purpose |

\|-------|--------|---------|

\| Evidence | \`documents\`, \`document\_pages\` | Immutable raw extraction |

\| Universal | \`extracted\_tables\`, \`table\_cells\`, \`formulas\` | All tables before domain mapping |

\| Domain | \`chemical\_registry\`, \`oel\_chemical\_limits\`, \`vibration\_limits\`, \`noise\_limits\`, \`biological\_exposure\_limits\` | Typed OHSE knowledge |

\| Semantic | \`document\_chunks\` | Section-aware vector chunks |

\| Quality | \`review\_queue\`, \`extraction\_validation\_reports\` | Human review gate |

\## Development Order

1\. Infrastructure ✅

2\. Database ✅

3\. Document AI ingestion ✅

4\. Universal extraction ✅

5\. Evidence storage ✅

6\. Validation / review queue ✅

7\. Domain normalization (partial)

8\. Semantic chunking (partial)

9\. Retrieval layer (stub)

10\. Agents (later)

\## Tests

\`\`\`powershell

pytest tests/ -q

\`\`\`

\## Security

\- Never commit \`.env\`, \`service-account.json\`, or any \`\*.json\` credentials

\- Low-confidence extractions route to \`review\_queue\` — they do not silently enter domain tables

\- SQL analytics uses parameterized query templates only (no free-form LLM SQL)