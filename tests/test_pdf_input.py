import asyncio
from contextlib import aclosing
import io
import json

from PIL import Image
import pytest

from pdf_tools import PDFError, _select_pages, pdf_pages_from_text, render_pdf
from render_service import render_input_pdf_batches_async


def colored_pdf(count=17):
    images = [Image.new("RGB", (30, 40), (number * 10,) * 3) for number in range(1, count + 1)]
    try:
        buffer = io.BytesIO()
        images[0].save(buffer, format="PDF", save_all=True, append_images=images[1:])
        return buffer.getvalue()
    finally:
        for image in images:
            image.close()


@pytest.mark.parametrize("text,expected,selected", [
    ("读取第3页", [3], True),
    ("第3-5页", [3, 4, 5], True),
    ("第3页到第5页", [3, 4, 5], True),
    ("第1-6页", [1, 2, 3, 4, 5, 6], True),
    ("第2、5、8-10页", [2, 5, 8, 9, 10], True),
    ("只读2-5页", [2, 3, 4, 5], True),
    ("第10至8页", [8, 9, 10], True),
    ("第2页、第5页，第8-10页", [2, 5, 8, 9, 10], True),
    ("第2、2、3-5、4页", [2, 3, 4, 5], True),
    ("第３－５页", [3, 4, 5], True),
    ("这份PDF共10页，只读第3页", [3], True),
    ("总共有10页，第3页", [3], True),
    ("总页数为10页，第3-5页", [3, 4, 5], True),
])
def test_page_selection_uses_the_complete_explicit_selection_within_limit(text, expected, selected):
    assert _select_pages(text, 20) == (expected, selected)


@pytest.mark.parametrize("text", ["", "求第3题，给出2种方法", "第0页", "第-1页", "第2、99页", "第19-21页", "第1000000000页",
                                     "第1.5页", "第2、、5页", "第2/5页", "第2--5页",
                                     "读取第3题", "只读第三页", "只读第3-5", "只读第3页和7", "第1-11页",
                                     "第1、2、3、4、5、6、7、8、9、10、11页"])
def test_invalid_page_selection_never_falls_back_to_all_or_a_partial_selection(text):
    with pytest.raises(PDFError):
        _select_pages(text, 20)


def test_pdf_pages_from_text_finds_page_numbers_anywhere():
    assert pdf_pages_from_text("请解答第3-5页，共10页") == {3, 4, 5}
    assert pdf_pages_from_text("第3题不需要页码") == set()
    with pytest.raises(PDFError, match="最多读取"):
        pdf_pages_from_text("第1-11页")


def test_automatic_selection_reads_only_the_first_pages():
    assert _select_pages("", 20, auto_pages=5) == ([1, 2, 3, 4, 5], False)
    assert _select_pages("", 3, auto_pages=5) == ([1, 2, 3], False)
    assert _select_pages("", 20, max_pages=4, auto_pages=5) == ([1, 2, 3, 4], False)


def test_caller_cap_can_only_reduce_the_shared_page_limit():
    with pytest.raises(PDFError, match="最多读取 4 页"):
        _select_pages("第1-5页", 17, 4)
    assert _select_pages("第3页", 17, 4) == ([3], True)


def test_synchronous_pdf_requires_explicit_page_selection():
    with pytest.raises(PDFError, match="必须明确指定"):
        render_pdf(colored_pdf(6))


@pytest.mark.parametrize("text,expected", [
    ("第1-5页", [1, 2, 3, 4, 5]),
    ("第2、5、8-10页", [2, 5, 8, 9, 10]),
])
def test_real_pdf_batches_read_only_explicit_selection_in_order_and_cleanup(tmp_path, text, expected):
    async def check():
        data = colored_pdf()
        observed, sizes = [], []
        async with aclosing(render_input_pdf_batches_async(data, tmp_path, text)) as batches:
            async for images, total, numbers, selected in batches:
                assert total == 17 and selected
                assert (tmp_path / "input.pdf").read_bytes() == data
                assert not list(tmp_path.glob("preview-*.png"))
                sizes.append(len(images))
                assert 1 <= len(images) == len(numbers) <= 4
                for payload, number in zip(images, numbers):
                    with Image.open(io.BytesIO(payload)) as image:
                        assert abs(image.getpixel((0, 0))[0] - number * 10) <= 2
                observed.extend(numbers)
        assert observed == expected
        assert sizes == [min(4, len(expected) - start) for start in range(0, len(expected), 4)]
        assert not list(tmp_path.glob("input*")) and not list(tmp_path.glob("preview-*.png"))
    asyncio.run(check())


def test_real_pdf_batches_can_auto_read_the_first_pages(tmp_path):
    async def check():
        data = colored_pdf()
        observed = []
        async with aclosing(render_input_pdf_batches_async(data, tmp_path, "", auto_pages=5)) as batches:
            async for images, total, numbers, selected in batches:
                assert total == 17 and not selected
                observed.extend(numbers)
        assert observed == [1, 2, 3, 4, 5]
    asyncio.run(check())


def test_invalid_selection_from_real_worker_keeps_page_error_and_cleans_up(tmp_path):
    async def check():
        with pytest.raises(PDFError, match="17.*1-17"):
            async with aclosing(render_input_pdf_batches_async(colored_pdf(), tmp_path, "第2、99页")) as batches:
                await anext(batches)
        assert not list(tmp_path.glob("input*")) and not list(tmp_path.glob("preview-*.png"))
    asyncio.run(check())


def test_closing_between_batches_removes_input_files(tmp_path):
    async def check():
        async with aclosing(render_input_pdf_batches_async(colored_pdf(), tmp_path, "第1-5页")) as batches:
            first = await anext(batches)
            assert first[2] == [1, 2, 3, 4]
            assert (tmp_path / "input.pdf").exists()
        assert not list(tmp_path.glob("input*")) and not list(tmp_path.glob("preview-*.png"))
    asyncio.run(check())


@pytest.mark.parametrize("failure", [RuntimeError("PDFError: PDF 第 5 页渲染失败"), TimeoutError()])
def test_later_batch_worker_failure_is_reported_and_cleans_all_files(tmp_path, monkeypatch, failure):
    async def worker(script, directory, args, **kwargs):
        request = json.loads((directory / args[0]).read_text(encoding="utf-8"))
        if request["offset"]:
            (directory / "preview-005.png").write_bytes(b"partial")
            raise failure
        paths = []
        for number in range(1, 5):
            path = directory / f"preview-{number:03d}.png"
            path.write_bytes(b"image")
            paths.append(str(path))
        return json.dumps({"preview_paths": paths, "total_pages": 8, "page_numbers": [1, 2, 3, 4],
                           "selected": True, "selection_count": 5}).encode()

    monkeypatch.setattr("render_service.run_worker", worker)

    async def check():
        with pytest.raises(PDFError, match="第 5 页|超时"):
            async with aclosing(render_input_pdf_batches_async(b"synthetic", tmp_path, "第1-5页")) as batches:
                assert (await anext(batches))[2] == [1, 2, 3, 4]
                await anext(batches)
        assert not list(tmp_path.glob("input*")) and not list(tmp_path.glob("preview-*.png"))
    asyncio.run(check())


def test_cancelling_active_worker_cleans_up_input_and_partial_images(tmp_path, monkeypatch):
    async def check():
        started = asyncio.Event()

        async def worker(script, directory, args, **kwargs):
            (directory / "preview-001.png").write_bytes(b"partial")
            started.set()
            await asyncio.Future()

        monkeypatch.setattr("render_service.run_worker", worker)
        async with aclosing(render_input_pdf_batches_async(b"synthetic", tmp_path, "第1-5页")) as batches:
            pending = asyncio.create_task(anext(batches))
            await started.wait()
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
        assert not list(tmp_path.glob("input*")) and not list(tmp_path.glob("preview-*.png"))
    asyncio.run(check())
