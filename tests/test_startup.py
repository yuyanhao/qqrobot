"""Exercise actual client construction with a read-only application directory."""
import subprocess
import sys
from pathlib import Path

from process_tools import worker_environment


def test_real_client_starts_without_writing_sdk_log(tmp_path):
    project = Path(__file__).resolve().parents[1]
    script = r'''
import asyncio
import builtins
import errno
import os
from pathlib import Path
import sys

os.environ.update(QQ_APPID="fake-app", QQ_SECRET="fake-secret", DEEPSEEK_API_KEY="fake-key")
from task_bot import TaskBot
from settings import Settings
import botpy

root = Path(sys.argv[1])
botpy.logging.DEFAULT_FILE_HANDLER["filename"] = str(root / "readonly-app" / "%(name)s.log")
original_open = builtins.open
attempts = []
def guarded_open(file, *args, **kwargs):
    if isinstance(file, (str, bytes, os.PathLike)) and str(file).endswith("botpy.log"):
        attempts.append(str(file))
        raise OSError(errno.EROFS, "Read-only file system", str(file))
    return original_open(file, *args, **kwargs)
builtins.open = guarded_open

async def main():
    client = TaskBot(botpy.Intents(public_messages=True), settings=Settings(data_dir=root / "data"))
    try:
        assert not attempts, attempts
        assert client.store is not None
    finally:
        await client.shutdown()
asyncio.run(main())
print("actual client constructed with SDK log writes forbidden")
'''
    result = subprocess.run([sys.executable, "-X", "utf8", "-c", script, str(tmp_path)],
                            cwd=project, env=worker_environment(tmp_path),
                            capture_output=True, text=True, encoding="utf-8", timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "actual client constructed" in result.stdout
