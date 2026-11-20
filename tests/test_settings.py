"""Configuration errors must be caught before accepting paid model tasks."""
import asyncio
import os
from pathlib import Path
import subprocess
import sys

import pytest

from ai_fixtures import model_client
from qqrobot.settings import Settings, validate_environment


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


def test_pdf_and_book_resource_limits_are_validated(monkeypatch):
    for name in ("PDF_INPUT_MAX_PAGES", "PDF_TASK_MAX_PAGES",
                 "BOOK_MAX_PDF_MB", "BOOK_LIBRARY_MAX_MB"):
        monkeypatch.setenv(name, "0")
        with pytest.raises(ValueError, match=name):
            validate_environment(require_credentials=False)
        monkeypatch.delenv(name)


def test_pdf_page_limits_cannot_be_configured_above_five(monkeypatch):
    for name in ("PDF_INPUT_MAX_PAGES", "PDF_TASK_MAX_PAGES"):
        monkeypatch.setenv(name, "6")
        with pytest.raises(ValueError, match=name):
            validate_environment(require_credentials=False)
        monkeypatch.delenv(name)


def test_unverified_official_vision_model_warns_without_blocking(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")
    monkeypatch.setenv("VISION_MODEL", "deepseek-v4-pro")
    warnings = validate_environment(require_credentials=False)
    assert any("VISION_MODEL" in warning and "支持图片" in warning for warning in warnings)


def test_effort_normalizes_before_selecting_token_budget(monkeypatch):
    monkeypatch.delenv("SOLVE_MAX_TOKENS", raising=False)
    monkeypatch.delenv("VERIFY_MAX_TOKENS", raising=False)
    client = model_client(reasoning_effort=" MAX ")
    try:
        assert client.reasoning_effort == "max" and client.solve_max_tokens == 131072
    finally:
        asyncio.run(client.close())


def test_cli_reports_invalid_configuration_without_network(monkeypatch):
    project = Path(__file__).resolve().parents[1]
    environment = {**os.environ, "VERIFY_SOLUTION": "synthetic-invalid-flag"}
    result = subprocess.run([sys.executable, "-X", "utf8", "-m", "qqrobot.settings", "--allow-missing-credentials"],
                            cwd=project, env=environment, capture_output=True, timeout=10)
    assert result.returncode != 0
    output = (result.stdout + result.stderr).decode("utf-8")
    assert "VERIFY_SOLUTION" in output and "synthetic-invalid-flag" not in output
