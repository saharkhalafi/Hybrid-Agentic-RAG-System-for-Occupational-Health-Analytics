from ingestion.merged_row_splitter import (
    CasGeometry,
    group_cas_by_pdf_row,
    reconstruct_limit_expression_from_pdf_words,
    _numeric_from_fragments,
    _pack_source_values,
    _split_nonchemical_cell_by_geometry,
    _words_to_text,
)


class _FakePage:
    def __init__(self, words):
        self._words = words

    def get_text(self, mode):
        assert mode == "words"
        return self._words


def _word(x0, y0, x1, y1, text):
    return (x0, y0, x1, y1, text, 0, 0, 0)


def test_multiple_cas_same_pdf_row_are_kept_together():

    geometry = [
        CasGeometry("30560-19-1", 132.0, 142.0),
        CasGeometry("115096-11-2", 133.0, 143.0),
    ]

    result = group_cas_by_pdf_row(
        [
            "30560-19-1",
            "115096-11-2",
        ],
        geometry,
    )

    assert result == [
        [
            "30560-19-1",
            "115096-11-2",
        ]
    ]


def test_different_pdf_rows_are_split():

    geometry = [
        CasGeometry("309-00-2", 132.91, 142.87),
        CasGeometry("107-18-6", 163.39, 173.35),
    ]

    result = group_cas_by_pdf_row(
        [
            "309-00-2",
            "107-18-6",
        ],
        geometry,
    )

    assert result == [
        ["309-00-2"],
        ["107-18-6"],
    ]


def test_three_physical_rows_are_split():

    geometry = [
        CasGeometry("7664-41-7", 204.07, 214.03),
        CasGeometry("12125-02-9", 234.57, 244.53),
        CasGeometry("7789-09-5", 264.57, 274.53),
    ]

    result = group_cas_by_pdf_row(
        [
            "7664-41-7",
            "12125-02-9",
            "7789-09-5",
        ],
        geometry,
    )

    assert result == [
        ["7664-41-7"],
        ["12125-02-9"],
        ["7789-09-5"],
    ]


def test_pack_mg_m_plus_exponent_is_one_unit():
    packed = _pack_source_values("20 mg/m 3 10 mg/m 3")
    assert packed == ["20 mg/m3", "10 mg/m3"]


def test_numeric_from_fragmented_slash_decimals():
    assert _numeric_from_fragments(["/0", "08"]) == "0.08"
    assert _numeric_from_fragments(["/0", "02"]) == "0.02"


def test_reconstruct_mg_m_plus_exponent_from_pdf_words():
    words = [
        (245.0, 223.0, 254.0, 239.0, "20"),
        (255.0, 223.0, 279.0, 239.0, "mg/m"),
        (279.0, 223.0, 282.0, 239.0, "3"),
    ]
    assert reconstruct_limit_expression_from_pdf_words(words) == "20 mg/m3"


def test_reconstruct_fragmented_slash_decimal_with_inhalable_suffix():
    stel = [
        (247.0, 251.0, 255.0, 265.0, "mg/m"),
        (257.0, 251.0, 264.0, 265.0, "/0"),
        (264.0, 251.0, 270.0, 265.0, "08"),
        (270.0, 251.0, 280.0, 275.0, "3(I)"),
    ]
    twa = [
        (304.0, 251.0, 311.0, 265.0, "/0"),
        (311.0, 251.0, 322.0, 265.0, "02"),
        (323.0, 251.0, 345.0, 265.0, "mg/m"),
        (347.0, 251.0, 350.0, 265.0, "3"),
        (350.0, 251.0, 357.0, 260.0, "(I)"),
    ]
    assert reconstruct_limit_expression_from_pdf_words(stel) == "0.08 mg/m3(I)"
    assert reconstruct_limit_expression_from_pdf_words(twa) == "0.02 mg/m3(I)"


def test_words_to_text_merges_mg_m_exponent():
    text = _words_to_text(
        [
            (230.0, 255.0, "mg/m"),
            (230.0, 245.0, "20"),
            (230.0, 279.0, "3"),
        ]
    )
    assert text == "20 mg/m3"


def _chloride_dichromate_page():
    return _FakePage(
        [
            _word(245, 223, 254, 239, "20"),
            _word(255, 223, 279, 239, "mg/m"),
            _word(279, 223, 282, 239, "3"),
            _word(312, 226, 320, 239, "10"),
            _word(322, 226, 345, 239, "mg/m"),
            _word(345, 226, 349, 239, "3"),
            _word(247, 251, 255, 265, "mg/m"),
            _word(257, 251, 264, 265, "/0"),
            _word(264, 251, 270, 265, "08"),
            _word(270, 251, 280, 275, "3(I)"),
            _word(304, 251, 311, 265, "/0"),
            _word(311, 251, 322, 265, "02"),
            _word(323, 251, 345, 265, "mg/m"),
            _word(347, 251, 350, 265, "3"),
            _word(381, 223, 389, 239, "53"),
            _word(389, 223, 392, 239, "/"),
            _word(392, 223, 400, 239, "50"),
            _word(379, 251, 395, 265, "252"),
        ]
    )


def _limit_cell(text):
    cell = type("Cell", (), {})()
    cell.text = text
    cell.bbox = None
    return cell


def test_pdf_primary_stel_twa_ignores_da_cdot_artifacts():
    groups = [["12125-02-9"], ["7789-09-5"]]
    cas_y = {"12125-02-9": 234.57, "7789-09-5": 264.57}
    page = _chloride_dichromate_page()

    stel = _split_nonchemical_cell_by_geometry(
        _limit_cell(r"r\cdot mg/m^{3} \cdot/\cdot mg/m^{3(l)}"),
        groups,
        cas_y,
        page=page,
        limit_slot=0,
        limit_slot_count=2,
    )
    twa = _split_nonchemical_cell_by_geometry(
        _limit_cell(r"1\cdot mg/m^{3} \cdot/\cdot mg/m^{3(I)}"),
        groups,
        cas_y,
        page=page,
        limit_slot=1,
        limit_slot_count=2,
    )

    assert stel == ["20 mg/m3", "0.08 mg/m3(I)"]
    assert twa == ["10 mg/m3", "0.02 mg/m3"]


def test_insufficient_pdf_evidence_leaves_corrupted_da_unresolved():
    groups = [["12125-02-9"], ["7789-09-5"]]
    cas_y = {"12125-02-9": 234.57, "7789-09-5": 264.57}
    page = _FakePage(
        [
            _word(255, 223, 279, 239, "mg/m"),
            _word(279, 223, 282, 239, "3"),
        ]
    )

    values = _split_nonchemical_cell_by_geometry(
        _limit_cell(r"r\cdot mg/m^{3} \cdot/\cdot"),
        groups,
        cas_y,
        page=page,
        limit_slot=0,
        limit_slot_count=2,
    )

    assert values == [None, None]


def test_exclusive_y_bands_do_not_merge_next_row_slash_decimal():
    groups = [["7664-41-7"], ["12125-02-9"], ["7789-09-5"]]
    cas_y = {
        "7664-41-7": 204.07,
        "12125-02-9": 234.57,
        "7789-09-5": 264.57,
    }
    page = _FakePage(
        [
            _word(250, 196, 259, 208, "35"),
            _word(261, 194, 279, 205, "ppm"),
            _word(316, 196, 326, 208, "25"),
            _word(328, 194, 346, 205, "ppm"),
            _word(245, 226, 253, 239, "20"),
            _word(255, 223, 279, 234, "mg/m"),
            _word(279, 223, 282, 230, "3"),
            _word(312, 226, 320, 239, "10"),
            _word(322, 223, 345, 234, "mg/m"),
            _word(345, 223, 348, 230, "3"),
            _word(257, 251, 264, 263, "/0"),
            _word(264, 251, 272, 263, "08"),
            _word(247, 263, 270, 274, "mg/m"),
            _word(270, 263, 280, 270, "3(I)"),
            _word(304, 256, 311, 269, "/0"),
            _word(311, 256, 319, 269, "02"),
            _word(323, 253, 347, 264, "mg/m"),
            _word(347, 253, 350, 260, "3"),
            _word(350, 253, 356, 260, "(I)"),
        ]
    )

    stel = _split_nonchemical_cell_by_geometry(
        _limit_cell(r"r\cdot mg/m^{3}"),
        groups,
        cas_y,
        page=page,
        limit_slot=0,
        limit_slot_count=2,
    )
    twa = _split_nonchemical_cell_by_geometry(
        _limit_cell(r"1\cdot mg/m^{3}"),
        groups,
        cas_y,
        page=page,
        limit_slot=1,
        limit_slot_count=2,
    )

    assert stel == ["35 ppm", "20 mg/m3", "0.08 mg/m3(I)"]
    assert twa == ["25 ppm", "10 mg/m3", "0.02 mg/m3(I)"]
    assert "0.20" not in "".join(stel)


def test_complete_twa_cluster_is_not_glued_to_molecular_weight():
    groups = [["7789-09-5"]]
    cas_y = {"7789-09-5": 264.57}
    page = _FakePage(
        [
            _word(257, 251, 264, 263, "/0"),
            _word(264, 251, 272, 263, "08"),
            _word(247, 263, 270, 274, "mg/m"),
            _word(270, 263, 280, 270, "3(I)"),
            _word(304, 256, 311, 269, "/0"),
            _word(311, 256, 319, 269, "02"),
            _word(323, 253, 347, 264, "mg/m"),
            _word(347, 253, 350, 260, "3"),
            _word(350, 253, 356, 260, "(I)"),
            _word(379, 256, 391, 268, "252"),
            _word(391, 256, 394, 268, "/"),
            _word(394, 256, 402, 268, "07"),
        ]
    )
    stel = _split_nonchemical_cell_by_geometry(
        _limit_cell(r"r\cdot mg/m^{3}"),
        groups,
        cas_y,
        page=page,
        limit_slot=0,
        limit_slot_count=2,
    )
    twa = _split_nonchemical_cell_by_geometry(
        _limit_cell(r"\cdot/\cdot mg/m^{3(I)}"),
        groups,
        cas_y,
        page=page,
        limit_slot=1,
        limit_slot_count=2,
    )
    assert stel == ["0.08 mg/m3(I)"]
    assert twa == ["0.02 mg/m3(I)"]


def test_legitimate_multi_cas_row_keeps_one_pdf_limit():
    groups = group_cas_by_pdf_row(
        ["30560-19-1", "115096-11-2"],
        [
            CasGeometry("30560-19-1", 137.0, 147.0),
            CasGeometry("115096-11-2", 138.0, 148.0),
        ],
    )
    assert groups == [["30560-19-1", "115096-11-2"]]

    cas_y = {"30560-19-1": 137.0, "115096-11-2": 138.0}
    page = _FakePage(
        [
            _word(245, 132, 254, 145, "15"),
            _word(255, 132, 279, 145, "mg/m"),
            _word(279, 132, 282, 145, "3"),
            _word(312, 132, 320, 145, "10"),
            _word(322, 132, 345, 145, "mg/m"),
            _word(345, 132, 349, 145, "3"),
        ]
    )

    stel = _split_nonchemical_cell_by_geometry(
        _limit_cell(r"\cdot/\pi mg/m^{3}"),
        groups,
        cas_y,
        page=page,
        limit_slot=0,
        limit_slot_count=2,
    )
    twa = _split_nonchemical_cell_by_geometry(
        _limit_cell(r"\cdot/\cdot mg/m^{3}"),
        groups,
        cas_y,
        page=page,
        limit_slot=1,
        limit_slot_count=2,
    )

    assert stel == ["15 mg/m3"]
    assert twa == ["10 mg/m3"]