"""通过 SSH 调用实验室机器上的本地 Qwen（OpenAI 兼容 /v1/chat/completions）。"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shlex
import threading
from typing import Any
from urllib.parse import urljoin

import paramiko

from .config import get_settings

logger = logging.getLogger(__name__)

_DISCOVER_PORTS = (8000, 8001, 8080, 8081, 8888, 9000, 9999, 11434, 5000, 7860, 6006, 18000, 30000)
_JSON_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)

_REMOTE_HTTP = r"""
import json, sys, urllib.error, urllib.request
payload = json.loads(sys.stdin.read())
headers = {"Accept": "application/json"}
if payload.get("content_type"):
    headers["Content-Type"] = payload["content_type"]
if payload.get("authorization"):
    headers["Authorization"] = payload["authorization"]
data = None if payload.get("body") is None else payload["body"].encode("utf-8")
req = urllib.request.Request(
    payload["url"], data=data, headers=headers,
    method="GET" if data is None else "POST",
)
try:
    with urllib.request.urlopen(req, timeout=payload.get("timeout", 300)) as resp:
        print(json.dumps({
            "ok": True,
            "status": getattr(resp, "status", 200),
            "body": resp.read().decode("utf-8", "replace"),
        }, ensure_ascii=False))
except urllib.error.HTTPError as exc:
    print(json.dumps({
        "ok": False, "status": exc.code,
        "body": exc.read().decode("utf-8", "replace"), "error": str(exc),
    }, ensure_ascii=False))
except Exception as exc:
    print(json.dumps({"ok": False, "status": 0, "body": "", "error": str(exc)}, ensure_ascii=False))
"""


class LLMError(RuntimeError):
    pass


class LLMClient:
    def __init__(self) -> None:
        settings = get_settings()
        if not settings.llm_ssh_host:
            raise LLMError("未配置 LLM_SSH_HOST。")
        if not settings.llm_ssh_user or not settings.llm_ssh_password:
            raise LLMError(
                "未配置 LLM_SSH_USER / LLM_SSH_PASSWORD，请复制 .env.example 为 .env 并填写 SSH 登录信息。"
            )
        self._settings = settings
        self._semaphore = asyncio.Semaphore(max(1, settings.max_concurrency))
        self._lock = threading.Lock()
        self._ssh: paramiko.SSHClient | None = None
        self._python = "python3"
        self._api_base = (settings.qwen_api_base or "").rstrip("/")
        self._model = "" if _looks_like_path(settings.qwen_model) else (settings.qwen_model or "").strip()
        self._connect()
        self._python = self._detect_python()
        if not self._api_base or not self._model:
            self._discover()
        logger.info("Qwen 接口 %s  模型 %s", self._api_base, self._model)

    async def complete_json(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        temperature: float = 0.0,
    ) -> dict[str, Any]:
        settings = self._settings
        last_error: Exception | None = None
        for attempt in range(1, settings.max_retries + 1):
            try:
                async with self._semaphore:
                    raw = await asyncio.to_thread(self._request, system_prompt, user_prompt, temperature)
                return _parse_json_object(raw)
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                logger.warning("LLM 调用失败（第 %s/%s 次）：%s", attempt, settings.max_retries, exc)
                if attempt < settings.max_retries:
                    await asyncio.sleep(min(2 ** attempt, 10))
        raise LLMError(f"LLM 调用在 {settings.max_retries} 次尝试后仍失败：{last_error}")

    def close(self) -> None:
        with self._lock:
            if self._ssh is not None:
                self._ssh.close()
                self._ssh = None

    def _connect(self) -> None:
        settings = self._settings
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(
            hostname=settings.llm_ssh_host,
            port=settings.llm_ssh_port,
            username=settings.llm_ssh_user,
            password=settings.llm_ssh_password,
            timeout=20,
            banner_timeout=20,
            auth_timeout=20,
            allow_agent=False,
            look_for_keys=False,
        )
        transport = client.get_transport()
        if transport is not None:
            transport.set_keepalive(30)
        self._ssh = client
        logger.info("已 SSH 登录 %s@%s", settings.llm_ssh_user, settings.llm_ssh_host)

    def _ensure_connected(self) -> None:
        with self._lock:
            transport = self._ssh.get_transport() if self._ssh is not None else None
            if transport is None or not transport.is_active():
                self._connect()

    def _exec(self, command: str, stdin_data: str | None = None) -> tuple[int, str, str]:
        self._ensure_connected()
        assert self._ssh is not None
        stdin, stdout, stderr = self._ssh.exec_command(command, timeout=self._settings.request_timeout)
        if stdin_data is not None:
            stdin.write(stdin_data)
            stdin.channel.shutdown_write()
        out = stdout.read().decode("utf-8", "replace")
        err = stderr.read().decode("utf-8", "replace")
        code = stdout.channel.recv_exit_status()
        return code, out, err

    def _detect_python(self) -> str:
        for exe in ("python3", "python"):
            code, out, _ = self._exec(f"{exe} -c \"import sys; print(sys.version_info[0])\"")
            if code == 0 and out.strip().startswith("3"):
                return exe
        raise LLMError("远程机器上未找到 Python 3，无法调用本地 Qwen 接口。")

    def _remote_http_call(self, url: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = {
            "url": url,
            "body": None if body is None else json.dumps(body, ensure_ascii=False),
            "content_type": None if body is None else "application/json",
            "authorization": f"Bearer {self._settings.qwen_api_key}" if self._settings.qwen_api_key else None,
            "timeout": int(self._settings.request_timeout),
        }
        code, out, err = self._exec(
            f"{self._python} -c {shlex.quote(_REMOTE_HTTP.strip())}",
            json.dumps(payload, ensure_ascii=False),
        )
        if code != 0:
            raise LLMError(f"远程 HTTP 辅助脚本失败（exit {code}）：{err or out}")
        raw = out.strip()
        try:
            result = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise LLMError(f"远程 HTTP 辅助脚本返回无法解析：{raw[:400]}") from exc
        if not result.get("ok"):
            raise LLMError(
                f"远程 {url} 调用失败（HTTP {result.get('status')}）："
                f"{result.get('error') or ''} {str(result.get('body') or '')[:300]}"
            )
        try:
            parsed = json.loads(result["body"]) if result.get("body") else {}
        except json.JSONDecodeError:
            parsed = {"raw": result.get("body")}
        return parsed if isinstance(parsed, dict) else {"raw": parsed}

    def _discover(self) -> None:
        errors: list[str] = []
        candidates = [self._api_base] if self._api_base else [f"http://127.0.0.1:{port}/v1" for port in _DISCOVER_PORTS]
        for base in candidates:
            models_url = base.rstrip("/") + "/models"
            try:
                data = self._remote_http_call(models_url)
            except LLMError as exc:
                errors.append(f"{models_url}: {exc}")
                continue
            model = self._pick_model(data)
            if model:
                self._api_base = base.rstrip("/")
                if not self._model:
                    self._model = model
                return
            errors.append(f"{models_url}: 未找到模型 {data}")
        raise LLMError(
            "SSH 已连通，但未在远程本机发现 Qwen 的 OpenAI 兼容接口。"
            "请确认服务已启动，或在 .env 中设置 QWEN_API_BASE（例如 http://127.0.0.1:8000/v1）和 QWEN_MODEL。\n"
            + "\n".join(errors[:8])
        )

    def _pick_model(self, data: dict[str, Any]) -> str:
        preferred = (self._model or "").strip()
        ids: list[str] = []
        for item in data.get("data") or []:
            if isinstance(item, dict) and item.get("id"):
                ids.append(str(item["id"]))
        for item in data.get("models") or []:
            if isinstance(item, dict):
                name = item.get("name") or item.get("model") or item.get("id")
                if name:
                    ids.append(str(name))
        if preferred:
            return preferred
        for name in ids:
            if "qwen" in name.lower():
                return name
        return ids[0] if ids else ""

    def _request(self, system_prompt: str, user_prompt: str, temperature: float) -> str:
        if not self._api_base or not self._model:
            self._discover()
        url = urljoin(self._api_base.rstrip("/") + "/", "chat/completions")
        payload = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": temperature,
            "stream": False,
        }
        data = self._remote_http_call(url, payload)
        try:
            content = data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"模型返回格式异常：{json.dumps(data, ensure_ascii=False)[:400]}") from exc
        if not str(content).strip():
            raise LLMError("模型返回了空内容")
        return str(content)


def _looks_like_path(name: str) -> bool:
    text = (name or "").strip()
    if not text:
        return False
    return text.endswith(".gguf") or any(ch in text for ch in r"/\:") or text[1:3] == ":\\"


def _parse_json_object(raw: str) -> dict[str, Any]:
    text = raw.strip()
    fence = _JSON_FENCE.search(text)
    if fence:
        text = fence.group(1).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            raise LLMError(f"无法从模型输出中解析 JSON：{raw[:300]}") from None
        data = json.loads(text[start : end + 1])
    if not isinstance(data, dict):
        raise LLMError(f"模型返回的 JSON 顶层不是对象：{raw[:300]}")
    return data


_client: LLMClient | None = None


def get_client() -> LLMClient:
    global _client
    if _client is None:
        _client = LLMClient()
    return _client
