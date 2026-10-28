import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from file_tools import DownloadError
from input_cache import (InputCacheError, cache_attachments, discard_cached_attachments,
                         read_cached_attachment)
from task_store import TaskStore


@pytest.fixture
def store(tmp_path):
    instance = TaskStore(tmp_path)
    try:
        yield instance
    finally:
        instance.close()


def collection(store, owner="alice"):
    return store.create(owner, f"collect-{owner}", "group", "g",
                        {"collected": True, "attachments": []}, state="collecting")[0]


def attachment(name):
    return {"url": f"https://example.com/{name}.png", "name": f"{name}.png", "content_type": "image/png"}


def downloader(monkeypatch, contents):
    calls = []

    async def download(url, *, max_mb, timeout):
        calls.append((url, max_mb, timeout))
        value = contents[url]
        if isinstance(value, BaseException):
            raise value
        return SimpleNamespace(data=value)

    monkeypatch.setattr("input_cache.download_file", download)
    return calls


async def cache_and_save(store, task, attachments, **kwargs):
    items = await cache_attachments(store, task, attachments, **kwargs)
    updated, _ = store.append_materials(task["task_id"], task["owner"], attachments=items)
    return updated, items


def input_files(store, task):
    return sorted((store.task_dir(task["task_id"]) / "inputs").glob("*.bin"))


def test_cached_attachment_survives_store_restart(tmp_path, monkeypatch):
    source = attachment("persisted")
    downloader(monkeypatch, {source["url"]: b"persistent synthetic image"})
    initial = TaskStore(tmp_path)
    task = collection(initial)
    updated, items = asyncio.run(cache_and_save(initial, task, [source]))
    initial.close()
    reopened = TaskStore(tmp_path)
    try:
        reopened.recover()
        restored = reopened.get(updated["task_id"])
        assert restored["state"] == "collecting"
        assert read_cached_attachment(reopened, restored, items[0]) == b"persistent synthetic image"
    finally:
        reopened.close()


def test_duplicate_urls_and_bytes_are_not_downloaded_or_cached_repeatedly(store, monkeypatch):
    first, alias, different = (attachment(name) for name in ("first", "alias", "different"))
    calls = downloader(monkeypatch, {first["url"]: b"same image", alias["url"]: b"same image",
                                    different["url"]: b"another image"})

    async def check():
        task = collection(store)
        task, original = await cache_and_save(store, task, [first])
        calls.clear()
        added = await cache_attachments(store, task, [first, alias, alias, different])
        assert [item["url"] for item in added] == [different["url"]]
        assert [call[0] for call in calls] == [alias["url"], different["url"]]
        assert len(input_files(store, task)) == 2
        assert read_cached_attachment(store, task, original[0]) == b"same image"

    asyncio.run(check())


def test_same_message_duplicate_images_only_create_one_cached_file(store, monkeypatch):
    first, alias = attachment("first"), attachment("alias")
    calls = downloader(monkeypatch, {first["url"]: b"same", alias["url"]: b"same"})

    async def check():
        task = collection(store)
        items = await cache_attachments(store, task, [first, first, alias, alias])
        assert len(items) == 1 and len(input_files(store, task)) == 1
        assert [call[0] for call in calls] == [first["url"], alias["url"]]

    asyncio.run(check())


@pytest.mark.parametrize("failure", [DownloadError("synthetic download failure"), asyncio.CancelledError()])
def test_partial_failure_discards_only_current_uploads(store, monkeypatch, failure):
    old, fresh, failed = (attachment(name) for name in ("old", "fresh", "failed"))
    downloader(monkeypatch, {old["url"]: b"old image", fresh["url"]: b"new image", failed["url"]: failure})

    async def check():
        task, kept = await cache_and_save(store, collection(store), [old])
        old_files = input_files(store, task)
        expected = InputCacheError if isinstance(failure, DownloadError) else asyncio.CancelledError
        with pytest.raises(expected):
            await cache_attachments(store, task, [fresh, failed])
        assert input_files(store, task) == old_files
        assert read_cached_attachment(store, task, kept[0]) == b"old image"
        assert store.get(task["task_id"])["request"] == task["request"]

    asyncio.run(check())


def test_cached_material_cannot_be_read_by_another_owner(store, monkeypatch):
    source = attachment("private")
    downloader(monkeypatch, {source["url"]: b"owner scoped image"})
    alice, items = asyncio.run(cache_and_save(store, collection(store), [source]))
    bob = collection(store, "bob")
    with pytest.raises(InputCacheError, match="本会话"):
        read_cached_attachment(store, bob, items[0])
    assert read_cached_attachment(store, alice, items[0]) == b"owner scoped image"


def test_cached_material_remains_usable_in_same_owner_followup(store, monkeypatch):
    source = attachment("parent")
    downloader(monkeypatch, {source["url"]: b"parent image"})
    parent, items = asyncio.run(cache_and_save(store, collection(store), [source]))
    child, _ = store.create("alice", "followup", "group", "g", {"parent_id": parent["task_id"]})
    assert read_cached_attachment(store, child, items[0]) == b"parent image"
    discard_cached_attachments(store, child, items)
    assert read_cached_attachment(store, parent, items[0]) == b"parent image"


@pytest.mark.parametrize("invalid_name", ["../outside.bin", "a" * 32 + ".bin/..", "A" * 32 + ".bin",
                                          "a" * 32 + ".txt", "short.bin", 123, ["not-a-name"]])
def test_invalid_cache_names_are_rejected(store, invalid_name):
    task = collection(store)
    item = {**attachment("invalid"), "source_task_id": task["task_id"], "cached_name": invalid_name,
            "sha256": "irrelevant"}
    with pytest.raises(InputCacheError, match="名称"):
        read_cached_attachment(store, task, item)
    assert not input_files(store, task)


@pytest.mark.parametrize("link_kind", ["file", "directory"])
def test_cache_symlinks_are_rejected_without_reading_or_deleting_target(store, tmp_path, link_kind):
    task = collection(store)
    root = store.task_dir(task["task_id"])
    outside = tmp_path / "unrelated"
    outside.mkdir()
    name = "a" * 32 + ".bin"
    sentinel = outside / name
    sentinel.write_bytes(b"unrelated synthetic data")
    directory = root / "inputs"
    try:
        if link_kind == "directory":
            directory.symlink_to(outside, target_is_directory=True)
        else:
            directory.mkdir()
            (directory / name).symlink_to(sentinel)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symbolic links unavailable: {exc}")
    item = {"source_task_id": task["task_id"], "cached_name": name, "sha256": "irrelevant"}
    with pytest.raises(InputCacheError, match="路径"):
        read_cached_attachment(store, task, item)
    with pytest.raises(InputCacheError, match="路径"):
        discard_cached_attachments(store, task, [item])
    assert sentinel.read_bytes() == b"unrelated synthetic data"


def test_tampered_cache_digest_is_rejected(store, monkeypatch):
    source = attachment("tampered")
    downloader(monkeypatch, {source["url"]: b"original"})
    task, items = asyncio.run(cache_and_save(store, collection(store), [source]))
    input_files(store, task)[0].write_bytes(b"changed contents")
    with pytest.raises(InputCacheError, match="校验"):
        read_cached_attachment(store, task, items[0])


def test_expired_source_cache_is_not_reused(store, monkeypatch):
    source = attachment("expired")
    downloader(monkeypatch, {source["url"]: b"old image"})
    task, items = asyncio.run(cache_and_save(store, collection(store), [source]))
    store.update(task["task_id"], state="expired")
    with pytest.raises(InputCacheError, match="过期"):
        read_cached_attachment(store, task, items[0])


def test_missing_cache_field_is_a_regular_remote_attachment(store):
    assert read_cached_attachment(store, collection(store), attachment("remote")) is None


@pytest.mark.parametrize("count", [4, 5])
def test_per_message_attachment_limit_is_enforced_before_downloading(store, monkeypatch, count):
    sources = [attachment(str(index)) for index in range(count)]
    calls = downloader(monkeypatch, {item["url"]: str(index).encode("ascii") for index, item in enumerate(sources)})

    async def check():
        task = collection(store)
        if count == 5:
            with pytest.raises(InputCacheError, match="4 个附件"):
                await cache_attachments(store, task, sources)
            assert not calls and not input_files(store, task)
        else:
            assert len(await cache_attachments(store, task, sources)) == 4
            assert len(calls) == 4

    asyncio.run(check())


def test_total_twelve_attachment_limit_preserves_existing_cache(store, monkeypatch):
    sources = [attachment(str(index)) for index in range(13)]
    downloader(monkeypatch, {item["url"]: f"image-{index}".encode("ascii") for index, item in enumerate(sources)})

    async def check():
        task = collection(store)
        for offset in range(0, 12, 4):
            task, _ = await cache_and_save(store, task, sources[offset:offset + 4])
        previous = input_files(store, task)
        with pytest.raises(InputCacheError, match="12 个附件"):
            await cache_attachments(store, task, sources[12:])
        assert input_files(store, task) == previous and len(previous) == 12

    asyncio.run(check())


def test_disk_budget_failure_removes_new_materials_but_keeps_old_ones(store, monkeypatch):
    old, small, large = (attachment(name) for name in ("old", "small", "large"))
    downloader(monkeypatch, {old["url"]: b"old", small["url"]: b"small", large["url"]: b"x" * 1024**2})

    async def check():
        task, kept = await cache_and_save(store, collection(store), [old])
        previous = input_files(store, task)
        with pytest.raises(InputCacheError, match="容量预算"):
            await cache_attachments(store, task, [small, large], max_total_mb=1)
        assert input_files(store, task) == previous
        assert read_cached_attachment(store, task, kept[0]) == b"old"

    asyncio.run(check())


@pytest.mark.parametrize("content", [b"", b"x" * (8 * 1024**2 + 1)], ids=["empty", "oversized"])
def test_empty_or_oversized_download_is_not_cached(store, monkeypatch, content):
    source = attachment("invalid-size")
    downloader(monkeypatch, {source["url"]: content})

    async def check():
        task = collection(store)
        with pytest.raises(InputCacheError, match="为空或超过"):
            await cache_attachments(store, task, [source])
        assert not input_files(store, task)

    asyncio.run(check())


def test_discard_only_removes_explicit_new_cache_items(store, monkeypatch):
    old, fresh = attachment("old"), attachment("fresh")
    downloader(monkeypatch, {old["url"]: b"old", fresh["url"]: b"fresh"})

    async def check():
        task, kept = await cache_and_save(store, collection(store), [old])
        temporary = await cache_attachments(store, task, [fresh])
        discard_cached_attachments(store, task, temporary)
        assert len(input_files(store, task)) == 1
        assert read_cached_attachment(store, task, kept[0]) == b"old"

    asyncio.run(check())
