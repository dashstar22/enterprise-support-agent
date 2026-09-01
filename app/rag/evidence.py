"""Map raw RAGFlow candidates to current, registered evidence. / 将原始 RAGFlow 候选映射为当前已登记证据。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Literal, cast

from app.agent.state import EvidenceItem
from app.api.schemas import SupportAnswer
from app.rag.ragflow_client import RAGFlowRetrievedChunk


class CitationBindingError(Exception):
    """Generated steps do not cite the accepted current evidence. / 生成步骤未引用已接受的当前证据。"""


@dataclass(frozen=True)
class RegisteredSource:
    """One fixed source whose hash and stable locator are known. / 已知哈希和稳定定位的一份固定来源。"""

    path: Path
    content_hash: str
    stable_location: str
    parser: Literal["text", "ocr"]


class EvidenceRegistry:
    """Fail closed unless a candidate matches current registered source data. / 候选不能匹配当前登记来源时拒绝通过。"""

    def __init__(self, sources: dict[str, RegisteredSource]) -> None:
        self._sources = sources

    @classmethod
    def from_manifest(cls, manifest_path: Path) -> EvidenceRegistry:
        fixture_directory = manifest_path.parent.resolve()
        raw_manifest: Any = json.loads(manifest_path.read_text(encoding="utf-8"))
        fixtures = raw_manifest.get("fixtures") if isinstance(raw_manifest, dict) else None
        if not isinstance(fixtures, list):
            raise ValueError("固定样本清单无效")
        sources: dict[str, RegisteredSource] = {}
        for fixture in fixtures:
            if not isinstance(fixture, dict):
                raise ValueError("固定样本清单条目无效")
            path_value = fixture.get("path")
            content_hash = fixture.get("sha256")
            stable_location = fixture.get("stable_location")
            parser = fixture.get("parser")
            if not all(
                isinstance(value, str)
                for value in (path_value, content_hash, stable_location, parser)
            ):
                raise ValueError("固定样本清单缺少证据字段")
            assert isinstance(path_value, str)
            assert isinstance(content_hash, str)
            assert isinstance(stable_location, str)
            assert isinstance(parser, str)
            source_path = (fixture_directory / path_value).resolve()
            if (
                source_path.parent != fixture_directory
                or not source_path.is_file()
                or parser not in {"text", "ocr"}
            ):
                raise ValueError("固定样本来源无效")
            sources[path_value] = RegisteredSource(
                source_path, content_hash, stable_location, cast(Literal["text", "ocr"], parser)
            )
        return cls(sources)

    def to_evidence(self, candidate: RAGFlowRetrievedChunk) -> EvidenceItem:
        source = self._sources.get(candidate.document_name)
        if source is None:
            raise ValueError("候选来源未登记")
        source_text = _read_source_text(source.path)
        if not _retrieved_text_matches(candidate.content, source_text, source.path.suffix.lower()):
            raise ValueError("候选正文不在当前来源中")
        page, section = _locator(source.stable_location)
        return EvidenceItem(
            document_id=candidate.document_id,
            source_name=candidate.document_name,
            page=page,
            section=section,
            text=candidate.content,
            content_hash=source.content_hash,
            parser=source.parser,
        )

    def is_current(self, evidence: EvidenceItem) -> bool:
        """Confirm that an evidence item still matches its registered source. / 确认一条证据仍匹配已登记来源。"""
        source = self._sources.get(evidence.source_name)
        if source is None:
            return False
        page, section = _locator(source.stable_location)
        return (
            evidence.content_hash == source.content_hash
            and evidence.parser == source.parser
            and evidence.page == page
            and evidence.section == section
            and _retrieved_text_matches(
                evidence.text,
                _read_source_text(source.path),
                source.path.suffix.lower(),
            )
        )


class EvidenceGate:
    """Apply the minimum current-evidence threshold before answer generation. / 在回答生成前执行当前证据最低门槛。"""

    def __init__(self, registry: EvidenceRegistry, *, minimum_items: int = 1) -> None:
        if minimum_items < 1:
            raise ValueError("最低证据数量必须至少为 1")
        self._registry = registry
        self._minimum_items = minimum_items

    def accepts(self, evidence: list[EvidenceItem]) -> bool:
        """Accept only unique, current evidence meeting the configured minimum. / 仅接受满足数量门槛的去重当前证据。"""
        unique_keys: set[tuple[str, str, int | None, str | None]] = set()
        for item in evidence:
            key = (item.source_name, item.content_hash, item.page, item.section)
            if key in unique_keys or not self._registry.is_current(item):
                continue
            unique_keys.add(key)
        return len(unique_keys) >= self._minimum_items


class CitationBinder:
    """Require every generated step to cite current accepted evidence. / 要求每个生成步骤引用当前已接受证据。"""

    def __init__(self, gate: EvidenceGate) -> None:
        self._gate = gate

    def supports(self, answer: SupportAnswer, evidence: list[EvidenceItem]) -> bool:
        if not self._gate.accepts(evidence):
            return False
        citations = {(item.source_name, item.page, item.section) for item in evidence}
        return all(
            (step.citation.source_name, step.citation.page, step.citation.section) in citations
            for step in answer.steps
        )


def _locator(stable_location: str) -> tuple[int | None, str | None]:
    if stable_location.startswith("page "):
        return int(stable_location.removeprefix("page ")), None
    return None, stable_location


def _normalize_retrieved_text(value: str) -> str:
    """Ignore provider changes to whitespace while retaining all visible text. / 忽略服务端空白变化，但保留所有可见文字。"""

    return re.sub(r"\s+", " ", value).strip()


def _read_source_text(path: Path) -> str:
    """Read text fixtures and simple PDF text operators for provider comparison. / 读取文本样本和简单 PDF 文本指令，供服务端结果比对。"""

    raw = path.read_bytes()
    if path.suffix.lower() != ".pdf":
        return raw.decode("latin-1")
    # The project fixture uses literal PDF text operators; keep the fallback for other PDFs.
    # / 项目样本使用字面量 PDF 文本指令; 其他 PDF 仍保留原始字节回退路径。
    parts = re.findall(rb"\(([^()]*)\)\s*Tj", raw)
    if parts:
        return "\n".join(part.decode("latin-1") for part in parts)
    return raw.decode("latin-1")


def _retrieved_text_matches(candidate: str, source: str, suffix: str) -> bool:
    """Match extracted text while allowing bounded PDF extraction noise. / 比对抽取文本，并仅对 PDF 允许有限抽取误差。"""

    normalized_candidate = _normalize_retrieved_text(candidate)
    normalized_source = _normalize_retrieved_text(source)
    if normalized_candidate in normalized_source:
        return True
    if suffix != ".pdf" or len(normalized_candidate) < 80:
        return False
    compact_candidate = re.sub(r"\s+", "", normalized_candidate).lower()
    compact_source = re.sub(r"\s+", "", normalized_source).lower()
    return SequenceMatcher(None, compact_candidate, compact_source).ratio() >= 0.985
