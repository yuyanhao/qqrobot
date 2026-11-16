from contextlib import closing

from task_store import TaskStore


def test_existing_favorite_content_and_ocr_metadata_remain_available(tmp_path):
    owner, tid = "g:a", "Q261115-000001"
    metadata = {"needs_confirmation": True, "notes": ["分母不清晰"]}
    with closing(TaskStore(tmp_path)) as store:
        store.save_favorite(tid, owner, "原题题干", "", "错因", question_ocr=metadata)
        store.save_favorite(tid, owner, "原题题干", "补存解答")
    with closing(TaskStore(tmp_path)) as store:
        favorite, = store.favorites_in_order(owner)
        assert favorite["task_id"] == tid
        assert favorite["question_ocr"] == metadata
        assert favorite["note"] == "错因"
        assert favorite["answer"] == "补存解答"
