"""Column identity for STEL/TWA vs MW from X geometry, not word order."""

from __future__ import annotations

from ingestion.merged_row_splitter import (
    CasGeometry,
    group_cas_by_pdf_row,
    reconstruct_limit_expression_from_pdf_words,
    split_table_rows_by_cas_geometry,
    _cluster_words_by_x,
    _pick_limit_cluster,
    _reconstructed_limit_has_unit,
    _split_limit_cell_from_pdf,
    _split_nonchemical_cell_by_geometry,
)


class _FakePage:
    def __init__(self, words):
        self._words = words
        self.number = 0

    def get_text(self, mode):
        assert mode == "words"
        return self._words


def _w(x0, y0, x1, y1, text):
    return (x0, y0, x1, y1, text, 0, 0, 0)


def _header_words(y=80.0):
    return [
        _w(240, y, 280, y + 12, "STEL/C"),
        _w(310, y, 340, y + 12, "TWA"),
        _w(375, y, 410, y + 12, "MW"),
    ]


def _limit_page(extra):
    return _FakePage(_header_words() + extra)


def test_columns_follow_header_x_not_token_order():
    # Tokens listed TWA-first, then MW, then STEL — X geometry still wins.
    page = _limit_page(
        [
            _w(312, 140, 322, 152, "10"),
            _w(322, 140, 345, 152, "mg/m"),
            _w(345, 140, 350, 148, "3"),
            _w(379, 140, 395, 152, "252"),
            _w(395, 140, 400, 152, "/"),
            _w(400, 140, 410, 152, "07"),
            _w(245, 140, 255, 152, "20"),
            _w(255, 140, 278, 152, "mg/m"),
            _w(278, 140, 283, 148, "3"),
        ]
    )
    groups = [["100-00-0"]]
    cas_y = {"100-00-0": 145.0}
    stel, _ = _split_limit_cell_from_pdf(
        page, groups, cas_y, limit_slot=0, limit_slot_count=2
    )
    twa, _ = _split_limit_cell_from_pdf(
        page, groups, cas_y, limit_slot=1, limit_slot_count=2
    )
    assert stel == ["20 mg/m3"]
    assert twa == ["10 mg/m3"]


def test_mw_cluster_never_assigned_to_stel_or_twa():
    page = _limit_page(
        [
            _w(245, 140, 255, 152, "20"),
            _w(255, 140, 278, 152, "mg/m"),
            _w(278, 140, 283, 148, "3"),
            _w(312, 140, 322, 152, "10"),
            _w(322, 140, 345, 152, "mg/m"),
            _w(345, 140, 350, 148, "3"),
            _w(379, 140, 392, 152, "431"),
            _w(392, 140, 396, 152, "/"),
            _w(396, 140, 408, 152, "10"),
        ]
    )
    words = [w[:5] for w in page.get_text("words") if w[1] > 100]
    clusters = _cluster_words_by_x(words)
    stel = _pick_limit_cluster(
        clusters, limit_slot=0, limit_slot_count=2, anchors=[260.0, 325.0]
    )
    twa = _pick_limit_cluster(
        clusters, limit_slot=1, limit_slot_count=2, anchors=[260.0, 325.0]
    )
    stel_text = reconstruct_limit_expression_from_pdf_words(stel)
    twa_text = reconstruct_limit_expression_from_pdf_words(twa)
    assert stel_text == "20 mg/m3"
    assert twa_text == "10 mg/m3"
    assert "431" not in (stel_text or "")
    assert "431" not in (twa_text or "")
    mw_cluster = max(clusters, key=lambda cluster: min(w[0] for w in cluster))
    mw_text = reconstruct_limit_expression_from_pdf_words(mw_cluster)
    assert not _reconstructed_limit_has_unit(mw_text)


def test_missing_stel_does_not_shift_twa_left():
    page = _limit_page(
        [
            _w(312, 140, 322, 152, "10"),
            _w(322, 140, 345, 152, "mg/m"),
            _w(345, 140, 350, 148, "3"),
            _w(379, 140, 395, 152, "349"),
            _w(395, 140, 400, 152, "/"),
            _w(400, 140, 410, 152, "40"),
        ]
    )
    groups = [["100-00-0"]]
    cas_y = {"100-00-0": 145.0}
    stel_split = _split_limit_cell_from_pdf(
        page, groups, cas_y, limit_slot=0, limit_slot_count=2
    )
    twa_split = _split_limit_cell_from_pdf(
        page, groups, cas_y, limit_slot=1, limit_slot_count=2
    )
    assert stel_split is None or stel_split[0] == [None]
    assert twa_split is not None
    assert twa_split[0] == ["10 mg/m3"]


def test_missing_twa_does_not_shift_mw_into_twa():
    page = _limit_page(
        [
            _w(245, 140, 255, 152, "20"),
            _w(255, 140, 278, 152, "mg/m"),
            _w(278, 140, 283, 148, "3"),
            _w(379, 140, 395, 152, "252"),
            _w(395, 140, 400, 152, "/"),
            _w(400, 140, 410, 152, "07"),
        ]
    )
    groups = [["100-00-0"]]
    cas_y = {"100-00-0": 145.0}
    twa_split = _split_limit_cell_from_pdf(
        page, groups, cas_y, limit_slot=1, limit_slot_count=2
    )
    assert twa_split is None or twa_split[0] == [None]


def test_multi_cas_same_visual_row_keeps_one_stel_twa_pair():
    groups = group_cas_by_pdf_row(
        ["111-11-1", "222-22-2"],
        [
            CasGeometry("111-11-1", 140.0, 150.0),
            CasGeometry("222-22-2", 141.0, 151.0),
        ],
    )
    assert len(groups) == 1
    page = _limit_page(
        [
            _w(245, 140, 255, 152, "15"),
            _w(255, 140, 278, 152, "mg/m"),
            _w(278, 140, 283, 148, "3"),
            _w(312, 140, 322, 152, "5"),
            _w(322, 140, 345, 152, "mg/m"),
            _w(345, 140, 350, 148, "3"),
        ]
    )
    cas_y = {"111-11-1": 140.0, "222-22-2": 141.0}
    stel = _split_nonchemical_cell_by_geometry(
        type("C", (), {"text": r"\cdot/\pi mg/m^{3}", "bbox": None})(),
        groups,
        cas_y,
        page=page,
        limit_slot=0,
        limit_slot_count=2,
    )
    twa = _split_nonchemical_cell_by_geometry(
        type("C", (), {"text": r"1\cdot mg/m^{3}", "bbox": None})(),
        groups,
        cas_y,
        page=page,
        limit_slot=1,
        limit_slot_count=2,
    )
    assert stel == ["15 mg/m3"]
    assert twa == ["5 mg/m3"]


class _GeomCell:
    def __init__(self, text, bbox=None):
        self.text = text
        self.bbox = bbox
        self.row = 0
        self.source_reference = {}


def _xywh(x, y, w, h):
    return {"x": x, "y": y, "width": w, "height": h}


def test_twa_target_x_does_not_inherit_stel_when_slot_is_zero():
    page = _limit_page(
        [
            _w(245, 140, 255, 152, "6"),
            _w(255, 140, 278, 152, "ppm"),
            _w(312, 140, 322, 152, "2"),
            _w(322, 140, 345, 152, "ppm"),
        ]
    )
    groups = [["100-00-0"]]
    cas_y = {"100-00-0": 145.0}
    twa, _ = _split_limit_cell_from_pdf(
        page,
        groups,
        cas_y,
        limit_slot=0,
        limit_slot_count=1,
        target_x=325.0,
    )
    stel, _ = _split_limit_cell_from_pdf(
        page,
        groups,
        cas_y,
        limit_slot=0,
        limit_slot_count=1,
        target_x=260.0,
    )
    assert twa == ["2 ppm"]
    assert stel == ["6 ppm"]


def test_twa_bbox_selects_twa_cluster_even_if_limit_slot_is_zero():
    page = _limit_page(
        [
            _w(245, 140, 255, 152, "6"),
            _w(255, 140, 278, 152, "ppm"),
            _w(312, 140, 322, 152, "2"),
            _w(322, 140, 345, 152, "ppm"),
        ]
    )
    values = _split_nonchemical_cell_by_geometry(
        _GeomCell("6 ppm", _xywh(305, 138, 45, 16)),
        [["100-00-0"]],
        {"100-00-0": 145.0},
        page=page,
        limit_slot=1,
        limit_slot_count=2,
    )
    assert values == ["2 ppm"]


def test_mw_column_does_not_keep_stel_twa_limit_tokens():
    page = _limit_page(
        [
            _w(245, 140, 255, 152, "5"),
            _w(255, 140, 278, 152, "mg/m"),
            _w(278, 140, 283, 148, "3"),
            _w(312, 140, 322, 152, "2"),
            _w(322, 140, 345, 152, "ppm"),
            _w(379, 140, 392, 152, "85"),
            _w(392, 140, 396, 152, "/"),
            _w(396, 140, 408, 152, "10"),
        ]
    )
    values = _split_nonchemical_cell_by_geometry(
        _GeomCell("5 mg/m3 - 85 / 10", _xywh(370, 138, 50, 16)),
        [["100-00-0"]],
        {"100-00-0": 145.0},
        page=page,
        limit_slot=None,
        limit_slot_count=0,
    )
    text = values[0] or ""
    lowered = text.lower()
    assert "mg/m" not in lowered
    assert "ppm" not in lowered
    assert "85" in text


def test_fragmented_ppm_tokens_are_reconstructed():
    page = _limit_page(
        [
            _w(245, 140, 252, 152, "6"),
            _w(253, 140, 258, 152, "p"),
            _w(258, 140, 272, 152, "pm"),
            _w(312, 140, 322, 152, "2"),
            _w(323, 140, 328, 152, "p"),
            _w(328, 140, 342, 152, "pm"),
        ]
    )
    groups = [["100-00-0"]]
    cas_y = {"100-00-0": 145.0}
    stel, _ = _split_limit_cell_from_pdf(
        page, groups, cas_y, limit_slot=0, limit_slot_count=2
    )
    twa, _ = _split_limit_cell_from_pdf(
        page, groups, cas_y, limit_slot=1, limit_slot_count=2
    )
    assert stel == ["6 ppm"]
    assert twa == ["2 ppm"]


def test_empty_stel_column_does_not_shift_twa_onto_stel_cluster():
    page = _limit_page(
        [
            _w(245, 140, 255, 152, "6"),
            _w(255, 140, 278, 152, "ppm"),
            _w(312, 140, 322, 152, "2"),
            _w(322, 140, 345, 152, "ppm"),
            _w(430, 140, 500, 152, "100-00-0"),
        ]
    )
    row = [
        _GeomCell(""),
        _GeomCell("6 ppm", _xywh(305, 138, 45, 16)),
        _GeomCell("85 / 10", _xywh(370, 138, 50, 16)),
        _GeomCell("100-00-0"),
    ]
    rows, split_count = split_table_rows_by_cas_geometry(
        [row],
        [CasGeometry("100-00-0", 140.0, 152.0)],
        page=page,
    )
    assert split_count == 0
    stel = (rows[0][0].text or "").strip()
    twa = (rows[0][1].text or "").strip()
    mw = (rows[0][2].text or "").strip()
    assert stel == "6 ppm"
    assert twa == "2 ppm"
    assert "ppm" not in mw.lower()
    assert "mg/m" not in mw.lower()


def test_header_column_repair_assigns_stel_twa_after_distinct_columns():
    page = _limit_page(
        [
            _w(245, 140, 255, 152, "6"),
            _w(255, 140, 278, 152, "ppm"),
            _w(312, 140, 322, 152, "2"),
            _w(322, 140, 345, 152, "ppm"),
            _w(379, 140, 392, 152, "85"),
            _w(392, 140, 396, 152, "/"),
            _w(396, 140, 408, 152, "10"),
            _w(430, 140, 500, 152, "100-00-0"),
        ]
    )
    header = [
        _GeomCell("STEL/C"),
        _GeomCell("TWA"),
        _GeomCell("وزن ملکولی MW"),
        _GeomCell("chemical"),
    ]
    for index, cell in enumerate(header):
        cell.column = index
    body = [
        _GeomCell("6 ppm", _xywh(240, 138, 40, 16)),
        _GeomCell("6 ppm", _xywh(305, 138, 45, 16)),
        _GeomCell("6 ppm 85 / 10", _xywh(370, 138, 50, 16)),
        _GeomCell("100-00-0"),
    ]
    for index, cell in enumerate(body):
        cell.column = index
    from ingestion.merged_row_splitter import repair_oel_numeric_from_pdf_headers

    repaired = repair_oel_numeric_from_pdf_headers([header, body], page)
    assert repaired[1][0].text == "6 ppm"
    assert repaired[1][1].text == "2 ppm"
    mw = (repaired[1][2].text or "").lower()
    assert "ppm" not in mw
    assert "85" in mw


def test_page60_row116_wrapped_cas_stays_one_logical_row():
    """Page 60 row 116: wrapped CAS lines stay one table row."""

    chemical_text = (
        "Butene, all isomers\n"
        "[106-98-9]; [107-01-7]; [590-18-1]; [624-64-6]; [25167-67-3]\n"
        "Isobutene [115-11-7]"
    )
    chemical = _GeomCell(
        chemical_text,
        _xywh(410, 348, 150, 70),
    )
    chemical.cell_id = "cell_p60_r116_name"
    chemical.source_reference = {"cell_id": "cell_p60_r116_name"}

    stel = _GeomCell("250 ppm", _xywh(310, 356, 40, 20))
    twa = _GeomCell("56 / 11", _xywh(375, 356, 30, 20))
    mw = _GeomCell("-", _xywh(200, 356, 20, 20))
    row_number = _GeomCell("116", _xywh(590, 365, 20, 20))
    row = [stel, twa, mw, chemical, row_number]

    # PDF Y-gaps between wrapped CAS lines exceed y_threshold=8.
    geometry = [
        CasGeometry("106-98-9", 352.08, 362.04, 491.9, 533.7),
        CasGeometry("107-01-7", 363.36, 373.32, 420.5, 462.2),
        CasGeometry("590-18-1", 363.36, 373.32, 464.3, 505.8),
        CasGeometry("624-64-6", 363.36, 373.32, 508.0, 549.6),
        CasGeometry("25167-67-3", 374.52, 384.48, 420.5, 468.5),
        CasGeometry("115-11-7", 403.56, 413.52, 457.7, 496.7),
    ]

    rows, split_count = split_table_rows_by_cas_geometry(
        [row],
        geometry,
        y_threshold=8.0,
    )

    assert split_count == 0
    assert len(rows) == 1

    out = rows[0]
    name = out[3]
    blob = " ".join(str(getattr(cell, "text", "") or "") for cell in out)
    cas_values = [
        "106-98-9",
        "107-01-7",
        "590-18-1",
        "624-64-6",
        "25167-67-3",
        "115-11-7",
    ]
    for cas in cas_values:
        assert cas in blob
    assert "Isobutene" in (name.text or "")
    assert (out[0].text or "").strip() == "250 ppm"
    assert (out[1].text or "").strip() == "56 / 11"
    assert (out[4].text or "").strip() == "116"

    for cell in out:
        ref = getattr(cell, "source_reference", None) or {}
        assert ref.get("multi_cas_visual_row_split") is not True
        assert ref.get("physical_group_index") is None

    name_ref = name.source_reference or {}
    assert name_ref.get("cas_y_groups_collapsed") is True
    recorded = {
        item["cas"]: item
        for item in name_ref.get("cas_source_geometry") or []
    }
    for cas in cas_values:
        assert cas in recorded
        assert recorded[cas]["y"] is not None
        assert recorded[cas]["x0"] is not None
    assert getattr(name, "cell_id", None) == "cell_p60_r116_name"


def test_header_mapping_not_overwritten_by_recovery_column_name():
    from pipeline_contracts.header_reconstruction import reconstruct_header_structure

    rows = [
        [
            {"column": 0, "text": "مبنای تعیین حد مجاز مواجهه"},
            {"column": 1, "text": "نمادها"},
            {
                "column": 2,
                "text": "حد مجاز مواجهه شغلی TWA STEL/C",
                "source_reference": {"column_span": 2},
            },
            {"column": 3, "text": ""},
            {"column": 4, "text": "وزن ملکولی MW"},
            {"column": 5, "text": "نام علمی ماده شیمیایی"},
            {"column": 6, "text": "ردیف"},
        ],
        [
            {"column": 2, "text": "STEL/C"},
            {"column": 3, "text": "TWA"},
        ],
        [
            {
                "column": 2,
                "text": "20 mg/m3",
                "source_reference": {"column_name": "TWA"},
            },
            {
                "column": 3,
                "text": "10 mg/m3",
                "source_reference": {"column_name": "STEL"},
            },
            {
                "column": 4,
                "text": "252/07",
                "source_reference": {"column_name": "STEL"},
            },
            {"column": 5, "text": "Foo [100-00-0]"},
            {"column": 6, "text": "1"},
        ],
    ]
    structure = reconstruct_header_structure(rows, table_type="chemical_oel")
    assert structure.header_mapping[2] == "STEL"
    assert structure.header_mapping[3] == "TWA"
    assert structure.header_mapping[4] == "molecular_weight"


def test_gold_uses_header_column_identity_not_token_order():
    from goldset_generator.table_gold_generator import TableGoldGenerator

    table = {
        "table_id": "table_identity_01",
        "page_number": 1,
        "table_type": "chemical_oel",
        "rows": [
            [
                {"column": 0, "text": "مبنای تعیین حد مجاز مواجهه"},
                {"column": 1, "text": "نمادها"},
                {
                    "column": 2,
                    "text": "حد مجاز مواجهه شغلی TWA STEL/C",
                    "source_reference": {"column_span": 2},
                },
                {"column": 3, "text": ""},
                {"column": 4, "text": "وزن ملکولی"},
                {"column": 5, "text": "نام علمی ماده شیمیایی"},
                {"column": 6, "text": "ردیف"},
            ],
            [
                {"column": 2, "text": "STEL/C"},
                {"column": 3, "text": "TWA"},
            ],
            [
                {"cell_id": "s2", "column": 2, "text": "20 mg/m3"},
                {"cell_id": "t3", "column": 3, "text": "10 mg/m3"},
                {"cell_id": "m4", "column": 4, "text": "252/07"},
                {"cell_id": "n5", "column": 5, "text": "Foo [100-00-0]"},
                {"cell_id": "r6", "column": 6, "text": "1"},
            ],
        ],
    }
    gold = TableGoldGenerator().generate(table)
    row = gold["rows"][0]
    assert gold["header_mapping"]["2"] == "STEL"
    assert gold["header_mapping"]["3"] == "TWA"
    assert gold["header_mapping"]["4"] == "molecular_weight"
    assert row["STEL"]["value"] == "20"
    assert row["STEL"]["cell_id"] == "s2"
    assert row["TWA"]["value"] == "10"
    assert row["TWA"]["cell_id"] == "t3"
    assert row["molecular_weight"]["cell_id"] == "m4"


def test_header_mapping_precedes_positional_column_map():
    from goldset_generator.oel_row_parser import resolve_field_name

    mapping = {2: "TWA", 3: "STEL", 4: "molecular_weight"}
    assert resolve_field_name(2, "STEL", mapping) == "TWA"
    assert resolve_field_name(3, "TWA", mapping) == "STEL"
    assert resolve_field_name(4, "STEL", mapping) == "molecular_weight"
    assert resolve_field_name(2, "STEL", {}) == "STEL"
