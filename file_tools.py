"""下载网页文件，带 SSRF 防护、大小限制和文件名解析。"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
import socket
from dataclasses import dataclass
from email.message import Message
from urllib.parse import unquote, urljoin, urlparse

import aiohttp

log = logging.getLogger("qq-deepseek-bot.file")

_EXT_BY_TYPE = {
    "application/pdf": ".pdf",
    "text/markdown": ".md",
    "text/plain": ".txt",
}


class DownloadError(RuntimeError):
    """下载网页文件失败。"""


class PublicResolver(aiohttp.abc.AbstractResolver):
    async def resolve(self, host, port=0, family=socket.AF_UNSPEC):
        infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM, family=family)
        records = []
        for fam, _, proto, _, address in infos:
            if not ipaddress.ip_address(address[0]).is_global:
                raise DownloadError("不允许连接非公网地址")
            records.append({"hostname": host, "host": address[0], "port": port,
                            "family": fam, "proto": proto, "flags": socket.AI_NUMERICHOST})
        return records

    async def close(self):
        pass


@dataclass
class DownloadedFile:
    data: bytes
    filename: str
    content_type: str
    url: str


async def _check_public_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise DownloadError("只支持 http/https 链接")
    if parsed.username or parsed.password:
        raise DownloadError("不接受含登录凭据的链接")
    host = parsed.hostname
    if not host:
        raise DownloadError("链接没有主机名")
    if host.lower() in (
        "localhost",
        "localhost.localdomain",
        "metadata.google.internal",
        "metadata",
    ):
        raise DownloadError("不允许访问本机/云元数据地址")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    if port not in (80, 443):
        raise DownloadError("只允许 80/443 端口")

    try:
        infos = await asyncio.get_running_loop().getaddrinfo(
            host, port, type=socket.SOCK_STREAM
        )
    except Exception as exc:
        raise DownloadError(f"域名解析失败：{host}") from exc

    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        if not ip.is_global:
            raise DownloadError("不允许访问内网地址")


def _filename(disposition: str, url: str, content_type: str) -> str:
    candidates: list[str] = []
    if disposition:
        message = Message()
        message["content-disposition"] = disposition
        name = message.get_filename()
        if name:
            candidates.append(name)

    path = unquote(urlparse(url).path)
    candidates.append(path.rsplit("/", 1)[-1])

    for name in candidates:
        name = name.strip().strip('"').strip("'")
        if not name:
            continue
        name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", name)[:120]
        if name in (".", ".."):
            continue
        if "." not in name:
            name += _EXT_BY_TYPE.get(content_type, "")
        return name or "download.bin"

    return "download" + _EXT_BY_TYPE.get(content_type, ".bin")


async def download_file(
    url: str,
    max_mb: int = 20,
    timeout: int = 30,
    allow_html: bool = False,
) -> DownloadedFile:
    url = (url or "").strip()
    if not url:
        raise DownloadError("链接为空")

    max_bytes = max(1, int(max_mb)) * 1024 * 1024
    timeout_obj = aiohttp.ClientTimeout(total=max(5, int(timeout)))
    parsed_url = urlparse(url)
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0 Safari/537.36"
        ),
        "Accept": "application/pdf,application/octet-stream,text/plain,text/markdown,*/*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Referer": f"{parsed_url.scheme}://{parsed_url.netloc}/",
    }

    current = url
    connector = aiohttp.TCPConnector(resolver=PublicResolver(), use_dns_cache=False)
    async with aiohttp.ClientSession(timeout=timeout_obj, headers=headers, connector=connector) as session:
        for _ in range(4):
            await _check_public_url(current)
            async with session.get(current, allow_redirects=False) as resp:
                if resp.status in (301, 302, 303, 307, 308):
                    location = resp.headers.get("Location")
                    if not location:
                        raise DownloadError("重定向缺少 Location")
                    current = urljoin(current, location)
                    continue

                if resp.status != 200:
                    raise DownloadError(f"下载失败 HTTP {resp.status}")

                content_type = str(resp.content_type or "").lower()
                if not allow_html and content_type.startswith("text/html"):
                    raise DownloadError("这个链接是网页，不是可下载文件")

                length_header = resp.headers.get("Content-Length")
                if length_header:
                    try:
                        if int(length_header) > max_bytes:
                            raise DownloadError(f"文件超过 {max_mb}MB")
                    except ValueError:
                        pass

                chunks: list[bytes] = []
                total = 0
                async for chunk in resp.content.iter_chunked(64 * 1024):
                    total += len(chunk)
                    if total > max_bytes:
                        raise DownloadError(f"文件超过 {max_mb}MB")
                    chunks.append(chunk)

                data = b"".join(chunks)
                filename = _filename(
                    resp.headers.get("Content-Disposition", ""),
                    current,
                    content_type,
                )
                return DownloadedFile(
                    data=data,
                    filename=filename,
                    content_type=content_type,
                    url=current,
                )

    raise DownloadError("重定向次数过多")
