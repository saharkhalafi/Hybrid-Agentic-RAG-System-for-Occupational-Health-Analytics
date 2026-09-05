# Table-value extraction and retrieval architecture

This document describes the **implemented** path that turns OHE6 chemical-OEL tables into authoritative STEL/TWA values. Names are modules, classes, and functions that exist in the repository. It is not a proposed design.

Numeric values are never invented. If a cell cannot be parsed, status is `absent` or `extraction_uncertain` (`pipeline_contracts.numeric_integrity.parse_numeric_cell`, `TableGoldGenerator._overlay_pdf_stel_twa`).

## End-to-end path

```
PDF (OHE6)
  → Document AI evidence (cached under data/; DocumentAIProcessor / adapters)
  → goldset_generator.structural_resolver.resolve_structure
       ↳ optional ingestion.table_recovery.recover_chemical_oel_table
       ↳ ingestion.merged_row_splitter.split_table_rows_by_cas_geometry
  → write_validated_structure (Layer-2 JSON, e.g. validated_structure_46-55.json)
  → goldset_generator.table_gold_generator.TableGoldGenerator.generate
       ↳ pipeline_contracts.header_reconstruction.reconstruct_header_structure
       ↳ _build_header_mapping / resolve_field_name
       ↳ _overlay_pdf_stel_twa → recover_stel_twa_from_words
       ↳ goldset_generator.oel_row_parser.enrich_oel_rows
       ↳ goldset_generator.validator.GoldsetValidator
  → persistence.evidence_pipeline.persist_validated_rows_and_domain
       ↳ validation_engine.engine.ValidationEngine.validate_table_row
       ↳ oel_chemical_limits + validated_table_rows
  → query time: agents.routing.chemical_resolver + agents.structured.store.PostgresStructuredStore.lookup_oel_field
```

Table-value **evaluation against `goldset.xlsx`** stops at Layer-2 cells (`tests/test_goldset_excel_46_55.py`). Domain persist uses **table gold after overlay**, not the raw Layer-2 STEL/TWA columns.

## 1. Table and row reconstruction

`resolve_structure()` (`goldset_generator/structural_resolver.py`) is Layer 2. Documented order in that function:

1. Geometry alignment — `_align_evidence_cells`
2. Page/table detection — `table_detection_gate.evaluate_table_detection`, `_is_chemical_oel_table`
3. Visual row refinement
4. Multi-CAS split — `_split_oel_multi_cas_rows` (must run **before** logical merge)
5. Logical rows — `_reconstruct_oel_logical_rows` (also `document_ai.logical_row_reconstructor.reconstruct_oel_logical_rows` for 6-column OEL)
6. Column expansion — `_expand_oel_limit_columns`, `_expand_oel_basis_symbols_column`
7. Merged-cell marking — `_is_merged_cell`
8. PyMuPDF recovery when DAI is unreliable — `_needs_pymupdf_recovery`, `_dai_oel_grid_repairable_in_place`, `_apply_recovered_oel_structure`

`ingestion.merged_row_splitter.split_table_rows_by_cas_geometry` / `_split_row_by_cas_geometry` splits a visual row when multiple CAS y-bands sit in one DAI row (`CasGeometry`, `group_cas_by_pdf_row`).

`ingestion.table_recovery.recover_chemical_oel_table` rebuilds a grid from `PageGeometryIndex` words when recovery is selected (`RecoveredTable`, `_find_row_anchors`, `_assign_column_by_boundary`).

Metrics on `StructuralResolverResult`: `logical_row_reconstruction_count`, `multi_cas_visual_row_split_count`, `geometry_aligned_cell_count`, `recovered_table_count`.

## 2. Cell extraction

Layer-1 cells come from `document_ai.parser.parse_document_ai_response` and adapters (`ClassicDocumentAIAdapter`, `LayoutParserAdapter`) into `ExtractionCell` / `ExtractedCellRecord`.

Layer-2 cells carry `text`, `normalized_value`, `bbox`, `column`, `cell_id`, `source_reference`. `TableGoldGenerator.generate` maps each cell through `resolve_field_name` and `_field_from_cell` / `_parse_exposure_value` / `parse_numeric_cell`. Symbols and health-effect fields keep text (`_set_field`); they are not forced through numeric parse.

`ingestion.merged_row_splitter.reconstruct_limit_expression_from_pdf_words` and `repair_oel_numeric_from_pdf_headers` rebuild limit strings from PDF words when DAI limit text is corrupted (`_da_limit_text_is_corrupted`).

## 3. Bbox / geometry resolution

- `document_ai.geometry_resolver.GeometryResolver` — match cell text to PDF words (`document_ai_bbox_to_xywh`, `is_valid_bbox`).
- `document_ai.bbox_provenance.is_trusted_document_ai_bbox` / `resolve_bbox_inputs`.
- `document_ai.geometry_gating.evaluate_cell_geometry_gate` — `ACCEPT` / `MISSING_BBOX` / `LOW_CONFIDENCE`; low-confidence boxes are not persisted.
- `document_ai.geometry_promotion.resolve_promotion_cell` — gated promotion with row-anchor retry (`resolve_cells_with_row_anchor_retry`).
- `document_ai.geometry_validation_checks` — `is_numeric_wrong_row_match`, `has_cross_row_bbox_union`, `should_flag_row_y_mismatch`.
- `ingestion.geometry_resolver.PdfGeometryResolver` / `ResolvedCellGeometry`.
- Overlay gates in `table_gold_generator`: `pdf_geometry_untrusted_fields`, `pdf_identity_fill_fields`, `_bboxes_cloned`, `_bbox_spans_stel_and_twa`, `_physical_column_of_bbox`, `pdf_row_identity_matches`, `pdf_unique_row_number_y_span`, `_row_limit_y_band`.

## 4. STEL / TWA column mapping

Physical OEL columns (`oel_row_parser.STANDARD_OEL_COLUMN_MAP`):

| column | field |
|--------|--------|
| 0 | health_effect |
| 1 | symbols |
| 2 | STEL |
| 3 | TWA |
| 4 | molecular_weight |
| 5 | chemical_name |
| 6 | row_number |

`TableGoldGenerator._build_header_mapping` + `CHEMICAL_HEADER_MAP` / `_match_header`:

- If a header cell contains both `stel` and `twa` and MW is at `column + 2`, map `column → STEL`, `column+1 → TWA` (two physical subcolumns under a merged Persian header).
- Otherwise map that cell to `TWA_STEL` (one physical cell; `_parse_combined_exposure_values`).
- Positional `STANDARD_OEL_COLUMN_MAP` is applied only when fewer than two headers were detected — so missing physical columns are not filled with phantom TWA mappings.

`pipeline_contracts.header_reconstruction.reconstruct_header_structure` supplies `header_mapping`, `header_row_indices`, `data_row_start`. `analyze_header_mapping` can set `mapping_status=review_required`.

PDF x-bands (`recover_stel_twa_from_words`): words in the row y-band are assigned by `_assign_column_by_boundary`; **column 2 = STEL/C (left of the pair), column 3 = TWA**. Assignment is physical x, not DAI reading order.

## 5. CAS / chemical identification

- Layer 2: `structural_resolver.extract_cas_values` / `normalize_cas` (single CAS source of truth in that module).
- Gold eval: `tests.test_goldset_excel_46_55.normalize_cas` (`\d{2,7}-\d{2}-\d`).
- Entities: `goldset_generator.chemical_entity_extractor.extract_chemical_entities_from_cell` → `ChemicalEntity`.
- `TableGoldGenerator._apply_chemical_entity`, `_clean_chemical_name`; `oel_row_parser.split_chemical_segments`, `extract_persian_names_from_row_number`.
- Query time: `agents.routing.chemical_resolver` (CAS pattern aligned with structural_resolver / merged_row_splitter).
- Authoritative lookup: `PostgresStructuredStore.lookup_oel_field` / `get_oel_by_cas` on `oel_chemical_limits` with `CANONICAL_GOLD_ARTIFACT_PATH = "canonical_evidence_v1"`.

## 6. Merged-cell handling

- `_is_merged_cell` (resolver and `table_gold_generator`): `source_reference.value_status == "merged_cell"`, or CAS + limit markers in one cell, or multiple CAS in one cell.
- `_split_merged_limit_header_text`, `_limit_substrings_from_merged_cell`, `_clone_expanded_limit_cell`, `_expand_oel_limit_columns` — split a merged STEL+TWA header/cell into two logical columns without inventing numbers.
- `_merge_casless_name_rows` / `_merge_visual_row_cells` — continuation name rows, not independent chemicals.
- Overlay **refuses** to run when STEL and TWA share the same `cell_id` (`_overlay_pdf_stel_twa` early return) so a single merged source is not blindly written as two independent limits.

## 7. RTL / LTR

- `oel_row_parser._is_rtl_reversed_mw` / `parse_molecular_weight` / `parse_layer2_molecular_weight` — slash MW tokens such as `08/71` → `71.08` only when the RTL pattern matches. `parse_layer2_molecular_weight` is conservative (eval/Layer-2).
- `ingestion.pdf_geometry.repair_reversed_cas_brackets`.
- Persian OCR repair: `persian_text_repair.repair_ocr_fragments`, `persian_text_reconstructor.reconstruct_block_text` (narrative; not used to invent STEL/TWA).
- Limit overlay uses **x-band**, so RTL text order inside a DAI cell cannot swap STEL and TWA if geometry is trusted.

## 8. Validation and canonical data

- `GoldsetValidator.validate_table_field` — numeric mismatch / critical issues attached on `TableGoldGenerator.generate`.
- `pipeline_contracts.table_quality_gate.evaluate_table_quality`.
- `validation_engine.engine.ValidationEngine.validate_table_row` before persist.
- `persist_validated_rows_and_domain` writes `ValidatedTableRow` + `OELChemicalLimit` with `field_provenance` (`cell_id`, `bbox`, `value_status`, `original_value`) via `fact_resolver.resolve_field_display`.
- Review: `EXTRACTION_UNCERTAIN` (`pipeline_contracts.validation_codes`) goes to HITL, not silent domain write.
- Structured reads are canonical-only (`PostgresStructuredStore`); legacy rows without provenance are `legacy_reference` / `PENDING_PROMOTION`, not query answers.

## 9. Design decisions that prevent wrong STEL/TWA assignment

1. **Identity is page + CAS, not physical row index** (`find_actual_row`). Reconstruction/split changes row indexes; matching on row number would pair the wrong chemical.
2. **Do not replace a usable DAI table merely because quality is low** (`_dai_oel_grid_repairable_in_place`, `_needs_pymupdf_recovery`). Full PyMuPDF replacement of an 8-column OEL grid destroys cloned geometry that overlay was written to repair.
3. **Overlay is gated.** `pdf_overrides_complete_dai_limits` may replace complete DAI numbers **only** when PDF x-bands are a permutation of the same two decimals (column swap). Incomplete or different PDF numbers must not overwrite both DAI limits.
4. **Cloned / spanning / wrong-band bboxes are untrusted** (`pdf_geometry_untrusted_fields`). Shared bbox, bbox spanning both limit columns, STEL x-center in TWA band, or limit parked in MW band → replace from `recover_stel_twa_from_words`.
5. **Row identity must be proven** (`pdf_row_identity_matches` / `pdf_unique_row_number_y_span`) before filling empty limits from a y-band, so the previous/next chemical’s words are not taken.
6. **STEL and TWA from the same `cell_id` are not overlaid as two values.**
7. **Header mapping does not invent columns.** Merged Persian “TWA + STEL/C” headers must not get a duplicate TWA at a column that does not exist.
8. **LaTeX OCR in limit cells is `extraction_uncertain`**, not treated as absent (`LATEX_ARTIFACT_PATTERN` in `table_gold_generator`).
9. **Query path does not re-parse the PDF.** `lookup_oel_field` returns persisted canonical fields. Semantic retrieval must not be used as the authority for STEL/TWA numbers.

## 10. What the Gold Excel evaluator actually measures

`validate_stel_twa` compares Gold Excel to **Layer-2 column text**, not to `TableGoldGenerator` overlay and not to Postgres. A Layer-2 STEL/TWA swap or empty cell can still be repaired later by `_overlay_pdf_stel_twa` before `persist_validated_rows_and_domain`. Treat Gold-vs-Layer-2 misses as **extraction/geometry**, and Gold-vs-query misses (separate `GoldsetQAEvaluator`) as **retrieval / routing**, unless proven otherwise.
