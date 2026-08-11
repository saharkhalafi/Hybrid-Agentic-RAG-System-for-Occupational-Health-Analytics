# Table Pipeline — Before/After (Pages 46–55, Real OHE6.pdf)

**Run date:** 2026-08-09  
**PDF:** `E:\cursor projects\HSE6 AI Agent\OHE6.pdf`  
**promote_gold:** false | **HITL refreshed:** false

Full diagnostics:
- BEFORE: [`table_pipeline_diagnostic_46-55_BEFORE.json`](table_pipeline_diagnostic_46-55_BEFORE.json)
- AFTER: [`table_pipeline_diagnostic_46-55_AFTER.json`](table_pipeline_diagnostic_46-55_AFTER.json)

---

## Executive comparison

| Page | Before cols | After cols | Before recovery | After recovery | Before gold | After structural gate | **Final status** |
|------|-------------|------------|-----------------|----------------|-------------|----------------------|------------------|
| 46 | 6 | **7** | none (DAI only) | pymupdf_geometry | blocked | gate passes* | **REVIEW_REQUIRED** |
| 47 | 6 (corrupt) | **7** | partial | pymupdf_geometry | blocked | gate passes* | **REVIEW_REQUIRED** |
| 48 | 6 | **7** | partial | pymupdf_geometry | blocked | gate passes* | **REVIEW_REQUIRED** |
| 49 | 6 | **7** | none | pymupdf_geometry | blocked | gate passes* | **REVIEW_REQUIRED** |
| **50** | **7** | **7** | none (DAI) | **document_ai** | **allowed** | gate passes* | **REVIEW_REQUIRED** |
| 51 | 6 | **7** | none | pymupdf_geometry | blocked | gate passes* | **REVIEW_REQUIRED** |
| 52 | 6 | **7** | none | pymupdf_geometry | blocked | gate passes* | **REVIEW_REQUIRED** |
| 53 | 6 | **7** | none | pymupdf_geometry | blocked | gate passes* | **REVIEW_REQUIRED** |
| 54 | 6 | **7** | none | pymupdf_geometry | blocked | gate passes* | **REVIEW_REQUIRED** |
| 55 | 6 | **7** | none | pymupdf_geometry | blocked | gate passes* | **REVIEW_REQUIRED** |

\*Structural quality gate reports `gold_allowed=true` because numeric mismatch issues from the post-extraction validator are not yet wired into the gate during generation. **All 10 pages are marked REVIEW_REQUIRED** based on cell-level validation and header mapping inspection.

**Recovery triggered:** 9/10 pages (all except page 50, which already had 7 Document AI columns).

---

## What improved (geometry-first)

1. **6-column Document AI tables** (pages 46, 49, 51–55) now trigger PyMuPDF recovery automatically when column count < 7.
2. **All pages resolve to 7 physical columns** with bboxes from PDF geometry.
3. **Page 50 (reference)** retains Document AI structure with correct header hierarchy:
   ```
   حد مجاز مواجهه شغلی
       ├── C2 → STEL/C
       └── C3 → TWA
   ```
4. **Row numbers separated** — e.g. page 46 row 1: `row_number=1` in C6 (x≈555), not merged with chemical text.
5. **Candidates written** to `gold/candidates/tables/` — NOT promoted to `gold/tables/`.

---

## What still fails (must fix before Gold)

### Recovered pages (46–49, 51–55): merged data cells

PyMuPDF recovery assigns 7 column indices but **data rows still contain mega-cells** spanning most of the table width. Example page 46, row 1 (Acephate):

| Field | original_value source | Problem |
|-------|----------------------|---------|
| chemical_name | single cell C5, bbox width **485px** | CAS + MW + STEL + TWA + name in one cell |
| TWA | parsed `0.3` from mega-cell | original_value is entire row blob, not `0.3` |
| molecular_weight | parsed `183.16` from mega-cell | same mega-cell provenance |

This violates: *"Physical placement must be determined from geometry/x-bands first."*

### Recovered pages: header mapping incomplete

Recovered header trees merge STEL/C label into C1 (symbols):

```
C1: symbols [نمادها → STEL/C]   ← should be separate
C2: column_2 [ز مواجهه شغلی]    ← unmapped, missing exposure_limit.stel_c
C3: exposure_limit.twa
```

Page 50 (Document AI) correctly maps C2→STEL/C, C3→TWA independently.

### Page 47 specific

- **Before:** Document AI mega-header with embedded CAS, MW, limits, row numbers `۹ ۱۰ ۱۱ ۱۲` in header row; rows 1–5 empty.
- **After:** 7 columns, 8 rows, PyMuPDF bboxes on all cells, row numbers separated.
- **Still wrong:** data limits/MW/chemical not split into distinct physical columns; header hierarchy incomplete.

---

## Page 50 reference row (Aluminum, row 32) — known good

| Field | original_value | value | normalized |
|-------|----------------|-------|------------|
| row_number | ۳۲ | 32 | 32 |
| chemical_name | فلز آلومینیوم… Aluminum metal [7429-90-5] | Aluminum metal | — |
| CAS | *(derived from chemical cell)* | 7429-90-5 | — |
| molecular_weight | ۲۶/۹۸ | ۲۶/۹۸ | 26.98 |
| TWA | 1mg/m³ | 1 | 1 |
| symbols | A4 | A4 | — |

Separate cells per column. Header hierarchy valid. Independent Document AI path (not copied to other pages).

---

## Artifacts generated

| Artifact | Path |
|----------|------|
| Document AI evidence | `data/evidence/16518156ffd4_46-55/` |
| Validated structure | `data/intermediate/validated_structure_46-55.json` |
| Canonical grids | `data/canonical/grids/16518156ffd4_46-55/` |
| OEL candidates (not gold) | `gold/candidates/tables/table_*_01.json` |

---

## Next fix (before Gold promotion)

1. **Word-level x-band splitting for data rows** — don't assign wide paragraphs to a single column; split by word x-position like header splitting.
2. **Wire post-extraction numeric validator into quality gate** — so `gold_allowed=false` when CRITICAL numeric mismatches exist.
3. **Re-run** `scripts/rerun_structural_pipeline_46_55.py` after fixes and verify page 47 + 6-column-origin pages match page 50 column separation quality.

**Do NOT run `--promote-gold` or HITL bridge until recovered pages pass cell-level validation.**
