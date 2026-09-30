"""测试公共夹具：隔离环境变量，避免本机 .env 影响用例。"""

import pytest


_ENV_KEYS = [
    "MAIL_ADDRESS",
    "MAIL_AUTH_CODE",
    "IMAP_HOST",
    "IMAP_PORT",
    "SMTP_HOST",
    "SMTP_PORT",
    "MAIL_FETCH_LIMIT",
    "POLL_INTERVAL_SECONDS",
    "DEEPSEEK_API_KEY",
    "OPENAI_API_KEY",
    "LLM_PROVIDER_ORDER",
    "DEEPSEEK_BASE_URL",
    "OPENAI_BASE_URL",
    "DEEPSEEK_MODEL",
    "OPENAI_MODEL",
    "LLM_TIMEOUT_SECONDS",
    "ROUTER_LLM_MIN_CONFIDENCE",
    "MAX_REWRITE_ROUNDS",
    "MAX_EMAIL_BODY_CHARS",
    "DATABASE_PATH",
    "HOST",
    "PORT",
    "DEMO_MODE",
]


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch):
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
