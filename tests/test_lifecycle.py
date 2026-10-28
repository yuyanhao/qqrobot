import asyncio
from collections import OrderedDict
import time
from types import SimpleNamespace

from task_bot import TaskBot, ReplyContext
from task_store import TaskStore


def make_bot(tmp_path, run, timeout=1):
    bot = object.__new__(TaskBot)
    bot.store = TaskStore(tmp_path)
    bot.solver = SimpleNamespace(run=run)
    bot.settings = SimpleNamespace(job_timeout=timeout, active_delivery=False)
    bot.queue = asyncio.Queue(maxsize=5)
    bot.resource_lock = asyncio.Lock()
    bot.contexts = {}
    bot.running_task = bot.running_id = None
    return bot


async def stop_worker(worker):
    worker.cancel()
    try:
        await worker
    except asyncio.CancelledError:
        pass


def test_cancel_running_job_keeps_worker_alive(tmp_path):
    async def check():
        started = asyncio.Event()
        async def run(task):
            if task['request']['text'] == 'first':
                started.set()
                await asyncio.Event().wait()
            bot.store.update(task['task_id'], state='completed')
        bot = make_bot(tmp_path, run)
        first, _ = bot.store.create('u', 'm1', 'c2c', 'u', {'text': 'first'})
        second, _ = bot.store.create('u', 'm2', 'c2c', 'u', {'text': 'second'})
        for task in (first, second): bot.queue.put_nowait(task['task_id'])
        worker = asyncio.create_task(bot._worker())
        await asyncio.wait_for(started.wait(), 2)
        bot.store.update(first['task_id'], state='cancelled')
        bot.running_task.cancel()
        await asyncio.wait_for(bot.queue.join(), 2)
        assert not worker.done()
        assert bot.store.get(first['task_id'])['state'] == 'cancelled'
        assert bot.store.get(second['task_id'])['state'] == 'completed'
        await stop_worker(worker)
        bot.store.close()
    asyncio.run(check())


def test_overall_timeout_preserves_draft_for_new_query(tmp_path):
    async def check():
        async def run(task):
            path = bot.store.task_dir(task['task_id']) / 'draft.txt'
            path.write_text('已经完成的推导', encoding='utf-8')
            bot.store.update(task['task_id'], state='verifying', result={'text_path': str(path)})
            await asyncio.Event().wait()
        bot = make_bot(tmp_path, run, timeout=0.05)
        task, _ = bot.store.create('u', 'm1', 'c2c', 'u', {'text': '题目'})
        bot.queue.put_nowait(task['task_id'])
        worker = asyncio.create_task(bot._worker())
        await asyncio.wait_for(bot.queue.join(), 2)
        row = bot.store.get(task['task_id'])
        assert row['state'] == 'completed' and row['result']['answer_status'] == 'uncertain'
        assert row['delivery'] == 'awaiting_query'
        sent = []
        async def text(context, value): sent.append((context.message_id, value)); return True
        bot._text = text
        await bot._deliver(row, ReplyContext('c2c', 'u', 'u', 'new-query', time.time()), '文字')
        assert sent[0][0] == 'new-query' and '已经完成的推导' in sent[0][1]
        await stop_worker(worker)
        bot.store.close()
    asyncio.run(check())


def test_delivery_exception_does_not_stop_next_job(tmp_path):
    async def check():
        async def run(task): bot.store.update(task['task_id'], state='completed')
        bot = make_bot(tmp_path, run)
        async def broken_delivery(*args): raise OSError('synthetic file missing')
        bot._deliver = broken_delivery
        ids = []
        for number in range(2):
            task, _ = bot.store.create('u', f'm{number}', 'c2c', 'u', {'text': '题目'})
            tid = task['task_id']
            ids.append(tid)
            bot.contexts[tid] = ReplyContext('c2c', 'u', 'u', f'm{number}', time.time())
            bot.queue.put_nowait(tid)
        worker = asyncio.create_task(bot._worker())
        await asyncio.wait_for(bot.queue.join(), 2)
        assert not worker.done()
        assert all(bot.store.get(tid)['delivery'] == 'awaiting_query' for tid in ids)
        assert all(bot.store.get(tid)['state'] == 'completed' for tid in ids)
        await stop_worker(worker)
        bot.store.close()
    asyncio.run(check())


def test_restart_keeps_draft_without_recharging(tmp_path):
    store = TaskStore(tmp_path)
    task, _ = store.create('u', 'm', 'c2c', 'u', {'text': '题目'})
    path = store.task_dir(task['task_id']) / 'draft.txt'
    path.write_text('已保存的草稿', encoding='utf-8')
    store.update(task['task_id'], state='verifying', result={'text_path': str(path)})
    store.close()
    reopened = TaskStore(tmp_path)
    reopened.recover()
    row = reopened.get(task['task_id'], 'u')
    assert row['state'] == 'completed' and row['result']['answer_status'] == 'uncertain'
    assert path.read_text(encoding='utf-8') == '已保存的草稿'
    reopened.close()


def test_all_three_skills_are_loaded_into_solver_prompt():
    from skill_loader import load_solver_instructions
    text = load_solver_instructions()
    for name in ('math-solving', 'physics-solving', 'math-physics-plotting'):
        assert f'【{name} 教学规范】' in text
    assert '你没有文件读取工具' in text
