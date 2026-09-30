"""运行配置：从 .env / 环境变量加载，并在启动前做整体校验。"""

from __future__ import annotations

from functools import lru_cache
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_ALLOWED_PROVIDERS = {"deepseek", "openai"}


class ConfigError(RuntimeError):
    """关键配置缺失或非法时抛出，报错信息面向使用者可读。"""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # QQ 邮箱
    mail_address: str = ""
    mail_auth_code: str = ""
    imap_host: str = "imap.qq.com"
    imap_port: int = Field(default=993, ge=1, le=65535)
    smtp_host: str = "smtp.qq.com"
    smtp_port: int = Field(default=465, ge=1, le=65535)

    # 拉取与轮询（约束：间隔不低于 60 秒，避免 QQ 邮箱风控）
    mail_fetch_limit: int = Field(default=10, ge=1, le=100)
    poll_interval_seconds: int = Field(default=120, ge=60)

    # LLM
    deepseek_api_key: str = ""
    openai_api_key: str = ""
    llm_provider_order: str = "deepseek,openai"
    deepseek_base_url: str = "https://api.deepseek.com"
    openai_base_url: str = "https://api.openai.com/v1"
    deepseek_model: str = "deepseek-chat"
    openai_model: str = "gpt-4o-mini"
    llm_timeout_seconds: int = Field(default=60, ge=1)
    router_llm_min_confidence: float = Field(default=0.7, gt=0.0, le=1.0)
    max_rewrite_rounds: int = Field(default=3, ge=0)
    max_email_body_chars: int = Field(default=8000, ge=100)

    # 应用
    database_path: str = "data/mail.db"
    host: str = "127.0.0.1"
    port: int = Field(default=8000, ge=1, le=65535)
    demo_mode: bool = False
    display_timezone: str = "Asia/Shanghai"

    @field_validator("display_timezone")
    @classmethod
    def _validate_display_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"DISPLAY_TIMEZONE 无效：{value}") from exc
        return value

    @field_validator("llm_provider_order")
    @classmethod
    def _validate_provider_order(cls, value: str) -> str:
        providers = [item.strip().lower() for item in value.split(",") if item.strip()]
        if not providers:
            raise ValueError("LLM_PROVIDER_ORDER 不能为空，示例：deepseek,openai")
        unknown = [item for item in providers if item not in _ALLOWED_PROVIDERS]
        if unknown:
            raise ValueError(
                f"不支持的 provider：{','.join(unknown)}，仅支持 deepseek,openai"
            )
        if len(providers) != len(set(providers)):
            raise ValueError("LLM_PROVIDER_ORDER 不能包含重复项")
        return ",".join(providers)

    @property
    def provider_order(self) -> list[str]:
        return self.llm_provider_order.split(",")

    def ensure_ready(self) -> None:
        """启动前聚合校验关键配置，一次性列出全部缺失项。"""
        if self.demo_mode:
            return
        problems: list[str] = []
        if not self.mail_address:
            problems.append("缺少 MAIL_ADDRESS（QQ 邮箱地址）")
        if not self.mail_auth_code:
            problems.append(
                "缺少 MAIL_AUTH_CODE（IMAP/SMTP 授权码，不是 QQ 登录密码）"
            )
        for provider in self.provider_order:
            key = self.deepseek_api_key if provider == "deepseek" else self.openai_api_key
            if not key:
                problems.append(
                    f"LLM_PROVIDER_ORDER 包含 {provider}，但未配置对应的 API Key；"
                    f"请补全该 Key，或将其从 LLM_PROVIDER_ORDER 中移除"
                )
        if problems:
            details = "\n- ".join(problems)
            raise ConfigError(
                f"配置校验失败：\n- {details}\n"
                "请复制 .env.example 为 .env，补全后重新启动。"
            )


@lru_cache
def get_settings() -> Settings:
    return Settings()
