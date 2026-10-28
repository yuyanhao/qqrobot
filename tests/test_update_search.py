import ast
from pathlib import Path
import subprocess
import sys

import pytest


APP = Path(__file__).resolve().parent.parent
NAMES = ("search_tools.py", "settings.py", "solver.py")


def block():
    script = (APP / "update_search.sh").read_text(encoding="utf-8")
    return script.split("<<'UPDATE_SEARCH_PY'\n", 1)[1].split("\nUPDATE_SEARCH_PY", 1)[0]


def installation(tmp_path):
    target = tmp_path / "installed"
    target.mkdir()
    (target / "search_tools.py").write_text("# Old search module\n", encoding="utf-8")
    (target / "settings.py").write_text('''import os
class Settings:
    bocha_key: str = os.getenv("BOCHA_API_KEY", "")
    bocha_endpoint: str = os.getenv("BOCHA_ENDPOINT", "https://api.bocha.cn/v1/web-search")
    existing_setting = "preserved"
''', encoding="utf-8")
    (target / "solver.py").write_text('''import asyncio
from search_tools import bocha_search
from reader_tools import extract_article
class Solver:
    async def search(self, query):
        return await bocha_search(self.settings.bocha_key, query)

    async def run(self, task):
        if task and self.settings.bocha_key:
            try:
                pass
            except Exception:
                evidence = "联网搜索失败。"
        return "existing solve logic preserved"
''', encoding="utf-8")
    (target / ".env").write_text("DEEPSEEK_API_KEY=synthetic-key\nBOCHA_API_KEY=old-unused-key\n", encoding="utf-8")
    (target / "data").mkdir()
    (target / "data" / "tasks.sqlite3").write_bytes(b"existing task data")
    return target


def run_update(target, phase, prefix=""):
    return subprocess.run([sys.executable, "-X", "utf8", "-c", prefix + block(), str(target), phase],
                          capture_output=True, timeout=30)


def test_standalone_migrates_search_without_overwriting_config_or_solve_logic(tmp_path):
    target = installation(tmp_path)
    originals = {name: (target / name).read_bytes() for name in NAMES}
    assert run_update(target, "check").returncode == 0
    assert all((target / name).read_bytes() == originals[name] for name in NAMES)
    result = run_update(target, "install")
    assert result.returncode == 0, result.stderr.decode("utf-8")
    assert (target / "search_tools.py").read_bytes() == (APP / "search_tools.py").read_bytes()
    solver = (target / "solver.py").read_text(encoding="utf-8")
    assert "bocha" not in solver and "existing solve logic preserved" in solver
    assert "不能声称已获取来源" in solver and 'warnings.append("搜索未完成")' in solver
    assert "existing_setting" in (target / "settings.py").read_text(encoding="utf-8")
    assert (target / ".env").read_text(encoding="utf-8") == "DEEPSEEK_API_KEY=synthetic-key\nBOCHA_API_KEY=old-unused-key\n"
    assert (target / "data" / "tasks.sqlite3").read_bytes() == b"existing task data"
    for name in NAMES:
        assert (target / (name + ".before-deepseek-search")).read_bytes() == originals[name]
    installed = {name: (target / name).read_bytes() for name in NAMES}
    assert run_update(target, "check").returncode == 0
    assert run_update(target, "install").returncode == 0
    assert all((target / name).read_bytes() == installed[name] for name in NAMES)
    assert all((target / (name + ".before-deepseek-search")).read_bytes() == originals[name] for name in NAMES)


def test_current_release_update_is_idempotent(tmp_path):
    target = installation(tmp_path)
    for name in NAMES:
        (target / name).write_bytes((APP / name).read_bytes())
    result = run_update(target, "check")
    assert result.returncode == 0, result.stderr.decode("utf-8")
    assert run_update(target, "install").returncode == 0
    assert all((target / name).read_bytes() == (APP / name).read_bytes() for name in NAMES)
    assert not list(target.glob("*.before-deepseek-search"))


@pytest.mark.parametrize("broken", ["settings.py", "solver.py", "missing"])
def test_unsupported_or_incomplete_installation_is_unchanged(tmp_path, broken):
    target = installation(tmp_path)
    if broken == "missing":
        (target / "search_tools.py").unlink()
    else:
        (target / broken).write_text("VERSION = 'unsupported'\n", encoding="utf-8")
    originals = {path.name: path.read_bytes() for path in target.glob("*.py")}
    assert run_update(target, "check").returncode != 0
    assert not list(target.glob("*.before-deepseek-search"))
    assert {path.name: path.read_bytes() for path in target.glob("*.py")} == originals


def test_install_requires_preflight_backups(tmp_path):
    target = installation(tmp_path)
    originals = {name: (target / name).read_bytes() for name in NAMES}
    assert run_update(target, "install").returncode != 0
    assert all((target / name).read_bytes() == originals[name] for name in NAMES)


def test_interrupted_update_restores_all_original_files(tmp_path):
    target = installation(tmp_path)
    originals = {name: (target / name).read_bytes() for name in NAMES}
    assert run_update(target, "check").returncode == 0
    prefix = '''import os
original_replace = os.replace
replace_count = 0
def fail_second_replace(*args):
    global replace_count
    replace_count += 1
    if replace_count == 2:
        raise OSError("synthetic install failure")
    return original_replace(*args)
os.replace = fail_second_replace
'''
    result = run_update(target, "install", prefix)
    assert result.returncode != 0 and b"synthetic install failure" in result.stderr
    assert all((target / name).read_bytes() == originals[name] for name in NAMES)
    assert not list(target.glob(".search-update-*"))


def test_embedded_python_is_valid_without_execution():
    ast.parse(block())


def test_search_update_preserves_current_dynamic_config_validation(tmp_path):
    target = installation(tmp_path)
    source = (Path(__file__).resolve().parents[1] / "settings.py").read_bytes()
    (target / "settings.py").write_bytes(source)
    assert run_update(target, "check").returncode == 0
    assert run_update(target, "install").returncode == 0
    assert (target / "settings.py").read_bytes() == source
