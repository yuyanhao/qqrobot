from pathlib import Path
import subprocess
import sys


NAMES = ("render.py", "render_worker.py", "render_service.py", "input_cache.py", "solver.py", "task_bot.py", "task_store.py", "search_tools.py", "settings.py", "pdf_tools.py", "ai.py", "qqmedia.py")


def update_block():
    script = (Path(__file__).resolve().parent.parent / "update_delivery.sh").read_text(encoding="utf-8")
    return script.split("<<'UPDATE_PY'\n", 1)[1].split("\nUPDATE_PY", 1)[0]


def installation(tmp_path):
    source, target = tmp_path / "release", tmp_path / "installed"
    source.mkdir()
    target.mkdir()
    for name in NAMES:
        (source / name).write_text("VERSION = 'new'\n", encoding="utf-8")
        (target / name).write_text("VERSION = 'old'\n", encoding="utf-8")
    (target / ".env").write_text("synthetic config\n", encoding="utf-8")
    (target / "data").mkdir()
    (target / "data" / "tasks.sqlite3").write_bytes(b"synthetic task data")
    return source, target


def run_update(source, target, phase, prefix=""):
    return subprocess.run([sys.executable, "-X", "utf8", "-c", prefix + update_block(),
                           str(source), str(target), phase], capture_output=True)


def test_update_preserves_config_tasks_and_initial_backups(tmp_path):
    source, target = installation(tmp_path)
    assert run_update(source, target, "check").returncode == 0
    assert all((target / name).read_text(encoding="utf-8") == "VERSION = 'old'\n" for name in NAMES)
    assert run_update(source, target, "install").returncode == 0
    assert run_update(source, target, "check").returncode == 0
    assert run_update(source, target, "install").returncode == 0
    for name in NAMES:
        (source / name).write_text("VERSION = 'newer'\n", encoding="utf-8")
    assert run_update(source, target, "check").returncode == 0
    assert run_update(source, target, "install").returncode == 0
    for name in NAMES:
        assert (target / name).read_bytes() == (source / name).read_bytes()
        assert (target / (name + ".before-image-delivery")).read_text(encoding="utf-8") == "VERSION = 'old'\n"
    assert (target / ".env").read_text(encoding="utf-8") == "synthetic config\n"
    assert (target / "data" / "tasks.sqlite3").read_bytes() == b"synthetic task data"


def test_update_validates_all_modules_before_backing_up_or_changing_any(tmp_path):
    source, target = installation(tmp_path)
    (source / NAMES[-1]).write_text("def invalid(\n", encoding="utf-8")
    assert run_update(source, target, "check").returncode != 0
    assert not list(target.glob("*.before-image-delivery"))
    assert all((target / name).read_text(encoding="utf-8") == "VERSION = 'old'\n" for name in NAMES)


def test_update_rejects_missing_module_and_same_directory(tmp_path):
    source, target = installation(tmp_path)
    (source / NAMES[-1]).unlink()
    assert run_update(source, target, "check").returncode != 0
    assert run_update(target, target, "check").returncode != 0
    assert not list(target.glob("*.before-image-delivery"))


def test_update_rolls_back_files_if_installation_is_interrupted(tmp_path):
    source, target = installation(tmp_path)
    assert run_update(source, target, "check").returncode == 0
    prefix = """import os
original_replace = os.replace
replace_count = 0
def fail_second_replace(*args):
    global replace_count
    replace_count += 1
    if replace_count == 2:
        raise OSError('synthetic interrupted install')
    return original_replace(*args)
os.replace = fail_second_replace
"""
    result = run_update(source, target, "install", prefix)
    assert result.returncode != 0
    assert "synthetic interrupted install" in result.stderr.decode("utf-8")
    assert all((target / name).read_text(encoding="utf-8") == "VERSION = 'old'\n" for name in NAMES)
    assert not list(target.glob(".delivery-update-*"))


def test_update_installs_new_cache_module_without_existing_file(tmp_path):
    source, target = installation(tmp_path)
    (target / "input_cache.py").unlink()
    assert run_update(source, target, "check").returncode == 0
    assert not (target / "input_cache.py.before-image-delivery").exists()
    assert run_update(source, target, "install").returncode == 0
    assert (target / "input_cache.py").read_bytes() == (source / "input_cache.py").read_bytes()
    assert not (target / "input_cache.py.before-image-delivery").exists()
    assert (target / ".env").read_text(encoding="utf-8") == "synthetic config\n"
    assert (target / "data" / "tasks.sqlite3").read_bytes() == b"synthetic task data"


def test_update_removes_new_module_on_later_installation_failure(tmp_path):
    source, target = installation(tmp_path)
    (target / "input_cache.py").unlink()
    assert run_update(source, target, "check").returncode == 0
    prefix = """import os
original_replace = os.replace
replace_count = 0
def fail_after_new_module(*args):
    global replace_count
    replace_count += 1
    if replace_count == 5:
        raise OSError('synthetic failure after new module')
    return original_replace(*args)
os.replace = fail_after_new_module
"""
    result = run_update(source, target, "install", prefix)
    assert result.returncode != 0
    assert "synthetic failure after new module" in result.stderr.decode("utf-8")
    assert not (target / "input_cache.py").exists()
    assert all((target / name).read_text(encoding="utf-8") == "VERSION = 'old'\n"
               for name in NAMES if name != "input_cache.py")
    assert not list(target.glob(".delivery-update-*"))


def test_update_still_rejects_missing_existing_store_module(tmp_path):
    source, target = installation(tmp_path)
    (target / "task_store.py").unlink()
    assert run_update(source, target, "check").returncode != 0
    assert not list(target.glob("*.before-image-delivery"))
