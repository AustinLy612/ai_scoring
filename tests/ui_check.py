"""界面验证脚本：用 Edge 打开本地页面，跑一遍导入->处理->修正->导出并截图。

前置：pip install playwright（使用系统 Edge，无需下载浏览器）
运行：
  python -m tests.ui_check         用假模型，不调用真实模型
  python -m tests.ui_check real    用真实本地 Qwen 处理 samples/ 下的样例
"""

from __future__ import annotations

import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
SAMPLES = ROOT / "samples"
SHOTS = Path(__file__).resolve().parent / "screenshots"
PORT = 8078


def wait_for_server(url: str, timeout: float = 30) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2):
                return
        except Exception:  # noqa: BLE001
            time.sleep(0.5)
    raise RuntimeError(f"服务未在 {timeout}s 内启动：{url}")


def run(real: bool = False) -> None:
    SHOTS.mkdir(exist_ok=True)
    app_module = "backend.main:app" if real else "tests.fake_server:app"
    prefix = "real" if real else "fake"
    process_timeout = 300000 if real else 30000
    print(f"使用 {'真实本地 Qwen' if real else '假模型'} 启动 {app_module}")
    server = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", app_module, "--port", str(PORT)],
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        wait_for_server(f"http://127.0.0.1:{PORT}/api/config")
        with sync_playwright() as p:
            browser = p.chromium.launch(channel="msedge")
            page = browser.new_page(viewport={"width": 1600, "height": 950})
            errors: list[str] = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)

            page.goto(f"http://127.0.0.1:{PORT}/")
            page.wait_for_selector("#modelInfo:not(:empty)")

            # 导入样例
            page.set_input_files(
                "#fileInput",
                [str(p) for p in sorted(SAMPLES.glob("*.txt"))] + [str(SAMPLES / "batch_example.csv")],
            )
            page.wait_for_selector("#diaryList li")
            assert page.locator("#diaryList li").count() == 5

            # 处理
            page.click("#processBtn")
            page.wait_for_function(
                "() => document.querySelectorAll('#diaryList .status.done').length === 5",
                timeout=process_timeout,
            )

            # 打开第一篇
            page.locator("#diaryList li").first.click()
            page.wait_for_selector("#diaryBody mark")
            marks = page.locator("#diaryBody mark").count()
            cards = page.locator(".seg-card").count()
            multi = page.locator("#diaryBody mark.multi").count()
            print(f"高亮块 {marks} 个（其中重叠 {multi} 个），片段卡片 {cards} 张")
            assert marks > 0 and cards > 0
            page.screenshot(path=str(SHOTS / f"{prefix}_01_annotated.png"))

            if real:
                # 逐篇看一眼，确认纯事实日记不产生标注
                for index in range(5):
                    item = page.locator("#diaryList li").nth(index)
                    name = item.locator(".id").inner_text()
                    item.click()
                    page.wait_for_function(
                        "(id) => document.getElementById('diaryTitle').textContent.startsWith(id)",
                        arg=name,
                    )
                    print(f"  {name}: {page.locator('.seg-card').count()} 个片段")
                    page.screenshot(path=str(SHOTS / f"real_diary_{name}.png"))
                page.locator("#diaryList li").first.click()
                page.wait_for_selector("#diaryBody mark")
                cards = page.locator(".seg-card").count()

            # 选中文字 -> 新增片段
            page.evaluate(
                """() => {
                  const spans = [...document.querySelectorAll('#diaryBody span, #diaryBody mark')];
                  const target = spans[spans.length - 1];
                  const range = document.createRange();
                  range.setStart(target.firstChild, 0);
                  range.setEnd(target.firstChild, Math.min(10, target.textContent.length));
                  const sel = window.getSelection();
                  sel.removeAllRanges();
                  sel.addRange(range);
                  document.getElementById('diaryBody').dispatchEvent(new MouseEvent('mouseup', {bubbles: true}));
                }"""
            )
            page.wait_for_selector("#selectionBar:not(.hidden)")
            page.select_option("#newStep", "5")
            page.click("#addSegmentBtn")
            page.wait_for_selector(".tag.human")
            assert page.locator(".seg-card").count() == cards + 1

            # 步骤评分区应有 5 行；改步骤 5 的深度分为 2、改第一条片段的步骤为 4，再保存
            assert page.locator(".score-row").count() == 5
            current = page.locator(".score-row.s5 .score-select").input_value()
            new_value = "1" if current == "2" else "2"
            page.locator(".score-row.s5 .score-select").select_option(new_value)
            page.locator(".score-row.s5 .score-reason").fill("人工核对后调整了着眼未来的深度分")
            page.locator(".seg-card .step-select").first.select_option("4")
            page.click("#saveBtn")
            page.wait_for_selector(".score-row.s5 .tag.ai-diff")
            page.wait_for_selector("#diaryList li.active .muted:has-text('已修正')")
            total = page.locator("#totalDepth").inner_text()
            print("保存后：", total)
            assert "总深度分" in total
            page.screenshot(path=str(SHOTS / f"{prefix}_02_after_edit.png"))

            # 导出
            with page.expect_download() as download:
                page.click("#exportBtn")
            saved = SHOTS.parent / f"_ui_export_{prefix}.xlsx"
            download.value.save_as(str(saved))
            print(f"导出文件：{saved}（{saved.stat().st_size} 字节）")

            real_errors = [e for e in errors if "favicon" not in e]
            assert not real_errors, real_errors
            print("界面无 JS 报错，截图已保存到", SHOTS)
            browser.close()
    finally:
        server.terminate()
        server.wait(timeout=10)


if __name__ == "__main__":
    run(real="real" in sys.argv[1:])
