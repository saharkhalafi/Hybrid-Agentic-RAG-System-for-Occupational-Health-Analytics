# Semantic Text Pipeline Report

Range: **21–387**
Generated: 2026-08-08T08:45:34.433865+00:00

## Summary

- Total semantic chunks: 898
- Pages with narrative chunks: 300
- Chunks ready (no validation issues): 728
- Chunks requiring review: 170
- Duplicate chunks detected: 0
- Empty chunks detected: 0

## Retrieval routing

- Narrative / semantic questions → `gold/rag/semantic_text.jsonl`
- Structured table / numeric questions → `gold/tables/`
- Formula / calculation questions → `gold/formulas/`
- Entity lookup → `gold/entities/`

## Validation

Checks applied: empty chunks, duplicates, broken Persian, OCR artifacts, missing provenance, table-row leakage, formula-body without reference, orphan cross-links.

JSON report: `E:\cursor projects\HSE6 AI Agent\ohse-document-intelligence\docs\reports\semantic_text_report.json`