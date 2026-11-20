"""Model provider presets for any OpenAI-compatible chat completions API.

The bot talks to one HTTP protocol: the OpenAI-compatible
``/chat/completions`` endpoint.  Most hosted providers and local runtimes
expose it, so switching models only needs a provider name, a base URL, a key
and a model id instead of a code change.  DeepSeek stays the default so an
existing ``.env`` that only contains ``DEEPSEEK_*`` keeps working unchanged.
"""
from __future__ import annotations

from dataclasses import dataclass
import os
from urllib.parse import urlparse

DEFAULT_PROVIDER = "deepseek"
DEFAULT_SEARCH_ENDPOINT = "https://api.deepseek.com/v1/tools/search"

# Provider presets only carry endpoints and protocol capabilities; model ids
# are supplied by the deployment because vendors rename and retire them often.
@dataclass(frozen=True)
class ProviderPreset:
    key: str
    label: str
    base_url: str
    default_model: str = ""
    default_vision_model: str = ""
    thinking: str = "none"
    json_mode: bool = True
    api_key_required: bool = True


PRESETS: dict[str, ProviderPreset] = {
    "deepseek": ProviderPreset(
        "deepseek", "DeepSeek", "https://api.deepseek.com",
        default_model="deepseek-flash", default_vision_model="deepseek-flash", thinking="deepseek"),
    "openai": ProviderPreset(
        "openai", "OpenAI", "https://api.openai.com/v1", thinking="openai"),
    "openrouter": ProviderPreset(
        "openrouter", "OpenRouter", "https://openrouter.ai/api/v1"),
    "anthropic": ProviderPreset(
        "anthropic", "Anthropic（OpenAI 兼容接口）", "https://api.anthropic.com/v1"),
    "gemini": ProviderPreset(
        "gemini", "Google Gemini（OpenAI 兼容接口）",
        "https://generativelanguage.googleapis.com/v1beta/openai"),
    "qwen": ProviderPreset(
        "qwen", "阿里云百炼 / 通义千问", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
    "moonshot": ProviderPreset(
        "moonshot", "Moonshot Kimi", "https://api.moonshot.cn/v1"),
    "zhipu": ProviderPreset(
        "zhipu", "智谱 GLM", "https://open.bigmodel.cn/api/paas/v4"),
    "siliconflow": ProviderPreset(
        "siliconflow", "SiliconFlow", "https://api.siliconflow.cn/v1"),
    "ollama": ProviderPreset(
        "ollama", "Ollama（本地）", "http://127.0.0.1:11434/v1",
        json_mode=False, api_key_required=False),
    "lmstudio": ProviderPreset(
        "lmstudio", "LM Studio（本地）", "http://127.0.0.1:1234/v1",
        json_mode=False, api_key_required=False),
    "custom": ProviderPreset(
        "custom", "自定义 OpenAI 兼容接口", ""),
}

ALIASES = {
    "google": "gemini",
    "dashscope": "qwen",
    "aliyun": "qwen",
    "kimi": "moonshot",
    "glm": "zhipu",
}

LEGACY_NAMES = ("DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL", "DEEPSEEK_MODEL",
                "DEEPSEEK_REASONING_EFFORT", "VISION_MODEL")


@dataclass(frozen=True)
class ModelConfig:
    provider: str
    label: str
    api_key: str
    base_url: str
    model: str
    vision_model: str
    reasoning_effort: str = "high"
    thinking: str = "none"
    json_mode: bool = True

    @property
    def is_deepseek(self) -> bool:
        return self.provider == "deepseek"


def provider_key() -> str:
    raw = (os.getenv("LLM_PROVIDER", "") or DEFAULT_PROVIDER).strip().lower()
    return ALIASES.get(raw, raw)


def preset_for(name: str) -> ProviderPreset:
    try:
        return PRESETS[name]
    except KeyError:
        options = "、".join(PRESETS)
        raise ValueError(f"LLM_PROVIDER 只能是 {options}（当前为 {name!r}）") from None


def _first(*names: str) -> tuple[str, str]:
    for name in names:
        value = os.getenv(name, "").strip()
        if value:
            return value, name
    return "", names[0] if names else ""


def _boolean(name: str, default: bool) -> bool:
    value = os.getenv(name, "").strip().lower()
    if not value:
        return default
    if value in ("true", "1", "yes", "on"):
        return True
    if value in ("false", "0", "no", "off"):
        return False
    raise ValueError(f"{name} 必须为 true 或 false（也支持 1/0、yes/no、on/off）")


def _validate_url(value: str, name: str) -> str:
    parsed = urlparse(value)
    local = parsed.hostname in ("localhost", "127.0.0.1", "::1")
    scheme_ok = parsed.scheme == "https" or (local and parsed.scheme == "http")
    valid = (scheme_ok and parsed.hostname
             and parsed.username is None and parsed.password is None
             and "?" not in value and "#" not in value
             and not any(char.isspace() or ord(char) < 32 for char in value)
             and (parsed.port is None or 1 <= parsed.port <= 65535))
    if not valid:
        raise ValueError(f"{name} 必须为有效的 HTTPS 地址（本机服务可用 http://127.0.0.1）")
    return value


def load_model_config(*, require_credentials: bool = True) -> ModelConfig:
    """Read the model configuration, keeping DeepSeek variables as fallback."""
    name = provider_key()
    preset = preset_for(name)

    api_key, _ = _first(*(["LLM_API_KEY"] + (["DEEPSEEK_API_KEY"] if name == "deepseek" else [])))
    base_url, base_source = _first("LLM_BASE_URL", *(["DEEPSEEK_BASE_URL"] if name == "deepseek" else []))
    if not base_url:
        base_url, base_source = preset.base_url, "LLM_BASE_URL"
    if not base_url:
        raise ValueError("LLM_BASE_URL 不能为空：custom 供应商必须填写接口地址")
    _validate_url(base_url, base_source)

    model, _ = _first("LLM_MODEL", *(["DEEPSEEK_MODEL"] if name == "deepseek" else []))
    model = model or preset.default_model
    if not model:
        raise ValueError("LLM_MODEL 不能为空：请填写供应商提供的模型名")

    vision, _ = _first("LLM_VISION_MODEL",
                       *(["VISION_MODEL"] if name == "deepseek" else []))
    vision = vision or preset.default_vision_model or model

    effort, effort_source = _first("LLM_REASONING_EFFORT",
                                   *(["DEEPSEEK_REASONING_EFFORT"] if name == "deepseek" else []))
    effort = (effort or "high").lower()
    if effort not in ("low", "high", "max"):
        raise ValueError(f"{(effort_source if effort != 'high' else 'LLM_REASONING_EFFORT')} "
                         "必须为 low、high 或 max")

    thinking = (os.getenv("LLM_THINKING", "").strip().lower() or preset.thinking)
    if thinking not in ("deepseek", "openai", "none"):
        raise ValueError("LLM_THINKING 必须为 deepseek、openai 或 none")

    json_mode = _boolean("LLM_JSON_MODE", preset.json_mode)

    if require_credentials and preset.api_key_required and not api_key:
        names = "LLM_API_KEY 或 DEEPSEEK_API_KEY" if name == "deepseek" else "LLM_API_KEY"
        raise ValueError(f"请先配置 .env：{names}")
    if not preset.api_key_required and not api_key:
        api_key = "not-needed"

    return ModelConfig(provider=name, label=preset.label, api_key=api_key,
                       base_url=base_url, model=model, vision_model=vision,
                       reasoning_effort=effort, thinking=thinking, json_mode=json_mode)


def search_credentials() -> tuple[str, str]:
    """Return (key, endpoint) for the optional DeepSeek Search API.

    DeepSeek deployments reuse the model key so an existing ``.env`` keeps
    working.  Other providers need an explicit ``SEARCH_API_KEY``; without it
    the solver simply skips web search.
    """
    key, _ = _first("SEARCH_API_KEY")
    if not key and provider_key() == "deepseek":
        key, _ = _first("LLM_API_KEY", "DEEPSEEK_API_KEY")
    endpoint, endpoint_source = _first("SEARCH_ENDPOINT", "DEEPSEEK_SEARCH_ENDPOINT")
    endpoint = endpoint or DEFAULT_SEARCH_ENDPOINT
    return key, _validate_url(endpoint, endpoint_source)


def legacy_overrides_ignored() -> list[str]:
    """Legacy variable names that the current provider will not read."""
    if provider_key() == "deepseek":
        return []
    return [name for name in LEGACY_NAMES if os.getenv(name, "").strip()]
