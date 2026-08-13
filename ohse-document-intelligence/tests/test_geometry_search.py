"""Tests for geometry search query variants."""

from document_ai.geometry_search import search_query_variants


def test_search_query_variants_adds_space_before_ppm():
    variants = search_query_variants("2ppm")
    assert "2 ppm" in variants


def test_search_query_variants_preserves_original():
    variants = search_query_variants("1 ppm")
    assert "1 ppm" in variants
