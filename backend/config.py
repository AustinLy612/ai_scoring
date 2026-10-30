"""运行配置，从项目根目录的 .env 读取。"""

from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent
FRONTEND_DIR = PROJECT_ROOT / "frontend"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    # SSH 登录实验室机器，再访问其上的本地 Qwen 服务
    llm_ssh_host: str = "10.200.20.244"
    llm_ssh_port: int = 22
    llm_ssh_user: str = ""
    llm_ssh_password: str = ""

    # 远程机器上 Qwen 的 OpenAI 兼容接口。留空则自动探测常见端口。
    qwen_api_base: str = Field(
        default="",
        validation_alias=AliasChoices("QWEN_API_BASE", "QWEN_BASE_URL", "qwen_api_base"),
    )
    qwen_model: str = ""
    qwen_api_key: str = ""

    # 阶段二评分 prompt 版本，见 prompts.STAGE2_PROMPTS
    stage2_prompt_version: str = "v7"

    max_concurrency: int = 2
    max_retries: int = 3
    request_timeout: float = 300.0


@lru_cache
def get_settings() -> Settings:
    return Settings()
