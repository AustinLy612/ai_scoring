"""内存中的会话数据（导入 -> 处理 -> 修正 -> 导出，不跨进程持久化）。"""

from __future__ import annotations

import asyncio

from .schemas import Diary, DiaryStatus, DiarySummary


class DiaryStore:
    def __init__(self) -> None:
        self._diaries: dict[str, Diary] = {}
        self._paste_counter = 0
        self.processing_task: asyncio.Task | None = None

    # --- 增删查 -------------------------------------------------------------

    def add(self, diary: Diary) -> Diary:
        diary.id = self._unique_id(diary.id)
        self._diaries[diary.id] = diary
        return diary

    def _unique_id(self, base: str) -> str:
        candidate = base or "日记"
        if candidate not in self._diaries:
            return candidate
        index = 2
        while f"{candidate}({index})" in self._diaries:
            index += 1
        return f"{candidate}({index})"

    def next_paste_index(self) -> int:
        self._paste_counter += 1
        return self._paste_counter

    def get(self, diary_id: str) -> Diary | None:
        return self._diaries.get(diary_id)

    def all(self) -> list[Diary]:
        return list(self._diaries.values())

    def remove(self, diary_id: str) -> bool:
        return self._diaries.pop(diary_id, None) is not None

    def clear(self) -> None:
        self._diaries.clear()
        self._paste_counter = 0

    # --- 状态 ---------------------------------------------------------------

    def summaries(self) -> list[DiarySummary]:
        return [
            DiarySummary(
                id=d.id,
                source_name=d.source_name,
                char_count=len(d.text),
                status=d.status,
                error=d.error,
                segment_count=len(d.active_segments()),
                total_depth=_total_depth(d),
                reviewed=d.reviewed,
            )
            for d in self._diaries.values()
        ]

    def is_processing(self) -> bool:
        return self.processing_task is not None and not self.processing_task.done()

    def targets_for_processing(self, diary_ids: list[str], force: bool) -> list[Diary]:
        pool = (
            [d for d in self._diaries.values() if d.id in set(diary_ids)]
            if diary_ids
            else list(self._diaries.values())
        )
        if force:
            return pool
        return [d for d in pool if d.status in (DiaryStatus.pending, DiaryStatus.failed)]


def _total_depth(diary: Diary) -> int | None:
    """五步深度分之和（0-10）；任一步骤尚未评分则为 None。"""
    scores = [s.score for s in diary.step_scores]
    if len(scores) < 5 or any(v is None for v in scores):
        return None
    return sum(v for v in scores if v is not None)


store = DiaryStore()
