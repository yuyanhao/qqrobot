import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_delivery_modes import make_bot


def test_panel_uses_the_same_menu_as_the_guide():
    from qqrobot.panel import GROUP_COMMANDS

    guide = (Path(__file__).resolve().parents[1] / "docs" / "qq-command-guide.md").read_text(encoding="utf-8")
    listed = [line for line in guide.splitlines() if line.startswith("| /")]
    assert listed == [f"| {name} | {description} |" for name, description in GROUP_COMMANDS]


def test_panel_preview_has_canonical_names_and_fits_qq_limits():
    from qqrobot.panel import panel_payload

    payload = panel_payload()
    assert payload["scope"] == "group" and payload["target_type"] == "all"
    items = payload["panel"]["items"]
    names = {item["name"] for item in items}
    assert {"/帮助", "/查错", "/最近", "/题目", "/书库"} <= names
    assert 0 < len(names) == len(items) <= 20
    for item in items:
        assert item["name"].startswith("/") and item["type"] == "command"
        assert not item["only_admin"]
        assert sum(1 if c.isascii() else 2 for c in item["name"]) <= 14
        assert sum(1 if c.isascii() else 2 for c in item["desc"]) <= 30


class PanelAPI:
    def __init__(self, pages):
        self.pages = iter(pages)
        self.calls = []

    async def _request(self, path, payload=None, *, method="POST", params=None):
        self.calls.append((method, path, params, payload))
        if method == "GET":
            return next(self.pages)
        if method == "POST":
            return {"panel_id": "created"}
        return {"version": 2}


def test_create_panel_when_no_global_panel_exists():
    from qqrobot.panel import panel_payload, sync_group_panel

    api = PanelAPI([{"records": [], "is_end": True}])
    assert asyncio.run(sync_group_panel(api)) == ("已创建", "created")
    assert api.calls == [
        ("GET", "/v2/panels", {"scope": "group", "limit": 20}, None),
        ("POST", "/v2/panels", None, panel_payload()),
    ]


@pytest.mark.parametrize("same", [False, True])
def test_sync_finds_its_own_panel_across_pages_and_only_updates_changes(same):
    from qqrobot.panel import panel_payload, sync_group_panel

    panel = panel_payload()["panel"]
    old_panel = panel if same else {**panel, "items": panel["items"][:1]}
    api = PanelAPI([
        {"records": [{"panel_id": "specific", "scope": "group", "target_type": "specific",
                      "panel": {"remark": "operator"}}], "is_end": False, "next_cursor": "more"},
        {"records": [{"panel_id": "own", "scope": "group", "target_type": "all",
                      "panel": old_panel}], "is_end": True},
    ])
    assert asyncio.run(sync_group_panel(api)) == ("已是最新" if same else "已更新", "own")
    assert api.calls[1][2]["cursor"] == "more"
    assert api.calls[2:] == ([] if same else [("PUT", "/v2/panels/own", None, {"panel": panel})])


@pytest.mark.parametrize("response", [
    {"records": [{"panel_id": "foreign", "target_type": "all", "panel": {"remark": "owner"}}], "is_end": True},
    {"records": [], "is_end": False},
    {},
])
def test_sync_does_not_write_when_ownership_or_list_is_unclear(response):
    from qqrobot.panel import PanelError, sync_group_panel

    api = PanelAPI([response])
    with pytest.raises(PanelError):
        asyncio.run(sync_group_panel(api))
    assert all(method == "GET" for method, *_ in api.calls)


def test_panel_request_uses_the_existing_qq_transport_authentication():
    from qqrobot.panel import sync_group_panel
    from qqrobot.media.qqmedia import QQMedia

    calls = []

    class Reply:
        status = 200

        def __init__(self, result):
            self.content = self
            self.result = result

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def read(self, size):
            return json.dumps(self.result).encode()

    class Session:
        def request(self, method, url, *, json, params, headers, allow_redirects):
            assert headers == {"Authorization": "QQBot fake-token", "X-Union-Appid": "bot-id"}
            assert url == "https://api.bot.qq.com/v2/panels"
            assert not allow_redirects
            calls.append((method, params, json))
            return Reply({"records": [], "is_end": True} if method == "GET" else {"panel_id": "new"})

    async def check():
        media = QQMedia("bot-id", "test-secret", "https://api.bot.qq.com", active_delivery=False)

        async def session():
            return Session()

        async def token():
            return "fake-token"

        media._get_session, media._get_token = session, token
        assert await sync_group_panel(media) == ("已创建", "new")

    asyncio.run(check())
    assert calls[0] == ("GET", {"scope": "group", "limit": 20}, None)
    assert calls[1][0] == "POST" and calls[1][2]["scope"] == "group"


@pytest.mark.parametrize("command", ["/结果", "/PDF", "/进度", "/追问"])
def test_sending_a_panel_command_without_arguments_shows_its_usage(tmp_path, command):
    bot = make_bot(tmp_path)
    try:
        message = SimpleNamespace(id="panel-command", content=f"<@!12345> {command}", attachments=[])
        asyncio.run(bot._handle(message, "group", "g", "group:g:u"))
        assert bot.media.sent[0][1].startswith(f"用法：{command} 题号")
        assert bot.queue.empty()
    finally:
        bot.store.close()
