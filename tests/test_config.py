"""M0 验收：配置默认值、校验规则与错误提示。"""

from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import ConfigError, Settings
from app.main import checkpoint_path_for_database


def _settings(**overrides) -> Settings:
    return Settings(_env_file=None, **overrides)


def test_default_fetch_limit_is_ten():
    settings = _settings()
    assert settings.mail_fetch_limit == 10
    assert settings.poll_interval_seconds == 120
    assert settings.provider_order == ["deepseek", "openai"]
    assert settings.display_timezone == "Asia/Shanghai"


def test_missing_required_config_raises_clear_error():
    settings = _settings()
    with pytest.raises(ConfigError) as exc_info:
        settings.ensure_ready()
    message = str(exc_info.value)
    assert "MAIL_ADDRESS" in message
    assert "MAIL_AUTH_CODE" in message
    assert ".env.example" in message


def test_provider_without_key_is_reported():
    settings = _settings(
        mail_address="user@qq.com",
        mail_auth_code="auth-code",
        llm_provider_order="deepseek",
        deepseek_api_key="sk-test",
    )
    settings.ensure_ready()  # deepseek 有 key，通过

    broken = _settings(
        mail_address="user@qq.com",
        mail_auth_code="auth-code",
        llm_provider_order="deepseek,openai",
        deepseek_api_key="sk-test",
    )
    with pytest.raises(ConfigError, match="openai.*API Key"):
        broken.ensure_ready()


def test_poll_interval_must_be_at_least_sixty_seconds():
    with pytest.raises(ValidationError):
        _settings(poll_interval_seconds=30)


def test_provider_order_validation():
    with pytest.raises(ValidationError, match="不支持的 provider"):
        _settings(llm_provider_order="deepseek,claude")
    with pytest.raises(ValidationError, match="重复"):
        _settings(llm_provider_order="deepseek,deepseek")
    with pytest.raises(ValidationError, match="不能为空"):
        _settings(llm_provider_order=" , ")


def test_display_timezone_validation():
    with pytest.raises(ValidationError, match="DISPLAY_TIMEZONE 无效"):
        _settings(display_timezone="Not/AZone")


def test_checkpoint_path_is_scoped_to_business_database(tmp_path):
    mail_db = tmp_path / "mail.db"
    m9_db = tmp_path / "m9.db"

    assert checkpoint_path_for_database(mail_db) == tmp_path / "mail.checkpoint.db"
    assert checkpoint_path_for_database(m9_db) == tmp_path / "m9.checkpoint.db"
    assert checkpoint_path_for_database(mail_db) != checkpoint_path_for_database(m9_db)