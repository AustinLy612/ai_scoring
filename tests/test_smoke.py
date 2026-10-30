"""端到端冒烟测试：用假模型替代本地 Qwen，验证导入 -> 处理 -> 修正 -> 导出全链路。

运行：python -m tests.test_smoke
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

from fastapi.testclient import TestClient
from openpyxl import load_workbook

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend import llm_client, main  # noqa: E402
from backend.store import store  # noqa: E402

SAMPLES = Path(__file__).resolve().parent.parent / "samples"


class FakeClient:
    """按提示词判断处于哪个阶段，返回可预期的 JSON。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def complete_json(self, system_prompt: str, user_prompt: str, **_: object) -> dict:
        diary = user_prompt.split("<<<DIARY_START>>>\n", 1)[1].split("\n<<<DIARY_END>>>", 1)[0]

        if "阶段一自动标注的片段" in user_prompt:  # 阶段二：五步全评
            self.calls.append("stage2")
            payload = json.loads(user_prompt.split("（按步骤分组，仅供参考）：\n", 1)[1].rsplit("\n\n", 1)[0])
            assert [g["step"] for g in payload] == [1, 2, 3, 4, 5], payload
            return {
                "step_scores": [
                    # 无片段的步骤评 0；步骤 3 有片段但故意评 0；步骤 1 评 2，其余评 1
                    {"step": g["step"],
                     "score": 0 if (g["step"] == 3 or not g["segments"]) else (2 if g["step"] == 1 else 1),
                     "reason": f"步骤{g['step']}综合{len(g['segments'])}个片段"}
                    for g in payload
                ]
            }

        self.calls.append("stage1")
        first_line = diary.splitlines()[0]
        segments = [
            # 精确复制原文
            {"step": 1, "text": first_line, "reason": "描述当时的想法与感受"},
            # 同一句同时标为第 2 步（测试重叠）
            {"step": 2, "text": first_line[: max(6, len(first_line) // 2)], "reason": "指出原因"},
            # 改写过的片段（测试模糊定位）
            {"step": 3, "text": first_line.replace("我", "本人")[:12], "reason": "重新评估"},
            # 完全不存在的片段（测试跳过）
            {"step": 4, "text": "这句话根本不在原文里出现过", "reason": "无法定位"},
            # 非法 step
            {"step": 9, "text": first_line, "reason": "非法步骤"},
        ]
        return {"segments": segments}


def main_test() -> None:
    fake = FakeClient()
    llm_client._client = fake  # noqa: SLF001 - 注入假客户端
    store.clear()

    client = TestClient(main.app)

    # 配置
    config = client.get("/api/config").json()
    assert config["model"] and config["depth_names"]["2"], config
    print("配置:", config["model"], "SSH", config.get("ssh_host"))

    # 导入 txt + csv
    files = [
        ("files", (p.name, p.read_bytes(), "text/plain"))
        for p in sorted(SAMPLES.glob("*.txt"))
    ]
    csv_path = SAMPLES / "batch_example.csv"
    files.append(("files", (csv_path.name, csv_path.read_bytes(), "text/csv")))
    resp = client.post("/api/import", files=files)
    assert resp.status_code == 200, resp.text
    imported = resp.json()
    assert not imported["errors"], imported["errors"]
    assert len(imported["added"]) == 5, imported["added"]
    print("已导入:", imported["added"])

    # 粘贴导入（含重复 ID，验证自动去重）
    resp = client.post("/api/import/text", json={"id": "P001-day1", "text": "我今天很累。因为熬夜了。"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["added"] == ["P001-day1(2)"], resp.json()["added"]

    # 附加列保留
    csv_diary = client.get("/api/diary/P004-day1").json()
    assert csv_diary["extra"] == {"被试": "P004", "日期": "2026-03-01"}, csv_diary["extra"]

    # 处理
    resp = client.post("/api/process", json={"diary_ids": [], "force": False})
    assert resp.status_code == 200, resp.text
    assert resp.json()["started"] == 6, resp.json()
    status = client.get("/api/status").json()
    assert not status["processing"], status
    assert all(d["status"] == "done" for d in status["diaries"]), status["diaries"]
    print("处理完成，阶段调用次数:", len(fake.calls))

    # 检查一篇的结果
    diary = client.get("/api/diary/P001-day1").json()
    segments = diary["segments"]
    steps = sorted(s["step"] for s in segments)
    assert steps == [1, 2, 3], steps  # step 9 与无法定位的 step 4 被跳过
    assert all(s["text"] == diary["text"][s["start"]: s["end"]] for s in segments), segments
    assert any(s["fuzzy_located"] for s in segments), "应有一条模糊定位片段"
    assert any(s["overlaps_with"] for s in segments), "应检测到重叠"
    assert any("无法在原文中定位" in w for w in diary["warnings"]), diary["warnings"]

    scores = {s["step"]: s for s in diary["step_scores"]}
    assert sorted(scores) == [1, 2, 3, 4, 5], scores
    assert scores[1]["score"] == 2 and scores[1]["ai_score"] == 2, scores[1]
    assert scores[2]["score"] == 1, scores[2]
    assert scores[3]["score"] == 0 and "综合" in scores[3]["reason"], scores[3]  # 模型评 0
    assert scores[4]["score"] == 0 and "综合0个片段" in scores[4]["reason"], scores[4]  # 无片段也交给模型评
    assert scores[5]["score"] == 0 and scores[5]["ai_score"] == 0, scores[5]
    listed = next(d for d in status["diaries"] if d["id"] == "P001-day1")
    assert listed["total_depth"] == 3, listed
    print("片段数:", len(segments), "警告数:", len(diary["warnings"]), "深度分:", {k: v["score"] for k, v in scores.items()})

    # 人工修正：改第一条片段的步骤、删第二条、新增一条；把步骤 4 的分改为 2、步骤 1 改为 1
    seg_updates = []
    for index, seg in enumerate(segments):
        item = {
            "id": seg["id"],
            "start": seg["start"],
            "end": seg["end"],
            "text": seg["text"],
            "step": seg["step"],
            "step_reason": seg["step_reason"],
            "deleted": False,
        }
        if index == 0:
            item["step"] = 5
        if index == 1:
            item["deleted"] = True
        seg_updates.append(item)
    seg_updates.append({
        "id": "new-1",
        "start": 0,
        "end": 8,
        "text": diary["text"][0:8],
        "step": 4,
        "step_reason": "人工补充",
        "deleted": False,
    })
    score_updates = [
        {"step": s["step"], "score": s["score"], "reason": s["reason"]} for s in diary["step_scores"]
    ]
    for item in score_updates:
        if item["step"] == 4:
            item["score"] = 2
            item["reason"] = "人工补充了评价片段"
        if item["step"] == 1:
            item["score"] = 1
    resp = client.put("/api/diary/P001-day1/review", json={"segments": seg_updates, "step_scores": score_updates})
    assert resp.status_code == 200, resp.text
    updated = resp.json()
    assert updated["reviewed"] is True

    by_id = {s["id"]: s for s in updated["segments"]}
    changed = by_id[segments[0]["id"]]
    assert changed["step"] == 5 and changed["ai_step"] == segments[0]["step"], changed
    assert changed["status"] == "modified", changed
    assert by_id[segments[1]["id"]]["status"] == "deleted"
    human = [s for s in updated["segments"] if s["source"] == "human_added"]
    assert len(human) == 1 and human[0]["id"] == "P001-day1#h1", human

    new_scores = {s["step"]: s for s in updated["step_scores"]}
    assert new_scores[4]["score"] == 2 and new_scores[4]["ai_score"] == 0 and new_scores[4]["status"] == "modified", new_scores[4]
    assert new_scores[1]["score"] == 1 and new_scores[1]["ai_score"] == 2 and new_scores[1]["status"] == "modified", new_scores[1]
    assert new_scores[2]["status"] == "unchanged", new_scores[2]
    print("修正后深度分:", {k: (v["ai_score"], v["score"], v["status"]) for k, v in new_scores.items()})

    # 非法区间被拒
    bad = [dict(seg_updates[0], start=0, end=99999)]
    assert client.put("/api/diary/P001-day1/review", json={"segments": bad, "step_scores": []}).status_code == 400

    # 导出
    resp = client.get("/api/export.xlsx")
    assert resp.status_code == 200, resp.text
    workbook = load_workbook(io.BytesIO(resp.content))
    assert workbook.sheetnames == ["日记汇总", "步骤评分", "片段明细"], workbook.sheetnames

    detail = workbook["片段明细"]
    headers = [c.value for c in detail[1]]
    for column in ("AI步骤", "最终步骤", "被试", "日期", "重叠片段", "片段来源", "修改状态"):
        assert column in headers, (column, headers)
    assert not any("feeling" in str(h).lower() for h in headers), headers
    rows = list(detail.iter_rows(min_row=2, values_only=True))
    assert rows, "明细表为空"

    step_sheet = workbook["步骤评分"]
    sh = [c.value for c in step_sheet[1]]
    step_rows = [dict(zip(sh, r)) for r in step_sheet.iter_rows(min_row=2, values_only=True)]
    assert len(step_rows) == 6 * 5, len(step_rows)
    p001_step4 = next(r for r in step_rows if r["日记ID"] == "P001-day1" and r["步骤"] == 4)
    assert p001_step4["AI深度分"] == 0 and p001_step4["最终深度分"] == 2 and p001_step4["修改状态"] == "人工修改", p001_step4
    assert p001_step4["该步骤片段数"] == 1, p001_step4

    summary = workbook["日记汇总"]
    summary_headers = [c.value for c in summary[1]]
    assert "步骤1(自我觉察)_最终深度分" in summary_headers, summary_headers
    summary_rows = {
        row[0]: dict(zip(summary_headers, row))
        for row in summary.iter_rows(min_row=2, values_only=True)
    }
    p001 = summary_rows["P001-day1"]
    assert p001["片段总数"] == 3, p001  # 3 条 AI - 1 删除 + 1 人工新增
    assert p001["AI总深度分"] == 3 and p001["最终总深度分"] == 4, p001  # AI: 2+1+0+0+0；最终: 1+1+0+2+0
    assert p001["步骤分人工修改数"] == 2, p001
    assert p001["片段人工修改数"] == 1 and p001["片段人工新增数"] == 1 and p001["片段人工删除数"] == 1, p001
    print("汇总行 P001-day1:", {k: v for k, v in p001.items() if v not in ("", None)})

    out = Path(__file__).resolve().parent / "_smoke_output.xlsx"
    out.write_bytes(resp.content)
    print(f"\n全部断言通过。导出样例已写入 {out}")


if __name__ == "__main__":
    main_test()
