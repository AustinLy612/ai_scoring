"""真实模型连通性检查：拿一篇样例日记跑一次完整的两阶段编码（会调用本地 Qwen）。

运行：python -m tests.api_check [日记文件路径]
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.llm_client import get_client  # noqa: E402
from backend.pipeline import process_diary  # noqa: E402
from backend.schemas import DEPTH_NAMES, STEP_NAMES, Diary  # noqa: E402

DEFAULT_SAMPLE = Path(__file__).resolve().parent.parent / "samples" / "P001-day1.txt"


async def main() -> None:
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_SAMPLE
    diary = Diary(id=path.stem, text=path.read_text(encoding="utf-8").strip())

    print(f"日记 {diary.id}（{len(diary.text)} 字），开始调用本地 Qwen…\n")
    await process_diary(diary, get_client())

    print(f"状态：{diary.status.value}")
    if diary.error:
        print(f"错误：{diary.error}")
    for warning in diary.warnings:
        print(f"提示：{warning}")

    print("\n五步深度评分：")
    total = 0
    for score in diary.step_scores:
        label = f"{score.score} {DEPTH_NAMES[score.score]}" if score.score is not None else "未评分"
        total += score.score or 0
        print(f"  步骤{score.step} {STEP_NAMES[score.step]:<4}：{label}")
        print(f"      理由：{score.reason}")
    print(f"  总深度分：{total} / 10")

    print(f"\n共标注 {len(diary.segments)} 个片段：")
    for seg in diary.segments:
        print(f"\n[步骤{seg.step} {STEP_NAMES[seg.step]}｜[{seg.start}, {seg.end})]")
        print(f"  原文：{seg.text}")
        print(f"  步骤理由：{seg.step_reason}")
        if seg.overlaps_with:
            print(f"  重叠：{', '.join(seg.overlaps_with)}")


if __name__ == "__main__":
    asyncio.run(main())
