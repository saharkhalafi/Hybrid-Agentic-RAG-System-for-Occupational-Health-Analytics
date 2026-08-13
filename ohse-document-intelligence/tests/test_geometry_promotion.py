"""Tests for shared geometry promotion retry."""

from document_ai.geometry_promotion import PromotionCellInput, resolve_cells_with_row_anchor_retry


class _FakePage:
    def search_for(self, _query: str):
        return []


class _FakeResolver:
    def __init__(self):
        self._row_anchors: set[tuple[str, int]] = set()

    def resolve(self, cell_input, document_ai_bbox=None, bbox_provenance=None):
        del document_ai_bbox, bbox_provenance
        from types import SimpleNamespace

        is_name = cell_input.column_index == 5
        if is_name:
            self._row_anchors.add((cell_input.table_id, cell_input.row_index))
            return SimpleNamespace(
                bbox={"x": 1, "y": 2, "width": 3, "height": 4},
                bbox_source="pymupdf",
                match_method="exact",
                match_confidence=0.99,
                source_reference={"page_number": cell_input.page_number},
            )
        has_anchor = (cell_input.table_id, cell_input.row_index) in self._row_anchors
        if has_anchor and cell_input.cell_text.strip() == "1 ppm":
            return SimpleNamespace(
                bbox={"x": 1, "y": 2, "width": 3, "height": 4},
                bbox_source="pymupdf",
                match_method="exact",
                match_confidence=0.99,
                source_reference={"page_number": cell_input.page_number},
            )
        return SimpleNamespace(
            bbox=None,
            bbox_source=None,
            match_method="none",
            match_confidence=0.0,
            source_reference={"page_number": cell_input.page_number},
        )


def test_second_pass_recovers_limit_after_name_anchor(monkeypatch):
    from config import settings as settings_module

    monkeypatch.setattr(settings_module.get_settings(), "bbox_confidence_threshold", 0.85)

    resolver = _FakeResolver()
    cells = [
        PromotionCellInput(
            page_number=50,
            table_id="table_050_01",
            cell_id="limit",
            row_index=4,
            column_index=3,
            text="1 ppm",
        ),
        PromotionCellInput(
            page_number=50,
            table_id="table_050_01",
            cell_id="name",
            row_index=4,
            column_index=5,
            text="2-Aminobutanol [96-20-8]",
        ),
    ]
    results = resolve_cells_with_row_anchor_retry(
        resolver,
        cells,
        threshold=0.85,
        page_lookup=lambda _page: _FakePage(),
    )
    assert results["limit"].persist is True
    assert results["limit"].resolved_pass == 2
