"""C4 OCR pipeline tests. / C4 文字识别流程测试。"""

from __future__ import annotations

from importlib import import_module
from pathlib import Path
from typing import Any

import pytest

from app.ocr.models import BoundingBox, OcrProcessingError, OcrToken
from app.ocr.pipeline import OcrPipeline, RapidOcrEngine

pymupdf: Any = import_module("pymupdf")
OCR_FIXTURE = (
    Path(__file__).resolve().parents[1] / "data" / "fixtures" / "e200-synthetic-control-panel.png"
)


class RecordedOcrEngine:
    """Deterministic engine to test parser control flow. / 用于测试解析分流的确定性引擎。"""

    def __init__(self, tokens: list[OcrToken]) -> None:
        self.tokens = tokens
        self.calls: list[bytes] = []

    def recognize_png(self, image_bytes: bytes, /) -> list[OcrToken]:
        self.calls.append(image_bytes)
        return list(self.tokens)


class FailingOcrEngine:
    """Expected OCR engine failure. / 预期的 OCR 引擎失败。"""

    def recognize_png(self, image_bytes: bytes, /) -> list[OcrToken]:
        del image_bytes
        raise OcrProcessingError("simulated engine failure")


def token(text: str, confidence: float = 0.96) -> OcrToken:
    """Build one OCR token with stable geometry. / 构造一条带稳定坐标的 OCR 文字块。"""

    return OcrToken(
        text=text,
        bounding_box=BoundingBox(left=10, top=20, right=100, bottom=60),
        confidence=confidence,
    )


def create_hybrid_pdf(path: Path) -> None:
    """Build one text page and one image-only page. / 生成一页文本层和一页纯图片的 PDF。"""

    document = pymupdf.open()
    text_page = document.new_page()
    text_page.insert_text((72, 72), "MODEL: E-200\\nFAULT: E01\\nFIRMWARE: 3.1.4")
    image_page = document.new_page()
    image_page.insert_image(image_page.rect, filename=str(OCR_FIXTURE))
    document.save(path)
    document.close()


def test_pdf_uses_text_layer_then_ocr_only_for_scanned_page(tmp_path: Path) -> None:
    """A text PDF page never calls OCR, while an image page preserves OCR metadata. / 文本页不调用 OCR，图片页保留 OCR 元数据。"""

    pdf_path = tmp_path / "hybrid.pdf"
    create_hybrid_pdf(pdf_path)
    engine = RecordedOcrEngine(
        [token("MODEL: E-200"), token("FAULT: E01"), token("FIRMWARE: 3.1.4")]
    )

    result = OcrPipeline(engine).extract(pdf_path)

    assert [page.parser for page in result.pages] == ["text", "ocr"]
    assert len(engine.calls) == 1
    assert result.pages[0].tokens == []
    assert result.pages[0].ocr_confidence is None
    scanned_page = result.pages[1]
    assert scanned_page.source_name == "hybrid.pdf"
    assert scanned_page.page == 2
    assert scanned_page.ocr_confidence == pytest.approx(0.96)
    assert scanned_page.tokens[0].bounding_box.left == 10
    assert scanned_page.extracted_fields.device_model is not None
    assert scanned_page.extracted_fields.device_model.value == "E-200"
    assert scanned_page.extracted_fields.fault_code is not None
    assert scanned_page.extracted_fields.fault_code.value == "E01"
    assert scanned_page.extracted_fields.firmware_version is not None
    assert scanned_page.extracted_fields.firmware_version.value == "3.1.4"


def test_low_confidence_field_requires_confirmation_without_losing_source(tmp_path: Path) -> None:
    """Low-confidence fields stay visible but are never marked reliable. / 低置信度字段仍可见，但绝不标记为可靠。"""

    image_path = tmp_path / "panel.png"
    image_path.write_bytes(b"not-a-real-image")
    engine = RecordedOcrEngine([token("MODEL: E-200", confidence=0.52), token("FAULT: E01")])

    result = OcrPipeline(engine, confirmation_threshold=0.90).extract(image_path)
    page = result.pages[0]

    assert page.parser == "ocr"
    assert page.source_name == "panel.png"
    assert page.extracted_fields.device_model is not None
    assert page.extracted_fields.device_model.requires_confirmation is True
    assert page.extracted_fields.fault_code is not None
    assert page.extracted_fields.fault_code.requires_confirmation is False
    assert result.requires_confirmation is True


def test_ocr_failure_is_not_reported_as_no_evidence(tmp_path: Path) -> None:
    """Engine failure remains OCR_FAILED instead of an empty result. / 引擎失败保留为 OCR_FAILED，不变成空结果。"""

    image_path = tmp_path / "failed.png"
    image_path.write_bytes(b"unreadable")

    with pytest.raises(OcrProcessingError) as exc_info:
        OcrPipeline(FailingOcrEngine()).extract(image_path)

    assert exc_info.value.code == "OCR_FAILED"
    assert "no_evidence" not in str(exc_info.value)


def test_rapidocr_extracts_the_fixed_synthetic_panel() -> None:
    """The selected real engine extracts all three fixed panel fields. / 选定的真实引擎识别固定面板中的三个字段。"""

    page = OcrPipeline(RapidOcrEngine()).extract(OCR_FIXTURE).pages[0]

    assert page.parser == "ocr"
    assert page.page == 1
    assert page.tokens
    assert page.extracted_fields.device_model is not None
    assert page.extracted_fields.device_model.value == "E-200"
    assert page.extracted_fields.fault_code is not None
    assert page.extracted_fields.fault_code.value == "E01"
    assert page.extracted_fields.firmware_version is not None
    assert page.extracted_fields.firmware_version.value == "3.1.4"
