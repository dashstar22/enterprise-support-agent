"""Run the fixed evaluation with a configured real model. / 使用已配置真实模型运行固定评测。"""

# ruff: noqa: E402

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, cast

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
stdout_reconfigure = getattr(sys.stdout, "reconfigure", None)
if callable(stdout_reconfigure):
    stdout_reconfigure(encoding="utf-8")

from app.agent.llm import OpenAICompatibleSupportAnswerGenerator
from app.config import Settings
from app.evaluation.runner import (
    DEFAULT_QUESTIONS_PATH,
    C6EvaluationRunner,
    export_result,
    load_question_set,
)


def main() -> None:
    parser = argparse.ArgumentParser(description="使用真实模型运行 C6 固定评测。")
    parser.add_argument("--questions", type=Path, default=DEFAULT_QUESTIONS_PATH)
    parser.add_argument(
        "--output", type=Path, default=Path("data/evaluation/results/real_model_latest.json")
    )
    args = parser.parse_args()

    settings = Settings()
    if not settings.llm_enabled:
        raise SystemExit("已停止, ESA_LLM_ENABLED 必须为 true，且需提供完整模型配置。")
    assert settings.llm_base_url is not None
    assert settings.llm_api_key is not None
    assert settings.llm_model is not None

    generator = OpenAICompatibleSupportAnswerGenerator(
        base_url=str(settings.llm_base_url),
        api_key=settings.llm_api_key,
        model=settings.llm_model,
        timeout_seconds=settings.llm_timeout_seconds,
    )
    question_set = load_question_set(args.questions)
    result = C6EvaluationRunner(question_set, answer_generator=generator).run()
    export_result(result, args.output)
    metrics = cast(dict[str, Any], result["metrics"])
    runtime = cast(dict[str, Any], result["runtime"])
    token_accounting = cast(dict[str, Any], metrics["token_accounting"])
    print(f"评测版本: {result['evaluation_version']}")
    print(f"回答生成器: {runtime['answer_generator']}")
    print(f"系统状态通过率: {metrics['system_status_pass_rate']}")
    print(f"结构化输出通过率: {metrics['structured_output_pass_rate']}")
    print(f"当前引用准确率: {metrics['current_citation_accuracy']}")
    print(
        f"模型 Token: {token_accounting['input_tokens']} 输入 / {token_accounting['output_tokens']} 输出"
    )
    print(f"结果文件: {args.output}")


if __name__ == "__main__":
    main()
