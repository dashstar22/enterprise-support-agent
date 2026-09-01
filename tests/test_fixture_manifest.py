"""Integrity checks for the fixed C3-T01 retrieval fixtures."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from scripts.generate_fixture_pdf import build_pdf
from scripts.generate_ocr_fixture import build_image

FIXTURE_DIRECTORY = Path(__file__).resolve().parents[1] / "data" / "fixtures"
MANIFEST_PATH = FIXTURE_DIRECTORY / "MANIFEST.json"
EXPECTED_FORMATS = {"pdf", "markdown", "text", "png"}


def load_manifest() -> dict[str, Any]:
    raw_manifest: object = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    assert isinstance(raw_manifest, dict)
    return raw_manifest


def test_fixture_manifest_covers_one_pdf_markdown_text_and_ocr_image_sample() -> None:
    manifest = load_manifest()
    fixtures = manifest["fixtures"]

    assert manifest["manifest_version"] == 1
    assert isinstance(fixtures, list)
    assert {fixture["format"] for fixture in fixtures} == EXPECTED_FORMATS
    assert all(fixture["origin"] == "project-authored synthetic material" for fixture in fixtures)
    assert all(fixture["distribution"] == "public-shareable" for fixture in fixtures)


def test_fixture_hashes_match_the_recorded_content() -> None:
    manifest = load_manifest()
    fixtures = manifest["fixtures"]
    assert isinstance(fixtures, list)

    for fixture in fixtures:
        path = fixture["path"]
        expected_hash = fixture["sha256"]
        assert isinstance(path, str)
        assert isinstance(expected_hash, str)
        assert "/" not in path and "\\" not in path

        actual_hash = hashlib.sha256((FIXTURE_DIRECTORY / path).read_bytes()).hexdigest()
        assert actual_hash == expected_hash


def test_pdf_fixture_has_a_complete_pdf_envelope_and_expected_content() -> None:
    pdf_bytes = (FIXTURE_DIRECTORY / "e200-synthetic-safety-notice.pdf").read_bytes()

    assert pdf_bytes == build_pdf()

    assert pdf_bytes.startswith(b"%PDF-1.4")
    assert pdf_bytes.rstrip().endswith(b"%%EOF")
    assert b"E-200 Synthetic Safety Notice" in pdf_bytes
    assert b"Fault code E01" in pdf_bytes


def test_ocr_image_fixture_matches_its_deterministic_generator() -> None:
    """The committed OCR sample must be rebuildable without a system font. / 已提交 OCR 样本必须能在不依赖系统字体时重建。"""

    png_bytes = (FIXTURE_DIRECTORY / "e200-synthetic-control-panel.png").read_bytes()

    assert png_bytes == build_image()
