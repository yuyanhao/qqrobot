"""Current QQ v2 upload/send APIs, including local PDF chunk uploads."""
from __future__ import annotations
import asyncio
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import time
from urllib.parse import quote, urlparse

import aiohttp
from file_tools import PublicResolver, _check_public_url
from settings import flag


def qq_api_base_url(value=None):
    """Use one HTTPS origin for SDK routes and media requests."""
    raw = (os.getenv("QQ_API_BASE_URL", "https://api.bot.qq.com")
           if value is None else value).strip()
    try:
        parsed = urlparse(raw)
        valid = (parsed.scheme == "https" and parsed.hostname
                 and re.fullmatch(r"[A-Za-z0-9.-]+", parsed.hostname)
                 and parsed.username is None and parsed.password is None
                 and parsed.path in ("", "/") and not parsed.params
                 and "?" not in raw and "#" not in raw
                 and not any(char.isspace() or ord(char) < 32 for char in raw)
                 and (parsed.port is None or 1 <= parsed.port <= 65535))
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("QQ_API_BASE_URL 必须是 HTTPS 域名地址，可含端口，不能含路径、账号或查询参数")
    return "https://" + parsed.netloc.lower()


class QQMediaError(RuntimeError):
    def __init__(self, message, category="delivery", code=None, status=None):
        super().__init__(message)
        self.category, self.code, self.status = category, code, status


class QQMedia:
    def __init__(self, appid, secret, api_base_url=None, *, active_delivery=None):
        self.appid, self.secret = appid, secret
        self.base = qq_api_base_url(api_base_url)
        self.active_delivery = flag("ACTIVE_DELIVERY", False) if active_delivery is None else active_delivery
        self._token, self._expires_at, self._session = None, 0, None
        self._token_lock = asyncio.Lock()
        self._send_lock = asyncio.Lock()
        self._last_send = 0.0

    async def _get_session(self):
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=120),
                                                  connector=aiohttp.TCPConnector(resolver=PublicResolver()))
        return self._session

    async def close(self):
        if self._session and not self._session.closed: await self._session.close()

    async def _get_token(self):
        async with self._token_lock:
            if self._token and time.time() < self._expires_at - 60: return self._token
            session = await self._get_session()
            try:
                async with session.post("https://bots.qq.com/app/getAppAccessToken",
                                        json={"appId": self.appid, "clientSecret": self.secret}) as resp:
                    data = await resp.json(content_type=None)
                    if resp.status >= 400 or not data.get("access_token"):
                        raise QQMediaError("QQ access_token 获取失败", "auth", status=resp.status)
            except QQMediaError: raise
            except Exception as exc: raise QQMediaError("QQ 鉴权请求失败", "network") from exc
            self._token = data["access_token"]
            self._expires_at = time.time() + int(data.get("expires_in", 7200))
            return self._token

    @staticmethod
    def _path(kind, target):
        if kind not in ("group", "c2c"): raise QQMediaError("未知会话类型")
        return "/v2/" + ("groups/" if kind == "group" else "users/") + quote(target, safe="")

    async def _request(self, path, payload):
        for attempt in range(2):
            token = await self._get_token()
            session = await self._get_session()
            try:
                async with session.post(self.base + path, json=payload,
                                        headers={"Authorization": f"QQBot {token}", "X-Union-Appid": self.appid},
                                        allow_redirects=False) as resp:
                    raw = await resp.content.read(1024 * 1024)
                    try: data = json.loads(raw)
                    except (ValueError, UnicodeError): data = {}
                    if resp.status == 401 and attempt == 0:
                        async with self._token_lock:
                            if self._token == token: self._token = None
                        continue
                    code = data.get("code") if isinstance(data, dict) else None
                    if resp.status >= 300 or code not in (None, 0, "0"):
                        category = "permission" if resp.status == 403 or str(code) == "40034105" else "rate_limit" if resp.status == 429 or str(code) == "40034100" else "delivery"
                        raise QQMediaError(f"QQ 接口拒绝请求（HTTP {resp.status}，code={code}）", category, code, resp.status)
                    if not isinstance(data, dict): raise QQMediaError("QQ 返回格式异常", "protocol")
                    return data
            except QQMediaError: raise
            except (aiohttp.ClientError, TimeoutError) as exc:
                raise QQMediaError("QQ 网络请求失败；未自动重复发送消息", "network") from exc
        raise QQMediaError("QQ 鉴权失败", "auth")

    @staticmethod
    def _info(data):
        if not data.get("file_info"): raise QQMediaError("上传响应缺少 file_info", "protocol")
        return data["file_info"]

    async def upload_image(self, kind, target, png, filename):
        if len(png) > 4_500_000: raise QQMediaError("预览图片过大", "upload")
        return self._info(await self._request(self._path(kind, target) + "/files", {
            "file_type": 1, "file_name": filename, "file_data": base64.b64encode(png).decode("ascii"), "srv_send_msg": False}))

    async def upload_file(self, kind, target, data, filename, source_url=""):
        if len(data) > 4_000_000:
            raise QQMediaError("较大本地文件请使用 upload_file_path 分片上传", "upload")
        return self._info(await self._request(self._path(kind, target) + "/files", {
            "file_type": 4, "file_name": filename, "file_data": base64.b64encode(data).decode("ascii"), "srv_send_msg": False}))

    async def upload_file_path(self, kind, target, path):
        path = Path(path)
        size = path.stat().st_size
        if size > 50 * 1024**2: raise QQMediaError("结果文件超过本程序 50MB 预算", "upload")
        if size <= 4_000_000: return await self.upload_file(kind, target, path.read_bytes(), path.name)
        md5, sha1 = hashlib.md5(usedforsecurity=False), hashlib.sha1(usedforsecurity=False)
        with path.open("rb") as file:
            first = file.read(10002432)
            prefix_md5 = hashlib.md5(first, usedforsecurity=False).hexdigest()
            md5.update(first); sha1.update(first)
            while block := file.read(1024 * 1024):
                md5.update(block); sha1.update(block)
        base = self._path(kind, target)
        prepared = await self._request(base + "/upload_prepare", {
            "file_type": 4, "file_size": str(size), "file_name": path.name,
            "md5": md5.hexdigest(), "sha1": sha1.hexdigest(), "md5_10m": prefix_md5})
        upload_id, parts = prepared.get("upload_id"), prepared.get("parts")
        block_size = int(prepared.get("block_size", 0))
        if not upload_id or not isinstance(parts, list) or not 1 <= block_size <= 32 * 1024**2:
            raise QQMediaError("QQ 分片配置异常", "protocol")
        parts = sorted(parts, key=lambda p: p["index"])
        if len(parts) != (size + block_size - 1) // block_size or [p["index"] for p in parts] != list(range(len(parts))):
            raise QQMediaError("QQ 分片列表不完整", "protocol")
        session = await self._get_session()
        with path.open("rb") as file:
            for part in parts:
                block = file.read(block_size)
                url = part["presigned_url"]
                if urlparse(url).scheme != "https": raise QQMediaError("分片地址必须为 HTTPS", "protocol")
                await _check_public_url(url)
                async with session.put(url, data=block, allow_redirects=False) as response:
                    if not 200 <= response.status < 300:
                        raise QQMediaError("分片上传失败，结果仍保存在本机", "upload", status=response.status)
                await self._request(base + "/upload_part_finish", {
                    "upload_id": upload_id, "part_index": part["index"], "block_size": str(len(block)),
                    "md5": hashlib.md5(block, usedforsecurity=False).hexdigest()})
        return self._info(await self._request(base + "/files", {
            "file_type": 4, "file_name": path.name, "upload_id": upload_id, "srv_send_msg": False}))

    async def _send_payload(self, kind, target, payload, msg_id, msg_seq):
        if msg_id:
            payload.update(msg_id=msg_id, msg_seq=msg_seq or 1)
        elif not self.active_delivery:
            raise QQMediaError("主动消息未启用", "permission")
        async with self._send_lock:
            delay = .26 - (time.monotonic() - self._last_send)
            if delay > 0: await asyncio.sleep(delay)
            self._last_send = time.monotonic()
            return await self._request(self._path(kind, target) + "/messages", payload)

    async def send_text(self, kind, target, text, msg_id=None, msg_seq=None):
        await self._send_payload(kind, target, {"msg_type": 0, "content": text}, msg_id, msg_seq)

    async def send_image(self, kind, target, file_info, msg_id=None, msg_seq=None):
        await self._send_payload(kind, target, {"msg_type": 7, "media": {"file_info": file_info}}, msg_id, msg_seq)

    async def send_file(self, kind, target, file_info, msg_id=None, msg_seq=None):
        await self.send_image(kind, target, file_info, msg_id, msg_seq)
