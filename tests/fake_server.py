"""带假模型的本地服务，供界面验证使用（不调用真实模型）。

运行：python -m uvicorn tests.fake_server:app --port 8078
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend import llm_client  # noqa: E402
from backend.main import app  # noqa: E402,F401  - uvicorn 需要 app
from tests.test_smoke import FakeClient  # noqa: E402

llm_client._client = FakeClient()  # noqa: SLF001
