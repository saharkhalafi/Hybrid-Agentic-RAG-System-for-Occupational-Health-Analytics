# Bbox Validation Report — Chemical Section (Pages 46–55)

Generated: 2026-08-03 12:59 UTC

## Summary

| Metric | Value |
|--------|------:|
| Document ID | `cebe3c71-c601-406e-98be-340cd6a81479` |
| Total cells inserted | 387 |
| Cells with bbox | 387 |
| Overall bbox coverage | 70.62% |
| **Non-empty cell bbox coverage** | **100.0%** |
| Average bbox confidence | 0.9495 |
| Review queue — missing bbox | 161 |
| Review queue — low bbox confidence | 0 |
| Empty cell reviews (expected) | 161 |
| Non-empty unmatched cells | 0 |

Target: **>95% non-empty cell bbox coverage** before Phase 2 domain normalization.

## Bbox sources

- `pymupdf`: 387 cells

## Example evidence cells

### Chemical name — page 46

- Text: `استالدئید Acetaldehyde [75-07-0]`
- Bbox: `{'x': 420.54998779296875, 'y': 168.98370361328125, 'width': 156.05218505859375, 'height': 24.05029296875}`
- Source: `pymupdf`
- Confidence: 0.98
- Reference: `{'table_id': '1', 'row_index': 2, 'page_width': 694.7999877929688, 'page_height': 496.0799865722656, 'page_number': 46, 'column_index': 4, 'match_method': 'exact', 'matched_text': 'استالدئید Acetaldehyde [75-07-0]', 'document_path': '..\\OHE6.pdf', 'matched_words': ['استالدئید Acetaldehyde [75-07-0]']}`

### CAS number — page 46

- Text: `استالدئید Acetaldehyde [75-07-0]`
- Bbox: `{'x': 420.54998779296875, 'y': 168.98370361328125, 'width': 156.05218505859375, 'height': 24.05029296875}`
- Source: `pymupdf`
- Confidence: 0.98
- Reference: `{'table_id': '1', 'row_index': 2, 'page_width': 694.7999877929688, 'page_height': 496.0799865722656, 'page_number': 46, 'column_index': 4, 'match_method': 'exact', 'matched_text': 'استالدئید Acetaldehyde [75-07-0]', 'document_path': '..\\OHE6.pdf', 'matched_words': ['استالدئید Acetaldehyde [75-07-0]']}`

### TWA limit — page 52

- Text: `حد مجاز مواجهه شغلی STEL/C TWA`
- Bbox: `{'x': 244.1300048828125, 'y': 97.34335327148438, 'width': 28.118865966796875, 'height': 12.221282958984375}`
- Source: `pymupdf`
- Confidence: 0.92
- Reference: `{'table_id': '384', 'row_index': 0, 'page_width': 694.7999877929688, 'page_height': 496.0799865722656, 'page_number': 52, 'column_index': 2, 'match_method': 'exact', 'matched_text': 'حد مجاز مواجهه شغلی STEL/C TWA', 'document_path': '..\\OHE6.pdf', 'matched_words': ['STEL']}`

### STEL limit — page 52

- Text: `حد مجاز مواجهه شغلی STEL/C TWA`
- Bbox: `{'x': 244.1300048828125, 'y': 97.34335327148438, 'width': 28.118865966796875, 'height': 12.221282958984375}`
- Source: `pymupdf`
- Confidence: 0.92
- Reference: `{'table_id': '384', 'row_index': 0, 'page_width': 694.7999877929688, 'page_height': 496.0799865722656, 'page_number': 52, 'column_index': 2, 'match_method': 'exact', 'matched_text': 'حد مجاز مواجهه شغلی STEL/C TWA', 'document_path': '..\\OHE6.pdf', 'matched_words': ['STEL']}`

## Notes

- Geometry resolver uses PyMuPDF hybrid matching (`bbox_source=pymupdf`).
- Empty cells and unmatched text are routed to `review_queue`.
- Cells with `bbox_confidence < 0.85` are routed to `review_queue` as `low_bbox_confidence`.
