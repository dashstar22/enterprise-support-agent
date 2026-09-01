"""Run the C6 fixed evaluation and export one local JSON result. / 运行 C6 固定评测并导出本地 JSON。"""

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

from app.evaluation.runner import (
    DEFAULT_QUESTIONS_PATH,
    C6EvaluationRunner,
    export_result,
    load_question_set,
    load_review_sheet,
)


def main() -> None:
    parser = argparse.ArgumentParser(description="运行固定 C6 评测。")
    parser.add_argument("--questions", type=Path, default=DEFAULT_QUESTIONS_PATH)
    parser.add_argument("--review", type=Path)
    parser.add_argument(
        "--output", type=Path, default=Path("data/evaluation/results/c6_latest.json")
    )
    args = parser.parse_args()

    question_set = load_question_set(args.questions)
    review_sheet = load_review_sheet(args.review) if args.review is not None else None
    result = C6EvaluationRunner(question_set).run(review_sheet)
    export_result(result, args.output)
    metrics = cast(dict[str, Any], result["metrics"])
    print(f"评测版本: {result['evaluation_version']}")
    print(f"系统状态通过率: {metrics['system_status_pass_rate']}")
    print(f"结果文件: {args.output}")


if __name__ == "__main__":
    main()
