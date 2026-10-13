"""Fail CI when a safety-critical module falls below its coverage floor."""

import json
import sys
from pathlib import Path


REPORT = Path(sys.argv[1] if len(sys.argv) > 1 else "coverage.json")
THRESHOLDS = {
    "app/services/conversation_service.py": 85,
    "app/services/reservation_service.py": 85,
    "app/services/agent_service.py": 85,
    "app/agent/tools.py": 85,
    "app/agent/tracer.py": 85,
}


def main() -> int:
    report = json.loads(REPORT.read_text(encoding="utf-8"))
    files = report.get("files", {})
    failures = []
    for filename, threshold in THRESHOLDS.items():
        key = next((name for name in files if name.endswith(filename)), None)
        if key is None:
            failures.append(f"{filename}: no coverage data")
            continue
        summary = files[key]["summary"]
        covered = summary["percent_covered"]
        print(f"{filename}: {covered:.1f}% (required {threshold}%)")
        if covered < threshold:
            failures.append(f"{filename}: {covered:.1f}% < {threshold}%")
    if failures:
        print("Critical coverage check failed:")
        print("\n".join(f"- {item}" for item in failures))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
