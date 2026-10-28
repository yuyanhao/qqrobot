import json
import sys
from process_tools import set_worker_limits
set_worker_limits(memory_mb=640, cpu_seconds=20)
from safe_math import run

request = json.load(sys.stdin)
results = []
for index, call in enumerate(request.get("calls", [])[:4], 1):
    record = {"call_id": index, "tool": call.get("tool"), "args": call.get("args")}
    try:
        record.update(ok=True, result=run(call))
    except Exception as exc:
        record.update(ok=False, error=str(exc)[:400])
    results.append(record)
print(json.dumps({"results": results}, ensure_ascii=False))
