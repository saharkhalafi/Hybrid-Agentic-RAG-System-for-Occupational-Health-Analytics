# Extraction Quality Report — OHE6.pdf (first 10 pages)

Generated: 2026-08-03 12:33 UTC

Document ID: `d44caded-bf51-4637-8675-a140137bf5fa`

## Critical Foundation Finding

**Processor type:** `LAYOUT_PARSER_PROCESSOR` (OHE6Layoutparser)

**Parser compatibility:** The current `document_ai/parser.py` expects classic Document AI output (`pages[].tables[]`). The Layout Parser returns `documentLayout.blocks[]` with nested `tableBlock` structures. **The pipeline stored 0 tables and 0 cells in PostgreSQL** despite Document AI succeeding.

This report analyzes the raw saved JSON at `data/processed/8b9e2240e437_document_ai.json`.

## Aggregate Metrics

| Metric | Layout Parser (raw) | PostgreSQL (pipeline) |
|--------|--------------------:|----------------------:|
| Tables detected | 3 | 0 |
| Cells extracted | 103 | 0 |
| Average OCR confidence | N/A | N/A |
| Merged cells | 0 | — |
| Cells without bounding box | 103 | — |
| Cells without confidence | 103 | — |
| Review queue items | — | 0 |
| Formulas extracted | — | 0 |
| Suspicious values | 0 | — |

## Document AI Auth Note

gRPC via ADC returned **403 Permission Denied**. REST API with `gcloud auth print-access-token` succeeded. Run `gcloud auth application-default login` to fix the Python client.

## Detected Tables

### Table 1 — Page 7

| Field | Value |
|-------|-------|
| Detected type | `unknown` |
| Confidence | N/A (not in Layout Parser output) |
| Cells | 58 |

**Headers:** سمت, نام و نام خانوادگی

**First 5 rows:**

```
سمت | نام و نام خانوادگی
مسئول طرح | مهندس محسن فرهادی، دکتر سید محمد سید مهدی
مجری طرح | دکتر فاطمه صادقی گلوردی
مسئول هماهنگی اجرا | مهندس حسین طلعتی
 | اعضاء کمیته های علمی
```

### Table 2 — Page 8

| Field | Value |
|-------|-------|
| Detected type | `unknown` |
| Confidence | N/A (not in Layout Parser output) |
| Cells | 16 |

**Headers:** کمیته پایش بیولوژیکی و عوامل, 

**First 5 rows:**

```
کمیته پایش بیولوژیکی و عوامل | 
 | دکتر سید جمال الدین شاه طاهری
بیولوژیک | 
" | دکتر محمد جواد عصاری
" | دکتر ابوالفضل مقدسی کوچکسرائی
```

### Table 3 — Page 9

| Field | Value |
|-------|-------|
| Detected type | `chemical_oel` |
| Confidence | N/A (not in Layout Parser output) |
| Cells | 29 |

**Headers:** حدود مجاز مواجهه شغلی - وزارت بهداشت ، درمان و آموزش پزشکی

**First 5 rows:**

```
حدود مجاز مواجهه شغلی - وزارت بهداشت ، درمان و آموزش پزشکی
کانال تلگرامی جامعه ی بهداشت حرفه ای و ایمنی کار بروز بمانید:
https://t.me/Ch_OHSSociety
فهرست مطالب
۴۰۰ پیام وزیر .
```

## Data Quality Issues

### Cells without bounding boxes
All 103 cells lack bounding box data. Spatial evidence traceability is not available for Layout Parser output in current form.

### Cells without confidence
All 103 cells lack confidence scores. Automated review routing cannot work on cell-level confidence yet.

### Suspicious numeric values
- None in pages 1–10 front matter

---

## Task 6 Recommendations (DO NOT IMPLEMENT YET)

### 1. Tables needing dedicated domain schemas
| Type | Evidence | Pages |
|------|----------|-------|
| `chemical_oel` | CAS, TWA, STEL, ppm, mg/m³ | 46–50+ (Section 1) |
| `biological_monitoring` | BEI, specimen | Section 2 (~182+) |
| `vibration` | A(8), VDV | Section 3 (~218+) |
| `noise` | LAeq, dBA, dose | Section 3 (~218+) |

### 2. Tables that should remain generic
- Committee member tables (pages 7–8)
- Table of contents (page 9)
- Cover/metadata pages (1–4)

### 3. Extraction failures requiring vision recovery
- **Pages 1–10:** Not needed — all have digital text
- **Blocker:** Layout Parser JSON not parsed by pipeline
- **Future:** Evaluate pages 46–60 for column alignment issues in chemical tables

### 4. Recommended confidence thresholds
| Gate | Threshold |
|------|-----------|
| Domain table promotion | ≥ 0.80 composite (once confidence available) |
| Review queue | ≥ 0.65 |
| Vision recovery | Triage table-heavy AND Document AI tables = 0 |
| Block domain insert | Missing bbox OR confidence |
