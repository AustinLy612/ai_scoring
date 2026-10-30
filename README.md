# 日记自我反思 AI 编码评分工具

对中文日记做两阶段自动编码：

1. **阶段一 标注**：按 Crane et al. (2019) 的 self-reflection 五步（自我觉察 / 触发识别 / 重新评估 / 评价 / 着眼未来），在全文中标出对应的文本片段。一篇日记不一定包含全部五步，未出现的步骤不标注。
2. **阶段二 评分**：按课题组 Codebook 通读全文，为五个步骤各评**深度分 0 / 1 / 2**（0 没有描述、1 有一些描述、2 具体且深度描述）。阶段一的片段只作参考，没有片段的步骤也由模型判断（隐含体现同样算数）。五步之和为总深度分（0–10）。

**问答型日记**：若文本是含 `originalDiary` 与 `问题1-5 / 答案1-5` 的 JSON（即使被截断），导入时会转成「原始日记 + 问答1–5」结构；按 Codebook，步骤 i 只依据答案 i 评分，原始日记仅作背景。问答型日记只看「原始日记」和五个问答，不看「AI生成的重构叙事」「是否修改了重构叙事」「最终重构叙事」。

模型：通过 SSH 登录实验室机器（默认 `10.200.20.244`），调用其上部署的本地 Qwen（OpenAI 兼容 `/v1/chat/completions`）。

## 安装

```bash
pip install -r requirements.txt
cp .env.example .env    # Windows: copy .env.example .env
```

编辑 `.env`。实验室机器 `10.200.20.244` 的 SSH 用户是 `root01`（本机 `~/.ssh/config` 里已经写过，所以平时登录只输入 IP 和密码）。密码、主机、Qwen 接口默认已按这台机器填写。

Qwen 用 vLLM 开在远程 `127.0.0.1:8000`。若评分时报接口连不上，先在服务器上启动模型，例如：

```bash
python -m vllm.entrypoints.openai.api_server \
  --model "/home/root01/Public/privacy/llm_models/Qwen3.5-9B-GPTQ-4bit" \
  --served-model-name "Qwen3.5-9B-GPTQ-4bit" \
  --host 0.0.0.0 --port 8000
```

| 变量 | 说明 |
| --- | --- |
| `LLM_SSH_HOST` | SSH 主机，默认 `10.200.20.244` |
| `LLM_SSH_PORT` | SSH 端口，默认 `22` |
| `LLM_SSH_USER` | SSH 用户名，默认 `root01` |
| `LLM_SSH_PASSWORD` | SSH 密码 |
| `QWEN_API_BASE` | 远程本机 Qwen 接口，如 `http://127.0.0.1:8000/v1`；留空则自动探测 |
| `QWEN_MODEL` | 模型名；留空则优先选择名称含 qwen 的模型 |
| `QWEN_API_KEY` | 若 Qwen 服务开启了鉴权再填写 |
| `STAGE2_PROMPT_VERSION` | 评分 prompt 版本，默认 `v7` |
| `MAX_CONCURRENCY` | 批量处理并发数，默认 2 |

程序 SSH 登录后，会在远程 `127.0.0.1` 上探测常见端口（8000、11434 等）的 `/v1/models`。若自动探测失败，请把实际地址写入 `QWEN_API_BASE`。

## 启动

```bash
python -m uvicorn backend.main:app --reload --port 8000
```

浏览器打开 <http://127.0.0.1:8000>。

## 使用流程

1. **导入**：点击「导入文件」批量上传，或「粘贴日记」输入单篇。
   - `.txt`：一篇日记一个文件，**文件名作为日记 ID**。
   - `.csv` / `.xlsx`：一行一篇，必须有 `text` 列（也接受 `文本`/`内容`/`日记`/`content`）；可选 `id` 列（无则按行号自动编号）；**其余列会原样带入导出**（如被试编号、日期）。
2. **处理**：点击「开始处理」，按并发数排队调用模型；列表显示每篇的状态，失败的可再次点击「开始处理」重试（勾选「重跑已完成」可强制重跑全部）。
3. **人工修正**：中栏按五色高亮显示标注结果（斜条纹 = 多个步骤重叠）。右栏上半是「步骤深度评分」，五个步骤各一行，可改 0/1/2 与理由；某步骤有片段却是 0 分、或无片段却有分时会标红提示「与分数不一致」。右栏下半是「标注片段」，可改步骤、改理由、删除；在中栏选中文字即可「新增片段」。改完点「保存修正」。
   - AI 原始判断始终保留，界面上以「AI原判 …」标签提示差异，导出时 AI 列与最终列并列。
4. **导出**：点击「导出 Excel」，得到三个工作表：
   - **日记汇总**（一行一篇）：字数、处理状态、五个步骤各自的 AI 深度分与最终深度分、AI 总分与最终总分、各步骤片段数、片段总数、步骤分/片段的人工修改计数、处理提示。
   - **步骤评分**（一行一篇×一步，共 5 行/篇）：步骤、该步骤片段数、AI 深度分、最终深度分、AI 理由、最终理由、修改状态。适合直接做长格式统计。
   - **片段明细**（一行一个片段）：片段文本、起止位置、AI 步骤、最终步骤、步骤理由、重叠片段、片段来源（AI/人工新增）、修改状态、是否模糊定位。

## 说明与注意

- 数据只保存在**当前进程内存**中，重启服务后清空。处理完请及时导出 Excel。
- 片段定位：模型必须逐字复制原文，程序据此定位字符区间。若模型改写了个别字，程序会忽略空白差异再做模糊匹配，并在界面与导出中标为「模糊定位」，请人工核对。
- 允许一段文本同时属于多个步骤，重叠关系记录在 `重叠片段` 列。
- 无法定位或 step 非法的标注会被跳过，并在该篇日记的「处理提示」中列出。
- 本工具在本地电脑运行，通过 SSH 访问实验室机器上的 Qwen；不要把 `.env` 提交到 git。

## 自检脚本

| 命令 | 作用 | 是否调用模型 |
| --- | --- | --- |
| `python -m tests.test_smoke` | 用假模型跑通「导入 → 处理 → 修正 → 导出」并校验 Excel 内容 | 否 |
| `python -m tests.ui_check` | 用系统 Edge 打开界面走一遍完整操作并截图到 `tests/screenshots/`（需 `pip install playwright`） | 否 |
| `python -m tests.api_check [日记文件]` | 经 SSH 跑一篇日记，打印五步深度分、理由与每个片段 | 是 |
| `python -m tests.ui_check real` | 用真实模型处理 `samples/` 并在浏览器里走完修正与导出 | 是 |
| `python -m tests.agreement_check 人工表.xlsx [导出.xlsx]` | 与人工一致分（表的最后 5 列）比较：完全一致率、相差≤1、二次加权 kappa、各步骤错位明细 | 是 |

批量给 Excel（需含 `ID`、`Text` 两列）评分：

```bash
python -m scripts.score_file 输入.xlsx 输出.xlsx --prompt=v7
```

## 目录

```
backend/
  main.py        FastAPI 路由 + 静态托管
  config.py      .env 配置
  schemas.py     数据模型
  llm_client.py  SSH → 本地 Qwen 调用（JSON 解析、重试、并发限流）
  prompts.py     两阶段提示词（内嵌五步定义与五步深度评分标准；改评分口径改这里）
  pipeline.py    两阶段流水线 + 重叠检测
  locator.py     片段回原文定位（精确 / 忽略空白 / 模糊）
  importers.py   txt / csv / xlsx / 粘贴解析
  exporter.py    Excel 导出
  store.py       会话内存存储
frontend/
  index.html  app.js  style.css
samples/         示例日记（3 篇 txt + 1 个 csv）
tests/           自检脚本
```

## 参考文献与编码标准

- Crane, M. F., Searle, B. J., Kangas, M., & Nwiran, Y. (2019). How resilience is strengthened by exposure to stressors: the systematic self-reflection model of resilience strengthening.（五步定义）
- 课题组 Codebook：五步各自的 0/1/2 深度评分标准，已写入 `backend/prompts.py`。
