"""Shared model client construction for offline tests."""
from qqrobot.providers import ModelConfig
from qqrobot.solving.ai import ModelClient


def model_config(**overrides) -> ModelConfig:
    values = dict(provider="deepseek", label="DeepSeek", api_key="fake-key",
                  base_url="https://api.deepseek.com", model="deepseek-flash",
                  vision_model="deepseek-flash", reasoning_effort="high",
                  thinking="deepseek", json_mode=True)
    values.update(overrides)
    return ModelConfig(**values)


def model_client(**overrides) -> ModelClient:
    return ModelClient(model_config(**overrides))
