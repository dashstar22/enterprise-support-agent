"""Single-engine PDF and image OCR pipeline. / 单引擎 PDF 与图片文字识别流程。"""

from __future__ import annotations

from collections.abc import Sequence
from importlib import import_module
from io import BytesIO
from pathlib import Path
from typing import Any, Protocol

from app.ocr.models import (
    BoundingBox,
    OcrDocumentResult,
    OcrPageResult,
    OcrProcessingError,
    OcrToken,
)
from app.ocr.normalize import extract_equipment_fields

PDF_SUFFIX = ".pdf"
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg"}


class OcrEngine(Protocol):
    """One replaceable OCR engine, fixed to RapidOCR in production. / 可替换的单个 OCR 引擎，生产环境固定为 RapidOCR。"""

    def recognize_png(self, image_bytes: bytes, /) -> list[OcrToken]:
        """Return recognized tokens with source-image coordinates. / 返回带原图坐标的识别文字块。"""

        ...


class RapidOcrEngine:
    """RapidOCR implementation used by this project. / 本项目使用的 RapidOCR 实现。"""

    def __init__(self) -> None:
        try:
            rapidocr_module: Any = import_module("rapidocr_onnxruntime")
        except ImportError as error:
            raise OcrProcessingError("RapidOCR 依赖未安装") from error
        self._engine: Any = rapidocr_module.RapidOCR()

    def recognize_png(self, image_bytes: bytes, /) -> list[OcrToken]:
        try:
            image_module: Any = import_module("PIL.Image")
            numpy_module: Any = import_module("numpy")

            image = numpy_module.array(image_module.open(BytesIO(image_bytes)).convert("RGB"))
            result, _elapsed = self._engine(image)
            return _rapidocr_result_to_tokens(result)
        except OcrProcessingError:
            raise
        except Exception as error:
            raise OcrProcessingError("RapidOCR 无法识别该页") from error


class OcrPipeline:
    """Extract existing PDF text first and OCR only scanned pages. / 优先提取 PDF 文字层，仅对扫描页执行 OCR。"""

    def __init__(self, engine: OcrEngine, *, confirmation_threshold: float = 0.90) -> None:
        if not 0.0 < confirmation_threshold <= 1.0:
            raise ValueError("OCR 确认阈值必须在 0 到 1 之间")
        self._engine = engine
        self._confirmation_threshold = confirmation_threshold

    def extract(self, path: Path) -> OcrDocumentResult:
        """Extract one PDF or image without converting OCR failures into no-evidence. / 提取一份 PDF 或图片，不把 OCR 失败改写成无证据。"""

        if not path.is_file():
            raise OcrProcessingError("OCR 输入文件不存在")
        suffix = path.suffix.lower()
        if suffix == PDF_SUFFIX:
            pages = self._extract_pdf(path)
        elif suffix in IMAGE_SUFFIXES:
            pages = [self._extract_image_page(path)]
        else:
            raise OcrProcessingError("OCR 仅接受 PDF、PNG、JPG 或 JPEG")
        return OcrDocumentResult(
            source_name=path.name,
            pages=pages,
            requires_confirmation=any(_page_requires_confirmation(page) for page in pages),
        )

    def _extract_pdf(self, path: Path) -> list[OcrPageResult]:
        try:
            pymupdf_module: Any = import_module("pymupdf")

            document: Any = pymupdf_module.open(path)
        except Exception as error:
            raise OcrProcessingError("PDF 无法打开") from error
        try:
            results: list[OcrPageResult] = []
            for index, page in enumerate(document, start=1):
                text = page.get_text("text").strip()
                if text:
                    results.append(self._text_page(path.name, index, text))
                    continue
                image_bytes = page.get_pixmap(
                    matrix=pymupdf_module.Matrix(2, 2), alpha=False
                ).tobytes("png")
                results.append(self._ocr_page(path.name, index, image_bytes))
            if not results:
                raise OcrProcessingError("PDF 不包含可提取页面")
            return results
        finally:
            document.close()

    def _extract_image_page(self, path: Path) -> OcrPageResult:
        try:
            return self._ocr_page(path.name, 1, path.read_bytes())
        except OSError as error:
            raise OcrProcessingError("图片无法读取") from error

    def _text_page(self, source_name: str, page: int, text: str) -> OcrPageResult:
        return OcrPageResult(
            source_name=source_name,
            page=page,
            parser="text",
            text=text,
            extracted_fields=extract_equipment_fields(
                text, [], confirmation_threshold=self._confirmation_threshold
            ),
        )

    def _ocr_page(self, source_name: str, page: int, image_bytes: bytes) -> OcrPageResult:
        tokens = self._engine.recognize_png(image_bytes)
        if not tokens:
            raise OcrProcessingError("OCR 未识别到任何文字")
        text = "\n".join(token.text for token in tokens)
        confidence = sum(token.confidence for token in tokens) / len(tokens)
        return OcrPageResult(
            source_name=source_name,
            page=page,
            parser="ocr",
            text=text,
            tokens=tokens,
            ocr_confidence=confidence,
            extracted_fields=extract_equipment_fields(
                text, tokens, confirmation_threshold=self._confirmation_threshold
            ),
        )


def _rapidocr_result_to_tokens(result: object) -> list[OcrToken]:
    """Normalize RapidOCR output while retaining every rectangle. / 规范化 RapidOCR 返回，并保留每个矩形。"""

    if not isinstance(result, Sequence) or not result:
        raise OcrProcessingError("RapidOCR 返回为空或结构无法确认")
    tokens: list[OcrToken] = []
    for item in result:
        if not isinstance(item, Sequence) or len(item) != 3:
            raise OcrProcessingError("RapidOCR 返回文字结构无法确认")
        polygon, raw_text, raw_confidence = item
        if not isinstance(polygon, Sequence) or len(polygon) != 4:
            raise OcrProcessingError("RapidOCR 返回坐标无法确认")
        points = _points(polygon)
        if not isinstance(raw_text, str) or not isinstance(raw_confidence, (float, int)):
            raise OcrProcessingError("RapidOCR 返回字段类型无法确认")
        tokens.append(
            OcrToken(
                text=raw_text,
                bounding_box=BoundingBox(
                    left=min(point[0] for point in points),
                    top=min(point[1] for point in points),
                    right=max(point[0] for point in points),
                    bottom=max(point[1] for point in points),
                ),
                confidence=float(raw_confidence),
            )
        )
    return tokens


def _points(polygon: object) -> list[tuple[float, float]]:
    if not isinstance(polygon, Sequence):
        raise OcrProcessingError("RapidOCR 坐标不是列表")
    points: list[tuple[float, float]] = []
    for point in polygon:
        if not isinstance(point, Sequence) or len(point) != 2:
            raise OcrProcessingError("RapidOCR 坐标点无法确认")
        x, y = point
        if not isinstance(x, (float, int)) or not isinstance(y, (float, int)):
            raise OcrProcessingError("RapidOCR 坐标类型无法确认")
        points.append((float(x), float(y)))
    return points


def _page_requires_confirmation(page: OcrPageResult) -> bool:
    """Promote any uncertain identity field to a document-level confirmation gate. / 将任一不确定身份字段提升为文档级确认门禁。"""

    fields = page.extracted_fields
    return any(
        field is not None and field.requires_confirmation
        for field in (fields.device_model, fields.fault_code, fields.firmware_version)
    )
