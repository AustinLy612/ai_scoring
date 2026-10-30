"""数据模型：日记、标注片段、步骤深度评分、批量处理状态。"""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field

# Crane et al. (2019) self-reflection 五步
STEP_NAMES: dict[int, str] = {
    1: "自我觉察",
    2: "触发识别",
    3: "重新评估",
    4: "评价",
    5: "着眼未来",
}

# 每步深度评分 0 / 1 / 2（Coding diary 0920 版编码标准）
DEPTH_NAMES: dict[int, str] = {
    0: "没有描述",
    1: "有一些描述",
    2: "明确且深度描述",
}


class DiaryStatus(str, Enum):
    pending = "pending"
    processing = "processing"
    done = "done"
    failed = "failed"


class Segment(BaseModel):
    """一段被标注的文本。同一段文本若属于多个步骤，会拆成多条 Segment 并互相登记重叠。"""

    id: str
    start: int
    end: int
    text: str

    step: int = Field(ge=1, le=5)
    step_reason: str = ""

    # AI 原始结果，人工修正后仍保留
    ai_step: int | None = None

    overlaps_with: list[str] = Field(default_factory=list)
    source: Literal["ai", "human_added"] = "ai"
    status: Literal["unchanged", "modified", "deleted"] = "unchanged"

    # 片段文本在原文中未能精确匹配时为 True（用模糊匹配定位）
    fuzzy_located: bool = False

    @property
    def step_name(self) -> str:
        return STEP_NAMES.get(self.step, "")


class StepScore(BaseModel):
    """一篇日记在某个步骤上的深度得分（0/1/2）。"""

    step: int = Field(ge=1, le=5)
    score: int | None = Field(default=None, ge=0, le=2)
    reason: str = ""

    # AI 原始结果，人工修正后仍保留
    ai_score: int | None = None
    ai_reason: str = ""

    status: Literal["unchanged", "modified"] = "unchanged"

    @property
    def step_name(self) -> str:
        return STEP_NAMES.get(self.step, "")

    @property
    def score_name(self) -> str:
        return DEPTH_NAMES.get(self.score if self.score is not None else -1, "")


def empty_step_scores() -> list[StepScore]:
    return [StepScore(step=i) for i in range(1, 6)]


class Diary(BaseModel):
    id: str
    text: str
    # 导入时携带的附加列（被试编号、日期等），原样带入导出
    extra: dict[str, Any] = Field(default_factory=dict)
    source_name: str = ""

    status: DiaryStatus = DiaryStatus.pending
    error: str = ""
    # 处理过程中的非致命问题（如片段无法在原文定位）
    warnings: list[str] = Field(default_factory=list)
    segments: list[Segment] = Field(default_factory=list)
    # 固定 5 项，按步骤 1-5 排列；未处理时 score 为 None
    step_scores: list[StepScore] = Field(default_factory=empty_step_scores)

    # 人工是否动过这篇
    reviewed: bool = False

    def active_segments(self) -> list[Segment]:
        return [s for s in self.segments if s.status != "deleted"]

    def step_score(self, step: int) -> StepScore:
        for item in self.step_scores:
            if item.step == step:
                return item
        item = StepScore(step=step)
        self.step_scores.append(item)
        self.step_scores.sort(key=lambda s: s.step)
        return item


class DiarySummary(BaseModel):
    """列表视图用的轻量信息。"""

    id: str
    source_name: str
    char_count: int
    status: DiaryStatus
    error: str
    segment_count: int
    total_depth: int | None
    reviewed: bool


class SegmentUpdate(BaseModel):
    """前端提交的片段修正。id 不在现有片段中（如 new- 开头）视为新增。"""

    id: str
    start: int
    end: int
    text: str
    step: int = Field(ge=1, le=5)
    step_reason: str = ""
    deleted: bool = False


class StepScoreUpdate(BaseModel):
    step: int = Field(ge=1, le=5)
    score: int | None = Field(default=None, ge=0, le=2)
    reason: str = ""


class ReviewUpdate(BaseModel):
    """一次人工修正提交：片段 + 五个步骤的深度分。"""

    segments: list[SegmentUpdate] = Field(default_factory=list)
    step_scores: list[StepScoreUpdate] = Field(default_factory=list)


class ImportTextRequest(BaseModel):
    id: str = ""
    text: str


class ProcessRequest(BaseModel):
    # 留空表示处理所有 pending/failed 的日记
    diary_ids: list[str] = Field(default_factory=list)
    force: bool = False
