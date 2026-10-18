"""Read one LangSmith trace and report latency without exporting conversation text.

Usage (from backend): .venv/bin/python -m scripts.analyze_langsmith --trace-id UUID
"""

import argparse
from collections import defaultdict
from datetime import datetime
import json
from uuid import UUID


def _time(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value


def _duration(run):
    start, end = _time(run.get("start_time")), _time(run.get("end_time"))
    return max(0, (end - start).total_seconds() * 1000) if start and end else None


def _self_ms(run, children):
    """Subtract the union of direct child intervals, clipped to the parent."""
    start, end = _time(run.get("start_time")), _time(run.get("end_time"))
    if not start or not end:
        return None
    intervals = []
    for child in children:
        a, b = _time(child.get("start_time")), _time(child.get("end_time"))
        if not a or not b:
            return None
        a, b = max(start, a), min(end, b)
        if b > a:
            intervals.append((a, b))
    covered = 0.0
    cursor = start
    for a, b in sorted(intervals):
        if b > cursor:
            covered += (b - max(a, cursor)).total_seconds() * 1000
            cursor = b
    return max(0, (end - start).total_seconds() * 1000 - covered)


def summarize(runs: list[dict]) -> dict:
    children = defaultdict(list)
    roots = []
    for run in runs:
        parent = run.get("parent_run_id")
        if parent is None:
            roots.append(run)
        else:
            children[str(parent)].append(run)
    rows = []
    for run in runs:
        metadata = (run.get("extra") or {}).get("metadata") or {}
        rows.append({
            "id": str(run["id"]),
            "parent_run_id": str(run["parent_run_id"]) if run.get("parent_run_id") else None,
            "name": run["name"], "type": run["run_type"],
            "duration_ms": _duration(run),
            "self_ms": _self_ms(run, children[str(run["id"])]),
            "failed": bool(run.get("error")),
            "cache_hit": metadata.get("cache_hit"),
            "attempts": metadata.get("attempts"),
            "total_tokens": run.get("total_tokens") if run["run_type"] == "llm" else None,
        })
    known_ids = {str(run["id"]) for run in runs}
    # Metadata may be inherited by descendants. Count each helper invocation
    # once, rather than counting the same cache hit/retry on model child runs.
    helpers = [row for row in rows if row["name"] in ("llm.structured", "llm.agent")]
    return {
        "root_latency_ms": _duration(roots[0]) if len(roots) == 1 else None,
        "root_count": len(roots),
        "orphan_count": sum(str(run["parent_run_id"]) not in known_ids for run in runs
                            if run.get("parent_run_id")),
        "unfinished_count": sum(_duration(run) is None for run in runs),
        "llm_calls": sum(row["type"] == "llm" for row in rows),
        "cache_hits": sum(row["cache_hit"] is True for row in helpers),
        "retries": sum(max(0, (row["attempts"] or 0) - 1) for row in helpers),
        "failed_spans": sum(row["failed"] for row in rows),
        "runs": rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trace-id", type=UUID, required=True)
    args = parser.parse_args()
    from app.agent.observability import close, get_client
    from app.config import settings

    client = get_client()
    if client is None:
        parser.error("Set LANGSMITH_TRACING=true and LANGSMITH_API_KEY in backend/.env")
    try:
        runs = [run.model_dump() for run in client.list_runs(
            project_name=settings.LANGSMITH_PROJECT, trace_id=args.trace_id,
        )]
        if not runs:
            parser.error("Trace not found; check project/region and wait for upload to finish")
        print(json.dumps(summarize(runs), ensure_ascii=False, indent=2))
        return 0
    finally:
        close()


if __name__ == "__main__":
    raise SystemExit(main())
