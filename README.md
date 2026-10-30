# 日记自我反思终端评分

用终端给 Excel 里的日记打五步深度分（0/1/2）。默认经 SSH 调用实验室机器 `10.200.20.244` 上的 Qwen。

## 准备

```bash
pip install -r requirements.txt
cp .env.example .env
```

Windows 用 `copy .env.example .env`。`.env` 里已填好这台机器的 SSH（用户 `root01`）和 Qwen 地址，一般不用改。

评分前先在服务器上把 vLLM 开起来：

```bash
python -m vllm.entrypoints.openai.api_server \
  --model "/home/root01/Public/privacy/llm_models/Qwen3.5-9B-GPTQ-4bit" \
  --served-model-name "Qwen3.5-9B-GPTQ-4bit" \
  --host 0.0.0.0 --port 8000
```

输入表只要两列：`ID`、`Text`。问答型 JSON 会自动只取「原始日记」和五个问答。重复 ID 会加 `#2`、`#3`。

当前评分口径用 `--prompt=v7`。

## 评 20 篇日记

只要深度分：

```bash
python -u -m scripts.score_file 20篇.xlsx 20篇_AI评分.xlsx --prompt=v7
```

如果这 20 篇带人工 final（表的最后 5 列是五步分数），要和人工比一致率：

```bash
python -u -m tests.agreement_check 20篇.xlsx --prompt=v7
python -m scripts.build_agreement_report 20篇.xlsx 20篇_AI评分_一致率.xlsx --prompt=v7
```

`agreement_check` 会在终端打印完全一致率、相差≤1、kappa；`build_agreement_report` 写出三页 Excel：评分prompt、AI深度分、一致率统计。

## 评全部日记

```bash
python -u -m scripts.score_file remaining_data.xlsx remaining_data_AI评分.xlsx --prompt=v7
```

输出三页：评分prompt、AI深度分、评分理由。失败的日记会自动重试。

把路径换成你的文件即可，例如：

```bash
python -u -m scripts.score_file "C:\Users\AustinHan\Downloads\remaining_data.xlsx" "C:\Users\AustinHan\Downloads\remaining_data_AI评分.xlsx" --prompt=v7
```
