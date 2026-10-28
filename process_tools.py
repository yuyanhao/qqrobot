"""Small, bounded child-process runner for parsing, calculation and rendering."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
import signal
import sys


def worker_environment(workdir: Path) -> dict[str, str]:
    environment = {k: os.environ[k] for k in
                   ("PATH", "SystemRoot", "WINDIR", "LANG", "LC_ALL") if k in os.environ}
    environment.update(PYTHONIOENCODING="utf-8", PYTHONNOUSERSITE="1",
                       TMP=str(workdir), TEMP=str(workdir), TMPDIR=str(workdir),
                       MPLCONFIGDIR=str(workdir / ".mplcache"),
                       OPENBLAS_NUM_THREADS="1", OMP_NUM_THREADS="1",
                       openin_any="p", openout_any="p")
    return environment


def set_worker_limits(memory_mb=1024, cpu_seconds=90):
    if sys.platform == "linux":
        import resource
        resource.setrlimit(resource.RLIMIT_AS, (memory_mb * 1024**2,) * 2)
        resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds,) * 2)
        resource.setrlimit(resource.RLIMIT_FSIZE, (64 * 1024**2,) * 2)


async def _kill(proc):
    if proc.returncode is not None:
        return
    if sys.platform == "win32":
        killer = await asyncio.create_subprocess_exec(
            "taskkill", "/PID", str(proc.pid), "/T", "/F",
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await killer.wait()
        if proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    await proc.wait()


async def run_worker(script: Path, workdir: Path, args=(), *, timeout=120,
                     payload: bytes | None = None) -> bytes:
    workdir = workdir.resolve()
    workdir.mkdir(parents=True, exist_ok=True)
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-X", "utf8", str(script.resolve()), *map(str, args),
        cwd=workdir, env=worker_environment(workdir),
        stdin=asyncio.subprocess.PIPE if payload is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        start_new_session=sys.platform != "win32")
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(payload), timeout)
    except BaseException:
        await asyncio.shield(_kill(proc))
        raise
    if proc.returncode:
        # Child errors do not contain provider responses or the parent environment.
        detail = stderr.decode("utf-8", errors="replace")[-500:]
        raise RuntimeError(f"工作进程失败（{proc.returncode}）：{detail}")
    if len(stdout) > 2 * 1024**2:
        raise RuntimeError("工作进程返回内容过大")
    return stdout
