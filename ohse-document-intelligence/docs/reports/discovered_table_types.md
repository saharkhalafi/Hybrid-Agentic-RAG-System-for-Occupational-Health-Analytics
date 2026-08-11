# Discovered Table Types — OHE6.pdf

Generated: 2026-08-03 12:33 UTC

## chemical_oel

**Pages (50-page text pattern):** [9, 23, 24, 25, 26, 27, 28, 29, 30, 31, 32, 33, 34, 35, 37, 42, 43, 44, 46, 47, 48, 49, 50]

**Pages (Document AI, first 10):** none — front matter only

**Expected headers:** CAS, Chemical name (EN/FA), TWA, STEL, Ceiling, ppm, mg/m³, notations (Skin, DSEN, etc.)

**Notes:** Chemical registry tables dominate from ~page 46 onward. PyMuPDF text shows CAS patterns e.g. `Acrolein [107-02-8]`, `Aldicarb [116-06-3]`.

## vibration

**Pages (50-page text pattern):** [12]

**Expected headers:** A(8), VDV, action limit, exposure limit, axis

## noise

**Pages (50-page text pattern):** none in first 50 pages

**Expected headers:** LAeq, dBA, dose, duration, criterion level

## biological_monitoring

**Pages (50-page text pattern):** [10, 36, 43, 46, 48]

**Expected headers:** BEI, indicator, specimen, sampling time

**Notes:** BEI references appear in TOC (page 10) pointing to Section 2 page 182+.

## unknown / generic

**Document AI tables (pages 1–10):**

| Page | Content | Classification |
|------|---------|----------------|
| 7 | Committee members — scientific committees | `unknown` (administrative) |
| 8 | Committee members — biological monitoring | `unknown` (administrative) |
| 9 | Table of contents | `unknown` (navigational) |

**Cover pages 1–4, 5–6:** No tables detected by Layout Parser — narrative text only.

## Key Finding

Processing pages 1–10 validates pipeline orchestration and triage, but **does not validate chemical OEL extraction**. Next evaluation should target **pages 46–55**.
