import time

import pytest

from task_store import QueueFull, TaskStore, describe


@pytest.fixture
def store(tmp_path):
    instance = TaskStore(tmp_path)
    try:
        yield instance
    finally:
        instance.close()


def draft(store, owner="alice", message="new", **request):
    return store.create(owner, message, "group", "g", {"collected": True, **request}, state="collecting")[0]


def image(number, digest=None, url=None):
    return {"url": url or f"https://example.com/{number}.png", "sha256": digest or f"digest-{number}",
            "cached_name": f"{number:032x}.bin", "source_task_id": "Q261027-000001"}


def test_collecting_state_and_initial_materials_use_existing_schema(store):
    task = draft(store, text="原题", attachments=[image(1), image(1)])
    assert task["state"] == "collecting" and task["request"]["collected"] is True
    assert len(task["request"]["attachments"]) == 1
    assert store.collecting("alice")["task_id"] == task["task_id"]
    assert store.collecting("bob") is None
    assert [row[1] for row in store.db.execute("PRAGMA table_info(tasks)")].count("request") == 1


def test_collection_creation_is_deduplicated_by_original_message(store):
    first = draft(store)
    replay, created = store.create("alice", "new", "group", "g", {"collected": True}, state="collecting")
    assert not created and replay["task_id"] == first["task_id"]
    with pytest.raises(QueueFull, match=first["task_id"]):
        draft(store, message="another")


def test_collection_slots_and_running_queue_slots_are_separate(store):
    first, _ = store.create("alice", "draft", "group", "g", {}, state="collecting", max_active=1)
    queued, _ = store.create("bob", "job", "group", "g", {"text": "另一道题"}, max_active=1)
    assert queued["state"] == "queued"
    with pytest.raises(QueueFull, match="收集"):
        store.create("charlie", "draft", "group", "g", {}, state="collecting", max_active=1)
    store.update(first["task_id"], state="cancelled")
    replacement, _ = store.create("charlie", "draft", "group", "g", {}, state="collecting", max_active=1)
    assert replacement["state"] == "collecting"


def test_full_running_queue_does_not_prevent_collecting_materials(store):
    store.create("alice", "job", "group", "g", {"text": "运行中"}, max_active=1, max_user_active=1)
    task, _ = store.create("alice", "draft", "group", "g", {"text": "待补图"}, state="collecting",
                           max_active=1, max_user_active=1)
    updated, added = store.append_materials(task["task_id"], "alice", attachments=[image(1)], message_id="photo")
    assert added == 1 and updated["state"] == "collecting"
    with pytest.raises(QueueFull):
        store.start_collection(task["task_id"], "alice", max_active=1, max_user_active=1)
    assert store.get(task["task_id"])["state"] == "collecting"


def test_append_preserves_order_and_deduplicates_hash_or_url(store):
    task = draft(store, text="第一段", attachments=[image(1)])
    updated, added = store.append_materials(task["task_id"], "alice", text="第二段", quoted="引用条件",
        attachments=[image(2, digest="digest-1"), image(3, url="https://example.com/1.png"), image(4),
                     image(5, digest="digest-4")], message_id="append")
    assert added == 1
    assert updated["request"]["attachments"] == [image(1), image(4)]
    assert updated["request"]["text"] == "第一段\n\n第二段"
    assert updated["request"]["quoted"] == "引用条件"
    replay, added = store.append_materials(task["task_id"], "alice", text="不得重复追加",
                                           attachments=[image(6)], message_id="append")
    assert added == 0 and replay["request"] == updated["request"]


def test_attachment_limit_rejection_is_atomic_and_does_not_consume_message_id(store):
    task = draft(store, text="原文", attachments=[image(i) for i in range(1, 13)])
    with pytest.raises(ValueError, match="12"):
        store.append_materials(task["task_id"], "alice", text="不得部分保存", attachments=[image(13)], message_id="retry")
    assert store.get(task["task_id"])["request"] == task["request"]
    updated, added = store.append_materials(task["task_id"], "alice", text="合法重发", attachments=[image(1)], message_id="retry")
    assert added == 0 and updated["request"]["text"].endswith("合法重发")


def test_combined_text_and_quote_limit_is_atomic(store):
    task = draft(store, text="题" * 12000, quoted="条" * 12000)
    with pytest.raises(ValueError, match="2.4"):
        store.append_materials(task["task_id"], "alice", text="字", attachments=[image(1)], message_id="large")
    assert store.get(task["task_id"])["request"] == task["request"]


@pytest.mark.parametrize("method", ["append", "start"])
def test_material_mutations_are_scoped_to_owner(store, method):
    task = draft(store, text="原题")
    with pytest.raises(ValueError, match="未找到"):
        if method == "append":
            store.append_materials(task["task_id"], "bob", text="不属于我")
        else:
            store.start_collection(task["task_id"], "bob")
    assert store.get(task["task_id"])["state"] == "collecting"
    assert store.get(task["task_id"])["request"] == task["request"]


@pytest.mark.parametrize("materials", [{"text": "文字题"}, {"quoted": "引用题"}, {"attachments": [image(1)]}])
def test_start_collection_queues_existing_id_exactly_once(store, materials):
    task = draft(store, **materials)
    queued = store.start_collection(task["task_id"], "alice")
    assert queued["task_id"] == task["task_id"] and queued["state"] == "queued"
    assert store.collecting("alice") is None
    with pytest.raises(ValueError, match="已开始"):
        store.start_collection(task["task_id"], "alice")
    with pytest.raises(ValueError, match="收集状态"):
        store.append_materials(task["task_id"], "alice", text="太晚")
    assert len(store.list_recent("alice")) == 1


def test_empty_collection_cannot_start(store):
    task = draft(store, text=" \n ", quoted="  ")
    with pytest.raises(ValueError, match="尚未收到"):
        store.start_collection(task["task_id"], "alice")
    assert store.get(task["task_id"])["state"] == "collecting"


def test_start_checks_user_limit_without_mutating_collection(store):
    job, _ = store.create("alice", "job", "group", "g", {"text": "运行题"})
    task = draft(store, text="待排队")
    with pytest.raises(QueueFull, match="你已有"):
        store.start_collection(task["task_id"], "alice", max_user_active=1)
    assert store.get(task["task_id"])["state"] == "collecting"
    store.update(job["task_id"], state="completed")
    assert store.start_collection(task["task_id"], "alice", max_user_active=1)["state"] == "queued"


def test_collection_uses_daily_submission_budget(store):
    task, _ = store.create("alice", "first", "group", "g", {}, state="collecting", max_daily=1)
    store.update(task["task_id"], state="cancelled")
    with pytest.raises(QueueFull, match="24 小时"):
        store.create("alice", "second", "group", "g", {}, state="collecting", max_daily=1)


def test_restart_preserves_collection_and_append_deduplication(tmp_path):
    initial = TaskStore(tmp_path)
    task = draft(initial, text="收集中")
    initial.append_materials(task["task_id"], "alice", attachments=[image(1)], message_id="photo")
    job, _ = initial.create("bob", "job", "group", "g", {"text": "运行中"})
    initial.close()
    reopened = TaskStore(tmp_path)
    try:
        reopened.recover()
        assert reopened.get(task["task_id"])["state"] == "collecting"
        assert reopened.get(job["task_id"])["state"] == "interrupted"
        replay, added = reopened.append_materials(task["task_id"], "alice", text="重发不得改变", message_id="photo")
        assert added == 0 and replay["request"]["text"] == "收集中"
    finally:
        reopened.close()


def test_expired_collection_is_cleaned_using_existing_retention(store):
    task = draft(store, text="过期题")
    directory = store.task_dir(task["task_id"])
    (directory / "cached.bin").write_bytes(b"synthetic cached material")
    store.db.execute("UPDATE tasks SET created=? WHERE task_id=?", (time.time() - 8 * 86400, task["task_id"]))
    store.db.commit()
    store.cleanup()
    expired = store.get(task["task_id"])
    assert expired["state"] == "expired" and expired["request"] == {}
    assert not directory.exists() and store.collecting("alice") is None


def test_collection_progress_explains_attachment_count_and_next_commands(store):
    task = draft(store, attachments=[image(1), image(2)])
    text = describe(task)
    assert "收集中" in text and "2 个附件" in text and "尚未开始" in text
    assert f"/补图 {task['task_id']}" in text and f"/开始 {task['task_id']}" in text


@pytest.mark.parametrize("state", ["completed", "reading", "cancelled"])
def test_create_rejects_unexpected_initial_states(store, state):
    with pytest.raises(ValueError, match="初始状态"):
        store.create("alice", "bad", "group", "g", {}, state=state)
