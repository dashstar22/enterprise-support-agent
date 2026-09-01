"""Validated OCR output models. / 经过校验的文字识别输出模型。"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.api.schemas import ShortText, SourceName


class OcrSchema(BaseModel):
    """Reject unknown OCR data and keep validation failures compact. / 拒绝未知 OCR 数据并精简校验失败。"""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class BoundingBox(OcrSchema):
    """One token rectangle in source-image pixel coordinates. / 一个文字块在原图像像素坐标中的矩形。"""

    left: float = Field(ge=0)
    top: float = Field(ge=0)
    right: float = Field(ge=0)
    bottom: float = Field(ge=0)

    @model_validator(mode="after")
    def keep_corners_ordered(self) -> "BoundingBox":
        if self.right < self.left or self.bottom < self.top:
            raise ValueError("坐标右下角不能位于左上角之外")
        return self


class OcrToken(OcrSchema):
    """Recognized text, position, and engine confidence. / 识别出的文字、位置与引擎置信度。"""

    text: ShortText
    bounding_box: BoundingBox
    confidence: float = Field(ge=0.0, le=1.0)


class ExtractedField(OcrSchema):
    """A cleaned field with the confidence used to decide confirmation. / 清洗后的字段与判定确认所用置信度。"""

    value: ShortText
    confidence: float = Field(ge=0.0, le=1.0)
    requires_confirmation: bool


class ExtractedEquipmentFields(OcrSchema):
    """Identity fields found in one document page. / 从一页资料中找到的设备身份字段。"""

    device_model: ExtractedField | None = None
    fault_code: ExtractedField | None = None
    firmware_version: ExtractedField | None = None


class OcrPageResult(OcrSchema):
    """One page result preserving its parser path and source. / 保留解析路径与来源的一页结果。"""

    source_name: SourceName
    page: int = Field(ge=1)
    parser: Literal["text", "ocr"]
    text: str = Field(min_length=1, max_length=20000)
    tokens: list[OcrToken] = Field(default_factory=list, max_length=5000)
    ocr_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    extracted_fields: ExtractedEquipmentFields

    @model_validator(mode="after")
    def keep_parser_metadata_consistent(self) -> "OcrPageResult":
        if self.parser == "text" and (self.tokens or self.ocr_confidence is not None):
            raise ValueError("文本层提取不能伪装成 OCR 结果")
        if self.parser == "ocr" and (not self.tokens or self.ocr_confidence is None):
            raise ValueError("OCR 页必须保留文字坐标和置信度")
        return self


class OcrDocumentResult(OcrSchema):
    """All extracted pages from one original file. / 一个原始文件的全部提取页。"""

    source_name: SourceName
    pages: list[OcrPageResult] = Field(min_length=1, max_length=1000)
    requires_confirmation: bool


class OcrProcessingError(Exception):
    """Expected OCR failure that must not become no-evidence. / 不能被误报成无证据的预期 OCR 失败。"""

    code = "OCR_FAILED"
