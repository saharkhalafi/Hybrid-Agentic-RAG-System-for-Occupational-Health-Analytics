"""Tests for rule-based table classification."""

from database.models import TableType
from knowledge.table_classifier import classify_table_text


def test_chemical_oel_classification():
    text = "CAS 67-56-1 Methanol TWA 200 ppm STEL 250 ppm OEL"
    assert classify_table_text(text) == TableType.CHEMICAL_OEL


def test_vibration_classification():
    text = "Hand-arm vibration A(8) m/s2 VDV m/s^1.75 exposure limit"
    assert classify_table_text(text) == TableType.VIBRATION


def test_noise_classification():
    text = "LAeq 85 dBA dose 100% criterion level"
    assert classify_table_text(text) == TableType.NOISE


def test_unknown_classification():
    assert classify_table_text("general notes without domain markers") == TableType.UNKNOWN
