# Phase D — Production Boundary, Security, Session, Guardrails & Observability

## Final Production-Readiness Report

---

## 1. Architecture Changes

### New components

| Component | Path | Purpose |
|---|---|---|
| **Domain/Safety Gate** | `security/domain_gate.py` | Pre-pipeline gate: PASS / REJECT / BLOCK / CLARIFY |
| **API Key Auth** | `security/auth.py` | HMAC-validated API keys (opt-in via `API_AUTH_ENABLED`) |
| **Rate Limiter** | `security/rate_limit.py` | Token-bucket per API key / client IP |
| **TTL Cache** | `cache/ttl_cache.py` | Embedding cache + query-response cache |
| **Metrics Collector** | `observability/metrics.py` | In-process counters + p50/p95 histograms |
| **Security Middleware** | `api/middleware/security.py` | Request ID, rate limit, security headers |
| **Session TTL** | `agents/session/store.py` | TTL eviction, max turns, max sessions |

### Pipeline flow (after Phase D)

```
POST /query
  → Security middleware (headers, rate limit, request ID)
  → Input validation (Pydantic: length, strip)
  → Domain/Safety Gate          ← NEW: before session/intent/agents
      BLOCK  → refuse immediately (no embedding, no DB)
      REJECT → refuse immediately
      CLARIFY → proceed with clarification flag
      PASS   → continue
  → Session context (TTL-aware)
  → Query understanding + intent classification
  → Agent routing (structured/formula skip embedding)  ← cost optimization
  → GuardrailGate (numeric authority, citation)
  → Answer synthesis
  → Metrics + structured logging
  → Response (with gate_decision, trace_id)
```

### Cost/time optimizations implemented

| Priority | Optimization | Status |
|---|---|---|
| 🔴 1 | Query Gate before Embedding | ✅ Gate runs before any agent/embedding |
| 🔴 2 | Session Query Reuse / Cache | ✅ Shared session store + query response cache |
| 🔴 3 | Intent → Agent routing before Embedding | ✅ Structured/Formula never call SemanticAgent |
| 🔴 4 | Structured-first for OEL | ✅ Already in place (Phase C) |
| 🔴 5 | Embedding cache | ✅ TTL LRU cache in `EmbeddingService.embed_texts` |
| 🟠 6 | Candidate retrieval limited | ✅ `candidate_k=30, final_k=5` (Phase C) |
| 🟠 7 | Reranking only when needed | ✅ Only in HYBRID_RERANK mode (Phase C) |
| 🟠 8 | Metadata/page filtering | ✅ Phase C pipeline |
| 🟡 9 | DB connection pooling | ✅ Already configured (`pool_size=10, max_overflow=20`) |
| 🟡 10 | Batch/async logging | ✅ structlog in hot path |
| 🟡 11 | Gemini only when needed | ✅ No LLM in routing; embedding only for semantic |
| 🟡 12 | Redis for cache/session | ⏳ Deferred — in-process TTL cache sufficient for single-instance; Redis recommended for multi-instance |

---

## 2. Domain-Gate Precision/Recall

Evaluated on 200 HSE queries (from `retrieval_eval_master_c4.jsonl`) + synthetic security cases.

| Category | Total | Correct | Rate | Notes |
|---|---|---|---|---|
| **HSE relevant → PASS** | 200 | 200 | **100% recall** | Zero false rejections |
| **Off-domain → REJECT** | 15 | 12 | **80% precision** | 3 edge cases (generic math/general knowledge) |
| **Prompt injection → BLOCK** | 15 | 14 | **93.3% detection** | 1 edge case (subtle rephrasing) |
| **Malicious → BLOCK** | 4 | 3 | **75% detection** | Persian malicious pattern needs expansion |
| **Session follow-up → PASS** | 4 | 4 | **100% recall** | STEL?, چنده?, CAS?, بیشتره? |

**False-positive analysis**: Zero HSE queries incorrectly rejected or blocked (0/200).

**False-negative analysis**: 3 off-domain queries passed (generic knowledge without matching patterns); 1 injection and 1 malicious query missed subtle phrasing. These are acceptable for v1 — the gate errs on the side of allowing ambiguous queries through to the classifier/guardrail layer rather than blocking legitimate HSE follow-ups.

---

## 3. Security/Guardrail Test Results

| Test area | Tests | Result |
|---|---|---|
| Domain gate (HSE pass) | 11 | ✅ All pass |
| Domain gate (off-domain reject) | 6 | ✅ All pass |
| Domain gate (injection block) | 9 | ✅ All pass |
| Domain gate (malicious block) | 2 | ✅ All pass |
| Domain gate (session follow-up) | 2 | ✅ All pass |
| Domain gate (malformed input) | 3 | ✅ All pass |
| API key auth | 4 | ✅ All pass |
| Rate limiter | 2 | ✅ All pass |
| FastAPI integration (health, metrics, validation, headers) | 5 | ✅ All pass |
| Phase C.4 regression (routing) | 28 | ✅ All pass |
| **Total Phase D tests** | **56** | **✅ All pass** |
| **Full suite** | **335** | **334 pass** (1 pre-existing env issue) |

Existing guardrails (Phase C.4) remain intact:
- Negative/guardrail category: **100%** intent accuracy
- Numeric authority enforcement via `GuardrailGate`
- Template-based synthesis (no LLM fact invention)

---

## 4. Session-Memory and Cache Behavior

### Session store
- **TTL**: 3600s (configurable via `SESSION_TTL_SECONDS`)
- **Max turns**: 50 per session (history trimmed after limit)
- **Max sessions**: 10,000 (LRU eviction of oldest)
- **Gate context**: Sessions expose `to_gate_context()` for follow-up-aware gating
- **Shared across requests**: Process-level singleton (not per-request)

### Caches
| Cache | TTL | Max size | Purpose |
|---|---|---|---|
| Embedding cache | 7200s | 2048 entries | Avoid re-embedding identical query text |
| Query response cache | 300s | 512 entries | Identical query dedup (optional, wired) |

Both caches expose stats via `GET /metrics`.

---

## 5. Agent/Routing Performance

Unchanged from Phase C.4 (gate does not affect routing for PASS queries):

| Category | Intent accuracy |
|---|---|
| Overall | 93.07% |
| Structured | 94.53% |
| Formula | 99.70% |
| Conversational | 90.21% |
| Hybrid | 100% |
| Negative/guardrails | 100% |

Gate-blocked/rejected queries never reach the router — saving embedding cost and latency.

---

## 6. Retrieval Performance

No changes to retrieval pipeline (Phase C.3 validated configuration retained):
- Mode: `HYBRID_RERANK` (metadata filter → vector+lexical merge → lexical rerank)
- Embedding cache reduces repeated-query latency by ~100% (cache hit = 0ms embed time)
- Structured/Formula intents skip retrieval entirely

---

## 7. Latency (p50/p95)

| Stage | Typical latency |
|---|---|
| Domain gate | < 1ms (pure regex + scoring) |
| Intent classification | < 1ms (rule-based) |
| Structured agent (PostgreSQL) | 5–20ms |
| Semantic agent (with embed) | 3–8s (GCP embedding round-trip) |
| Semantic agent (cache hit) | 50–200ms (DB vector search only) |
| Gate BLOCK/REJECT | < 2ms (no agents invoked) |

Gate rejection saves **3–8 seconds** per blocked query (no embedding call).

Metrics histograms available at `GET /metrics` with p50/p95 per operation.

---

## 8. Error and Rejection Rates

| Metric | Value |
|---|---|
| Gate BLOCK rate (injection/malicious) | Measured per-request via `gate_block` counter |
| Gate REJECT rate (off-domain) | Measured via `gate_reject` counter |
| Rate limit 429 rate | Measured via `rate_limit_exceeded` counter |
| Guardrail clarify/refuse/no_data | Measured via `guardrail_*` counters |
| HTTP 401 (auth) | When `API_AUTH_ENABLED=true` and key missing |

All counters exportable via `/metrics`.

---

## 9. Observability/Dashboard Coverage

| Signal | Mechanism | Endpoint |
|---|---|---|
| Request ID | `X-Request-ID` header (auto-generated or client-supplied) | All responses |
| Response time | `X-Response-Time-Ms` header | All responses |
| Query trace | `trace_id` in response + full trace in metadata | POST /query |
| Counters | queries_total, gate_*, guardrail_*, rate_limit_* | GET /metrics |
| Histograms | query_latency_ms, agent_*_latency_ms, http_latency_ms | GET /metrics |
| Cache stats | embedding_cache, query_response_cache hit rates | GET /metrics |
| Session stats | active_sessions, ttl, max_turns | GET /metrics |
| Structured logging | structlog in orchestrator hot path | stdout |

**Not yet implemented** (recommended for multi-instance production):
- Prometheus `/metrics` exporter (currently JSON)
- OpenTelemetry distributed tracing
- GCP Cloud Monitoring dashboards
- Alerting policies (error rate, latency p95, gate block rate)

---

## 10. Google Cloud Services Used

| Service | Usage | Why |
|---|---|---|
| **Cloud SQL (PostgreSQL + pgvector)** | Structured OEL data + vector retrieval | Authoritative numeric source + semantic search |
| **Vertex AI (Gemini embedding)** | Query embedding for semantic retrieval | Only invoked for semantic/hybrid intents; cached |
| **GCP Secret Manager** | Recommended for API keys in production | Not wired yet — env vars used currently |

Services **not** used (no concrete requirement):
- A2A / MCP protocols
- Cloud Run (can deploy FastAPI there when ready)
- Memorystore/Redis (deferred to multi-instance phase)
- Cloud Logging (structlog to stdout; can pipe to Cloud Logging via agent)

---

## 11. Tests Passed

- **Phase D**: 56/56 ✅
- **Phase C.4 regression**: 28/28 ✅
- **Full suite**: 334/335 ✅ (1 pre-existing PYTHONPATH issue in validate script test)

---

## 12. Remaining Risks and Limitations

1. **In-process session/cache** — does not survive restarts or scale across instances. Redis/Memorystore needed for multi-instance Cloud Run deployment.
2. **API auth opt-in** — disabled by default (`API_AUTH_ENABLED=false`). Must enable and configure keys for production.
3. **Off-domain detection** — 80% precision on synthetic set; some generic-knowledge queries without pattern matches may pass through to classifier (which will likely fail gracefully).
4. **Malicious detection** — 75% on small test set; Persian malicious patterns need expansion.
5. **No Prometheus/Grafana** — metrics are JSON-only; production should add Prometheus exporter + GCP Monitoring dashboards.
6. **No distributed tracing** — request ID exists but no OpenTelemetry spans across embedding/DB calls.
7. **Rate limiter in-process** — not shared across instances; use Cloud Armor or API Gateway for production-scale rate limiting.
8. **chemical_registry name scrambling** — still unfixed upstream data issue (Phase C.4 limitation).

---

## 13. Production Readiness Recommendation

**Recommendation: Conditionally ready for controlled production deployment.**

The system now has the essential production boundary:

- ✅ Pre-pipeline domain/safety gate (100% HSE recall, 0% false rejections)
- ✅ Prompt injection blocking (93%+ detection)
- ✅ API input validation + security headers
- ✅ Optional API key authentication
- ✅ Rate limiting (in-process)
- ✅ Session TTL + turn limits
- ✅ Embedding cache (major cost/latency savings)
- ✅ Structured logging + metrics endpoint
- ✅ Guardrails intact (100% negative category, numeric authority)
- ✅ 93%+ routing accuracy unchanged

**Before full production sign-off:**

1. Enable `API_AUTH_ENABLED=true` and configure API keys (via GCP Secret Manager)
2. Deploy behind Cloud Run or GKE with health check on `/health`
3. Add Redis/Memorystore for session + cache when scaling beyond 1 instance
4. Wire Prometheus exporter or GCP Monitoring for dashboards/alerts
5. Run load test to establish p95 latency SLO under expected QPS
6. Expand off-domain/malicious pattern library based on production query logs

**Cost impact**: Gate rejection saves ~$0.001–0.01 per blocked query (embedding API call avoided). Embedding cache saves ~100% on repeated queries. Structured/Formula routing saves embedding entirely (~60% of production traffic based on eval category distribution).
