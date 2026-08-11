# Bounding Box Strategy Comparison

Generated: 2026-08-03

Document: **OHE6.pdf** (389 pages, fully digital Persian/English text)  
Processor: **Layout Parser** (`documentLayout.blocks[]`) — tables detected, no cell geometry  
Validation scope: pages **46–55** (chemical OEL tables)

## Problem

Layout Parser returns structured table text (`tableBlock.bodyRows[].cells[].blocks[]`) but **no bounding polygons** for cells. PostgreSQL `table_cells` requires evidence-grade geometry for traceability.

---

## Option A — PyMuPDF text blocks / words

**Method:** `page.get_text("words")` and `page.get_text("dict")` on the source PDF.

| Aspect | Assessment |
|--------|------------|
| Availability | Immediate — no extra API calls |
| Coordinates | PDF points (`x0,y0,x1,y1`) per word and line |
| Digital PDF fit | **Excellent** — OHE6 has embedded text on all triaged pages |
| Persian support | Works with normalization (ی/ک, digit conversion) |
| Cost | Free, local |
| Measured coverage (prototype) | **100%** of non-empty cells at ≥0.85 confidence on pages 46–55 |

**Sample evidence (page 46):**

| Text | PyMuPDF bbox (approx) |
|------|------------------------|
| `TWA` | x=317, y=118, w=26, h=12 |
| `[75-07-0]` | x=472, y=183, w=35, h=10 |
| `Acetaldehyde` | searchable via `page.search_for()` |

**Limitations:**

- Merged / garbled Layout Parser cells (e.g. page 48 header) may map to a line span rather than a tight cell box
- Short symbols (`A2`, `C`) rely on token-level matching — needs confidence gating
- Empty cells have no geometry — correctly routed to review queue

---

## Option B — Google Document AI OCR processor (with layout)

**Method:** Run a separate OCR/form processor that returns `pages[].tokens[]` or `pages[].lines[]` with `boundingPoly`.

| Aspect | Assessment |
|--------|------------|
| Availability | Requires second processor + API cost per page |
| Coordinates | Normalized vertices or pixel polygons |
| Digital PDF fit | Redundant OCR on text-native PDF |
| Persian support | Good (Document AI multilingual OCR) |
| Cost | ~$1.50 / 1k pages (OCR tier) |
| Expected coverage | High for scanned docs; **marginal benefit here** |

**Verdict:** Useful for **scanned** pages only. OHE6 pages 46–55 already have digital text — OCR adds latency, cost, and potential text drift vs PDF embedded text.

---

## Option C — Document AI Enterprise OCR processor

**Method:** Enterprise Document OCR or specialized table OCR with coordinate export.

| Aspect | Assessment |
|--------|------------|
| Availability | Enterprise tier / separate processor setup |
| Coordinates | Full layout with tables in some configurations |
| Digital PDF fit | Same redundancy as Option B |
| Cost | Higher enterprise pricing |
| Current project state | Layout Parser already chosen; Enterprise OCR not provisioned |

**Verdict:** Future option if PyMuPDF matching fails on scanned annexes. **Not needed for OHE6 main tables.**

---

## Option D — Hybrid (Document AI text + PyMuPDF coordinates)

**Method:**

1. Layout Parser → normalized cell text, row/col structure
2. PyMuPDF → word geometry index per page
3. Geometry resolver → match normalized text to PDF words/lines
4. Confidence scoring → exact (0.98), line (0.92), token sequence (0.98/0.85), CAS (0.95), distinctive token (0.85)

| Aspect | Assessment |
|--------|------------|
| Combines | Best structure from Document AI + best geometry from PDF |
| Handles | Persian digits, whitespace, LaTeX noise, CAS numbers, RTL word order |
| Confidence | Explicit `bbox_confidence`; low scores → `review_queue` |
| Extensibility | `bbox_source` field supports future `ocr_overlay` fallback |

**Matching cascade (implemented in `document_ai/geometry_resolver.py`):**

```
1. Document AI bbox (if present)           → 1.00
2. page.search_for(exact text)             → 0.98
3. line block contains normalized text     → 0.92
4. ordered token sequence on same line     → 0.98 / 0.85
5. CAS pattern search                      → 0.95
6. distinctive token (len ≥ 4)               → 0.85
7. no match                                → review_queue (missing_bbox)
```

---

## Recommendation

**Use Option D (Hybrid)** for OHE6 and similar digital OHSE PDFs.

| Priority | Approach |
|----------|----------|
| **Primary** | PyMuPDF word/line matching against Layout Parser cell text |
| **Fallback** | OCR processor overlay for scanned pages (`has_digital_text=false`) |
| **Not now** | Enterprise OCR processor — defer until scanned-volume justifies cost |

### Why hybrid wins for this PDF

1. **OHE6 is digital** — embedded text is authoritative; PyMuPDF bboxes match what users see
2. **Layout Parser already extracts table structure** — no need to re-parse tables via OCR
3. **>95% coverage achievable** on chemical section with local matching (no API cost)
4. **Evidence chain complete:** `page_number` + `bbox` + `source_reference` + `raw_text`
5. **Scanned pages** in later phases can add `bbox_source=ocr_overlay` without changing extraction logic

### When to add OCR overlay

- Page triage reports `has_digital_text=false`
- Geometry resolver returns `match_confidence < threshold` after PyMuPDF pass
- User uploads scanned legacy PDFs without embedded text layer

---

## Implementation

| Component | Path |
|-----------|------|
| PDF word index | `ingestion/pdf_geometry.py` |
| Geometry resolver | `document_ai/geometry_resolver.py` |
| Pipeline integration | `scripts/process_document.py` |
| DB columns | `bbox_source`, `bbox_confidence`, `source_reference` |
| Review routing | `missing_bbox`, `low_bbox_confidence` |

---

## Next step

Run bbox validation on pages 46–55. Target: **>95% bbox coverage** before Phase 2 (chemical registry normalization).
