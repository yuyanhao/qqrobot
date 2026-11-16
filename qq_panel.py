"""Preview or sync the native QQ group command panel."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from urllib.parse import quote

from qqmedia import QQMedia, QQMediaError


PANEL_PATH = "/v2/panels"
PANEL_REMARK = "qqrobot:math-study:group"
GROUP_COMMANDS = (
    ("/帮助", "查看全部指令和用法"),
    ("/查错", "检查推导是否正确和完整"),
    ("/最近", "选择自己的最近三个题号"),
    ("/题目", "查看自己最近提交的题目"),
    ("/进度", "输入题号查询处理进度"),
    ("/结果", "输入题号领取解题结果"),
    ("/PDF", "输入题号领取完整PDF"),
    ("/题干", "输入题号查看题干和疑点"),
    ("/确认题干", "确认题干或填写更正"),
    ("/追问", "输入题号和想追问的问题"),
    ("/收集", "分批提交同一道题的材料"),
    ("/补图", "为收集中的题目追加材料"),
    ("/开始", "收齐材料后开始解题"),
    ("/收藏", "查看收藏或输入题号保存"),
    ("/复习", "选收藏并领取题目和答案"),
    ("/错题本", "把本会话的收藏导出为PDF"),
    ("/书库", "查看共享的PDF书籍"),
    ("/引用", "按书籍编号和页码解题"),
)


class PanelError(ValueError):
    pass


def panel_payload():
    return {
        "scope": "group",
        "target_type": "all",
        "panel": {
            "remark": PANEL_REMARK,
            "items": [
                {"type": "command", "name": name, "desc": description, "only_admin": False}
                for name, description in GROUP_COMMANDS
            ],
        },
    }


async def sync_group_panel(media):
    payload = panel_payload()
    desired = payload["panel"]
    params = {"scope": "group", "limit": 20}
    cursors = set()
    existing = None
    for _ in range(20):
        page = await media._request(PANEL_PATH, method="GET", params=dict(params))
        records = page.get("records")
        if records is None and page.get("is_end") is True and not page.get("next_cursor"):
            records = []
        if not isinstance(records, list):
            raise PanelError("QQ 未返回有效的面板列表")
        for record in records:
            if not isinstance(record, dict) or record.get("scope", "group") != "group":
                raise PanelError("QQ 返回了无法识别的群面板")
            if record.get("target_type") == "specific":
                continue
            panel = record.get("panel")
            if (record.get("target_type") != "all" or not isinstance(panel, dict)
                    or panel.get("remark") != PANEL_REMARK):
                raise PanelError("已存在其他全局群指令面板，请在 QQ 管理端核对其配置")
            if existing is not None:
                raise PanelError("有多个同标记群指令面板，请在 QQ 管理端核对")
            existing = record
        if page.get("is_end") is True or ("is_end" not in page and not page.get("next_cursor")):
            break
        cursor = page.get("next_cursor")
        if not isinstance(cursor, str) or not cursor or cursor in cursors:
            raise PanelError("QQ群面板列表分页未完成")
        cursors.add(cursor)
        params["cursor"] = cursor
    else:
        raise PanelError("QQ群面板列表分页未完成")

    if existing is None:
        created = await media._request(PANEL_PATH, payload)
        panel_id = created.get("panel_id")
        if not isinstance(panel_id, str) or not panel_id:
            raise PanelError("QQ 未返回新面板的编号，请核对线上状态")
        return "已创建", panel_id

    panel_id = existing.get("panel_id")
    if not isinstance(panel_id, str) or not panel_id or panel_id in (".", ".."):
        raise PanelError("QQ 未返回可用的面板编号")
    current_items = existing["panel"].get("items")
    keys = ("type", "name", "desc", "only_admin")
    if isinstance(current_items, list):
        current_items = [
            {key: item.get(key, False if key == "only_admin" else "") for key in keys}
            if isinstance(item, dict) else item
            for item in current_items
        ]
    if current_items == desired["items"]:
        return "已是最新", panel_id
    await media._request(f"{PANEL_PATH}/{quote(panel_id, safe='')}", {"panel": desired}, method="PUT")
    return "已更新", panel_id


async def _apply(appid, secret):
    media = QQMedia(appid, secret, active_delivery=False)
    try:
        async with asyncio.timeout(30):
            return await sync_group_panel(media)
    finally:
        await media.close()


def main():
    parser = argparse.ArgumentParser(description="预览或同步 QQ 群聊原生指令面板")
    parser.add_argument("--apply", action="store_true", help="使用当前机器人的 QQ 凭据同步到开放平台")
    args = parser.parse_args()
    if not args.apply:
        print(json.dumps(panel_payload(), ensure_ascii=False, indent=2))
        return
    appid, secret = (os.getenv(key, "").strip() for key in ("QQ_APPID", "QQ_SECRET"))
    if not appid or not secret:
        parser.exit(1, "面板同步未完成：请在机器人部署目录的 .env 或服务环境中配置 QQ_APPID 和 QQ_SECRET。\n")
    try:
        action, panel_id = asyncio.run(_apply(appid, secret))
    except (QQMediaError, ValueError, TimeoutError) as exc:
        raise SystemExit(f"面板同步未完成：{exc or '请求超时'}") from None
    print(f"{action} QQ 群聊指令面板（{panel_id}）。请在手机 QQ 的机器人所在群中 @ 机器人验证。")


if __name__ == "__main__":
    main()
