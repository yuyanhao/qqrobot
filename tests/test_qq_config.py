"""Configuration must reach both QQ transports before resources are opened."""
import asyncio
from types import SimpleNamespace

import pytest
from botpy.http import Route

from qqmedia import QQMedia, QQMediaError, qq_api_base_url
from settings import Settings
from task_bot import TaskBot


@pytest.mark.parametrize("value", [
    "http://api.bot.qq.com", "https://", "https://api.bot.qq.com/v2",
    "https://user:password@api.bot.qq.com", "https://api.bot.qq.com?key=value",
    "https://api.bot.qq.com#fragment", "https://bad host.example",
    "https://api.bot.qq.com:bad", "https://api.bot.qq.com:0",
    "https://api.bot.qq.com\\proxy",
])
def test_qq_origin_rejects_unsupported_urls(value):
    with pytest.raises(ValueError, match="QQ_API_BASE_URL"):
        qq_api_base_url(value)


def test_qq_origin_defaults_and_normalizes(monkeypatch):
    monkeypatch.delenv("QQ_API_BASE_URL", raising=False)
    assert qq_api_base_url() == "https://api.bot.qq.com"
    assert qq_api_base_url(" HTTPS://QQ-API.EXAMPLE:8443/ ") == "https://qq-api.example:8443"


def test_taskbot_uses_same_origin_and_delivery_setting_for_both_transports(monkeypatch, tmp_path):
    monkeypatch.setenv("QQ_APPID", "fake-app")
    monkeypatch.setenv("QQ_SECRET", "fake-secret")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fake-key")
    monkeypatch.setenv("QQ_API_BASE_URL", "https://qq-api.example:8443/")
    monkeypatch.setenv("ACTIVE_DELIVERY", "false")
    monkeypatch.setattr(Route, "DOMAIN", Route.DOMAIN)
    monkeypatch.setattr("botpy.Client.__init__", lambda self, **kwargs: None)
    monkeypatch.setattr("task_bot.DeepSeek", lambda *args, **kwargs: object())
    monkeypatch.setattr("task_bot.TaskStore", lambda directory: SimpleNamespace(
        recover=lambda: None, cleanup=lambda ttl: None))
    bot = TaskBot(None, settings=Settings(data_dir=tmp_path, active_delivery=True))
    assert bot.media.base == "https://qq-api.example:8443"
    assert Route("GET", "/gateway").url == bot.media.base + "/gateway"
    assert bot.media.active_delivery is True


def test_invalid_origin_fails_before_sdk_or_store_construction(monkeypatch):
    monkeypatch.setenv("QQ_APPID", "fake-app")
    monkeypatch.setenv("QQ_SECRET", "fake-secret")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fake-key")
    monkeypatch.setenv("QQ_API_BASE_URL", "https://qq-api.example/unsupported-prefix")
    opened = []
    monkeypatch.setattr("botpy.Client.__init__", lambda self, **kwargs: opened.append("sdk"))
    monkeypatch.setattr("task_bot.TaskStore", lambda *args: opened.append("store"))
    with pytest.raises(ValueError, match="QQ_API_BASE_URL"):
        TaskBot(None)
    assert not opened


def test_direct_media_normalizes_boolean_and_keeps_the_startup_setting(monkeypatch):
    monkeypatch.setenv("ACTIVE_DELIVERY", " true ")
    media = QQMedia("fake-app", "fake-secret")
    assert media.active_delivery is True
    monkeypatch.setenv("ACTIVE_DELIVERY", "false")
    calls = []

    async def request(path, payload):
        calls.append((path, payload))
        return {}

    media._request = request
    asyncio.run(media.send_text("c2c", "user", "answer", None, None))
    assert len(calls) == 1 and "msg_id" not in calls[0][1]


def test_explicit_disabled_delivery_blocks_active_send(monkeypatch):
    monkeypatch.setenv("ACTIVE_DELIVERY", "true")
    media = QQMedia("fake-app", "fake-secret", active_delivery=False)
    with pytest.raises(QQMediaError, match="主动消息未启用"):
        asyncio.run(media.send_text("c2c", "user", "answer", None, None))
