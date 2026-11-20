import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

from qqrobot.media import file_tools

def test_large_download_writes_each_chunk_to_file_as_it_arrives(tmp_path, monkeypatch):
    async def public_url(url):
        pass

    monkeypatch.setattr(file_tools, "_check_public_url", public_url)
    monkeypatch.setattr(file_tools.aiohttp, "TCPConnector", lambda **kwargs: None)

    path = tmp_path / "download.pdf"
    first, second = b"%PDF-1.4\n", b"more content"
    with path.open("wb") as output:
        async def iter_chunked(size):
            yield first
            assert output.tell() == len(first)
            yield second

        @asynccontextmanager
        async def get(url, **kwargs):
            yield SimpleNamespace(
                status=200, content_type="application/pdf",
                headers={"Content-Length": str(101 * 1024**2)},
                content=SimpleNamespace(iter_chunked=iter_chunked),
            )

        @asynccontextmanager
        async def session(**kwargs):
            assert kwargs["timeout"].total is None
            yield SimpleNamespace(get=get)

        monkeypatch.setattr(file_tools.aiohttp, "ClientSession", session)
        result = asyncio.run(file_tools.download_file(
            "https://example.com/book.pdf", max_mb=None, timeout=None, output=output,
        ))

    assert path.read_bytes() == first + second
    assert not result.data
