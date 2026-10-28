"""Configuration errors must be caught before accepting paid model tasks."""
import asyncio
import os
from pathlib import Path
import subprocess
import sys

import pytest

from ai import DeepSeek
from settings import Settings, validate_environment


def test_settings_read_current_environment_and_preserve_explicit_overrides(monkeypatch):
    monkeypatch.setenv("VERIFY_SOLUTION", "true")
    assert Settings().verify is True
    monkeypatch.setenv("VERIFY_SOLUTION", " off ")
    assert Settings().verify is False
    assert Settings(verify=True).verify is True


@pytest.mark.parametrize("name,value", [
    ("VERIFY_SOLUTION", "ture"), ("SEARCH_ENABLED", ""),
    ("ACTIVE_DELIVERY", "enabled"), ("TASK_TIMEOUT", "abc"),
    ("MAX_QUEUE", "0"), ("RESULT_MAX_MB", "999999"),
    ("DEEPSEEK_REASONING_EFFORT", "medium"),
    ("SOLVE_MAX_TOKENS", "4000"), ("DATA_DIR", ""),
    ("DEEPSEEK_BASE_URL", "http://api.deepseek.com"),
    ("QQ_API_BASE_URL", "https://qq-api.example\\proxy"),
])
def test_bad_environment_fails_with_name_and_without_value(monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError, match=name):
        validate_environment(require_credentials=False)


def test_credentials_are_required_without_echoing_secrets(monkeypatch):
    monkeypatch.setenv("QQ_APPID", "")
    monkeypatch.setenv("QQ_SECRET", "synthetic-private-secret")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "")
    with pytest.raises(ValueError) as caught:
        validate_environment()
    assert "QQ_APPID" in str(caught.value) and "DEEPSEEK_API_KEY" in str(caught.value)
    assert "synthetic-private-secret" not in str(caught.value)


def test_budget_and_obsolete_settings_produce_actionable_notices(monkeypatch):
    monkeypatch.setenv("TASK_TIMEOUT", "120")
    monkeypatch.setenv("PDF_INPUT_PAGES", "4")
    monkeypatch.setenv("BOCHA_API_KEY", "synthetic-private-secret")
    warnings = "\n".join(validate_environment(require_credentials=False))
    assert "TASK_TIMEOUT" in warnings and "PDF_INPUT_PAGES" in warnings
    assert "BOCHA_API_KEY" in warnings and "synthetic-private-secret" not in warnings


def test_official_vision_model_must_support_images(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")
    monkeypatch.setenv("VISION_MODEL", "deepseek-v4-pro")
    with pytest.raises(ValueError, match="VISION_MODEL"):
        validate_environment(require_credentials=False)


def test_effort_normalizes_before_selecting_token_budget(monkeypatch):
    monkeypatch.delenv("SOLVE_MAX_TOKENS", raising=False)
    monkeypatch.delenv("VERIFY_MAX_TOKENS", raising=False)
    client = DeepSeek("fake-key", "https://api.deepseek.com", "deepseek-flash", " MAX ")
    try:
        assert client.reasoning_effort == "max" and client.solve_max_tokens == 131072
    finally:
        asyncio.run(client.close())


def test_cli_reports_invalid_configuration_without_network(monkeypatch):
    project = Path(__file__).resolve().parents[1]
    environment = {**os.environ, "VERIFY_SOLUTION": "synthetic-invalid-flag"}
    result = subprocess.run([sys.executable, "-X", "utf8", "settings.py", "--allow-missing-credentials"],
                            cwd=project, env=environment, capture_output=True, timeout=10)
    assert result.returncode != 0
    output = (result.stdout + result.stderr).decode("utf-8")
    assert "VERIFY_SOLUTION" in output and "synthetic-invalid-flag" not in output
