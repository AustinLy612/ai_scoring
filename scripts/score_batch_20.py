"""对 Downloads/20 (2).xlsx 的 20 条日志按当前两阶段流程评分并导出标准 Excel。"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from openpyxl import load_workbook

from backend.config import get_settings
from backend.exporter import build_workbook
from backend.importers import normalize_diary_text
from backend.llm_client import LLMClient
from backend.pipeline import process_diary
from backend.schemas import Diary

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("score_batch_20")

INPUT_PATH = Path(r"c:\Users\AustinHan\Downloads\20 (2).xlsx")
OUTPUT_PATH = Path(r"c:\Users\AustinHan\Downloads\20 (2)_评分结果.xlsx")


def load_diaries(path: Path) -> list[Diary]:
    wb = load_workbook(path, data_only=True)
    ws = wb.active
    diaries: list[Diary] = []
    seen: dict[str, int] = {}

    for row_no in range(2, ws.max_row + 1):
        raw_id = ws.cell(row_no, 1).value
        raw_text = ws.cell(row_no, 2).value
        if raw_id is None and (raw_text is None or str(raw_text).strip() == ""):
            continue

        base_id = str(raw_id).strip() if raw_id is not None else f"row{row_no}"
        count = seen.get(base_id, 0) + 1
        seen[base_id] = count
        diary_id = base_id if count == 1 else f"{base_id}#{count}"

        scorable = normalize_diary_text(str(raw_text or "").strip())
        source_field = "问答" if "【问答1" in scorable else "plain"
        if not scorable:
            logger.warning("行 %s (%s) 无可评分正文，跳过", row_no, diary_id)
            continue

        diaries.append(
            Diary(
                id=diary_id,
                text=scorable,
                source_name=path.name,
                extra={
                    "原始ID": base_id,
                    "行号": row_no,
                    "评分文本来源": source_field,
                    "原始Text字数": len(str(raw_text or "")),
                },
            )
        )
    return diaries


async def main() -> None:
    settings = get_settings()
    if not settings.llm_ssh_user or not settings.llm_ssh_password:
        raise SystemExit("未配置 LLM_SSH_USER / LLM_SSH_PASSWORD")

    diaries = load_diaries(INPUT_PATH)
    logger.info("载入 %s 条日记，开始评分…", len(diaries))
    for d in diaries:
        logger.info(
            "  %s | 来源=%s | 字数=%s",
            d.id,
            d.extra.get("评分文本来源"),
            len(d.text),
        )

    client = LLMClient()
    sem = asyncio.Semaphore(settings.max_concurrency)

    async def run_one(diary: Diary) -> Diary:
        async with sem:
            logger.info("处理中：%s", diary.id)
            result = await process_diary(diary, client)
            scores = [
                s.score if s.score is not None else 0 for s in result.step_scores
            ]
            logger.info(
                "完成：%s status=%s scores=%s total=%s segs=%s",
                result.id,
                result.status.value,
                scores,
                sum(scores),
                len(result.segments),
            )
            return result

    await asyncio.gather(*(run_one(d) for d in diaries))

    failed = [d for d in diaries if d.status.value == "failed"]
    done = [d for d in diaries if d.status.value == "done"]
    logger.info("完成 %s / 失败 %s / 合计 %s", len(done), len(failed), len(diaries))
    for d in failed:
        logger.error("失败 %s: %s", d.id, d.error)

    OUTPUT_PATH.write_bytes(build_workbook(diaries))
    logger.info("已写出：%s", OUTPUT_PATH)


if __name__ == "__main__":
    asyncio.run(main())
