"""Provider selection stays configuration-only and DeepSeek remains compatible."""
import asyncio
from types import SimpleNamespace

import pytest

from ai_fixtures import model_config
from qqrobot.providers import (DEFAULT_SEARCH_ENDPOINT, load_model_config,
                               search_credentials)
from qqrobot.solving.ai import ModelClient

CLEAR = (
    "LLM_PROVIDER", "LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL", "LLM_VISION_MODEL",
    "LLM_REASONING_EFFORT", "LLM_THINKING", "LLM_JSON_MODE",
    "DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL", "DEEPSEEK_MODEL", "VISION_MODEL",
    "DEEPSEEK_REASONING_EFFORT", "SEARCH_API_KEY", "SEARCH_ENDPOINT", "DEEPSEEK_SEARCH_ENDPOINT",
)


def clean(monkeypatch):
    for name in CLEAR:
        monkeypatch.delenv(name, raising=False)


def test_existing_deepseek_env_keeps_working_without_new_variables(monkeypatch):
    clean(monkeypatch)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "legacy-key")
    monkeypatch.setenv("DEEPSEEK_MODEL", "deepseek-v4-pro")
    monkeypatch.setenv("VISION_MODEL", "deepseek-flash")
    config = load_model_config()
    assert config.is_deepseek
    assert config.api_key == "legacy-key"
    assert config.base_url == "https://api.deepseek.com"
    assert config.model == "deepseek-v4-pro"
    assert config.vision_model == "deepseek-flash"
    assert config.thinking == "deepseek" and config.reasoning_effort == "high"
    assert search_credentials() == ("legacy-key", DEFAULT_SEARCH_ENDPOINT)


def test_openai_compatible_provider_uses_llm_variables(monkeypatch):
    clean(monkeypatch)
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("LLM_API_KEY", "openai-key")
    monkeypatch.setenv("LLM_MODEL", "gpt-test")
    config = load_model_config()
    assert config.provider == "openai"
    assert config.base_url == "https://api.openai.com/v1"
    assert config.model == config.vision_model == "gpt-test"
    assert config.thinking == "openai"
    assert search_credentials() == ("", DEFAULT_SEARCH_ENDPOINT)


def test_provider_does_not_reuse_deepseek_key(monkeypatch):
    clean(monkeypatch)
    monkeypatch.setenv("LLM_PROVIDER", "openrouter")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "legacy-key")
    monkeypatch.setenv("LLM_MODEL", "vendor/model")
    with pytest.raises(ValueError, match="LLM_API_KEY"):
        load_model_config()


def test_unknown_provider_and_missing_model_are_actionable(monkeypatch):
    clean(monkeypatch)
    monkeypatch.setenv("LLM_PROVIDER", "no-such-provider")
    with pytest.raises(ValueError, match="LLM_PROVIDER"):
        load_model_config()
    monkeypatch.setenv("LLM_PROVIDER", "custom")
    monkeypatch.setenv("LLM_API_KEY", "key")
    with pytest.raises(ValueError, match="LLM_BASE_URL"):
        load_model_config()
    monkeypatch.setenv("LLM_BASE_URL", "https://models.example/v1")
    with pytest.raises(ValueError, match="LLM_MODEL"):
        load_model_config()


def test_local_provider_allows_loopback_http_without_a_key(monkeypatch):
    clean(monkeypatch)
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv("LLM_MODEL", "qwen2.5:7b")
    config = load_model_config()
    assert config.api_key == "not-needed"
    assert config.base_url == "http://127.0.0.1:11434/v1"
    assert config.json_mode is False


def test_non_loopback_http_base_url_is_rejected(monkeypatch):
    clean(monkeypatch)
    monkeypatch.setenv("LLM_PROVIDER", "custom")
    monkeypatch.setenv("LLM_API_KEY", "key")
    monkeypatch.setenv("LLM_MODEL", "model")
    monkeypatch.setenv("LLM_BASE_URL", "http://models.example/v1")
    with pytest.raises(ValueError, match="LLM_BASE_URL"):
        load_model_config()


@pytest.mark.parametrize("name,value", [
    ("DEEPSEEK_REASONING_EFFORT", "medium"),
    ("LLM_REASONING_EFFORT", "extreme"),
    ("LLM_THINKING", "always"),
    ("LLM_JSON_MODE", "sometimes"),
])
def test_bad_model_options_name_the_offending_variable(monkeypatch, name, value):
    clean(monkeypatch)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "key")
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError, match=name):
        load_model_config()


def test_search_can_be_configured_independently_for_other_providers(monkeypatch):
    clean(monkeypatch)
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("LLM_API_KEY", "openai-key")
    monkeypatch.setenv("LLM_MODEL", "gpt-test")
    monkeypatch.setenv("SEARCH_API_KEY", "search-key")
    monkeypatch.setenv("SEARCH_ENDPOINT", "https://search.example/v1/search")
    assert search_credentials() == ("search-key", "https://search.example/v1/search")


def test_request_parameters_follow_provider_protocol():
    messages = [{"role": "user", "content": "hi"}]
    deepseek = ModelClient(model_config())
    openai = ModelClient(model_config(provider="openai", label="OpenAI", thinking="openai"))
    plain = ModelClient(model_config(provider="custom", label="Custom", thinking="none", json_mode=False))
    try:
        deep = deepseek._request_kwargs(messages, 1000, "enabled", False, None)
        assert deep["extra_body"] == {"thinking": {"type": "enabled"}}
        assert deep["reasoning_effort"] == "high"
        assert deep["response_format"] == {"type": "json_object"}
        hosted = openai._request_kwargs(messages, 1000, "enabled", False, None)
        assert "extra_body" not in hosted
        assert hosted["reasoning_effort"] == "high"
        minimal = plain._request_kwargs(messages, 1000, "enabled", False, None)
        assert "extra_body" not in minimal and "reasoning_effort" not in minimal
        assert "response_format" not in minimal
    finally:
        asyncio.run(deepseek.close())
        asyncio.run(openai.close())
        asyncio.run(plain.close())


def test_client_retries_once_without_unsupported_extension():
    client = ModelClient(model_config())
    calls = []

    class BadRequest(Exception):
        status_code = 400

    async def create(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise BadRequest("response_format is unsupported")
        return "ok"

    client.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)),
                                    close=lambda: asyncio.sleep(0))
    try:
        result = asyncio.run(client._create({"model": "m", "messages": [],
                                             "max_tokens": 10,
                                             "response_format": {"type": "json_object"}}))
        assert result == "ok"
        assert "response_format" not in calls[1]
        assert calls[1]["max_tokens"] == 10
    finally:
        asyncio.run(client.close())
