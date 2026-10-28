from __future__ import annotations
import json
from pathlib import Path
import tempfile
from process_tools import run_worker


async def execute(calls: list[dict], timeout: int = 20, workdir: Path | None = None) -> list[dict]:
    calls = [c for c in calls[:4] if isinstance(c, dict)]
    if not calls:
        return []
    with tempfile.TemporaryDirectory(prefix="sympy-", dir=workdir) as temp:
        try:
            output = await run_worker(Path(__file__).with_name("sympy_worker.py"), Path(temp),
                                      timeout=timeout,
                                      payload=json.dumps({"calls": calls}).encode("utf-8"))
            return json.loads(output)["results"]
        except Exception as exc:
            return [{"call_id": i, "tool": c.get("tool"), "args": c.get("args"),
                     "ok": False, "error": f"计算未完成：{type(exc).__name__}"}
                    for i, c in enumerate(calls, 1)]


def format_results(results: list[dict]) -> str:
    return json.dumps(results, ensure_ascii=False, indent=2)
