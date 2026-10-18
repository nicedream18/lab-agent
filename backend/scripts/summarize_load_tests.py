"""Compare final Locust snapshots; keep setup traffic and endpoint quantiles separate."""

import argparse
import csv
import json
from pathlib import Path
from statistics import median

API_PATHS = ("/api/lab/list", "/api/equipment/list", "/api/user/info")


def read_run(directory: Path) -> dict:
    manifest = json.loads((directory / "run.json").read_text())
    if manifest.get("scenario") not in ("api", "rag"):
        raise ValueError("this comparison supports api/rag workloads")
    if manifest.get("exit_code") != 0:
        raise ValueError(f"load test did not finish successfully: {directory}")
    final = directory / "final-stats.csv"
    if final.is_file():
        with final.open(newline="") as handle:
            stats = [{"method": row["Type"], "name": row["Name"],
                      "requests": int(row["Request Count"]), "failures": int(row["Failure Count"]),
                      "qps": float(row["Requests/s"]), "mean_ms": float(row["Average Response Time"]),
                      "p50_ms": float(row["50%"]), "p95_ms": float(row["95%"]),
                      "p99_ms": float(row["99%"])} for row in csv.DictReader(handle)]
        source = "final-stats.csv"
    else:
        # Historical periodic CSVs ended before drain. Their HTML has the final
        # request counts/quantiles, so never silently prefer results_stats.csv.
        html = (directory / "report.html").read_text()
        marker = "window.templateArgs = "
        start = html.index(marker) + len(marker)
        report = json.JSONDecoder().raw_decode(html[start:])[0]
        stats = [{"method": row["method"], "name": row["name"],
                  "requests": row["num_requests"], "failures": row["num_failures"],
                  "qps": row["total_rps"], "mean_ms": row["avg_response_time"],
                  "p50_ms": row["median_response_time"],
                  "p95_ms": row["response_time_percentile_0.95"],
                  "p99_ms": row["response_time_percentile_0.99"]}
                 for row in report["requests_statistics"]]
        source = "report.html"
    names = API_PATHS if manifest["scenario"] == "api" else ("/__loadtest/rag",)
    method = "GET" if manifest["scenario"] == "api" else "POST"
    routes = {row["name"]: row for row in stats if row["method"] == method and row["name"] in names}
    if set(routes) != set(names) or not sum(row["requests"] for row in routes.values()):
        raise ValueError(f"missing workload statistics: {directory}")
    if any(row["failures"] for row in stats):
        raise ValueError(f"failed requests in {directory}")
    requests = sum(row["requests"] for row in routes.values())
    qps = sum(row["qps"] for row in routes.values())
    if qps <= 0:
        raise ValueError("no measurable workload throughput")
    return {"directory": str(directory), "source": source, "config": manifest,
            "requests": requests, "statistics_window_seconds": requests / qps,
            "failures": sum(row["failures"] for row in routes.values()),
            "qps": qps, "routes": routes}


def values_summary(values) -> dict:
    values = list(values)
    return {"median": median(values), "min": min(values), "max": max(values)}


def comparison(before: list[dict], after: list[dict]) -> dict:
    fields = ("scenario", "users", "spawn_rate", "seconds", "think_time", "target", "real_llm")

    def key(run):
        return tuple(run["config"][field] for field in fields)

    keys = set(map(key, before)) | set(map(key, after))
    groups = []
    for scope in sorted(keys):
        old = [run for run in before if key(run) == scope]
        new = [run for run in after if key(run) == scope]
        if not old or not new:
            raise ValueError("unmatched load configuration; compare the same workload and load settings")
        if scope[0] == "rag":
            # Batch limits are the experiment variable; everything else stays fixed.
            fingerprints = [tuple(json.dumps(run["config"]["inference_before"][field], sort_keys=True)
                                  for field in ("embedding_model", "reranker_model", "device",
                                                "torch_threads", "wait_ms", "chunks", "documents"))
                            for run in old + new]
            if len(set(fingerprints)) != 1:
                raise ValueError("RAG model, hardware or documents differ between runs")
        old_qps = values_summary(run["qps"] for run in old)
        new_qps = values_summary(run["qps"] for run in new)
        routes = {}
        for name in old[0]["routes"]:
            routes[name] = {}
            for metric in ("mean_ms", "p50_ms", "p95_ms", "p99_ms"):
                routes[name][metric] = {
                    "before": values_summary(run["routes"][name][metric] for run in old),
                    "after": values_summary(run["routes"][name][metric] for run in new),
                }
        groups.append({"scope": dict(zip(fields, scope)), "before_runs": len(old),
                       "after_runs": len(new), "before_qps": old_qps, "after_qps": new_qps,
                       "qps_change_percent": (new_qps["median"] / old_qps["median"] - 1) * 100,
                       "before_requests": sum(run["requests"] for run in old),
                       "after_requests": sum(run["requests"] for run in new),
                       "before_failures": sum(run["failures"] for run in old),
                       "after_failures": sum(run["failures"] for run in new), "routes": routes})
    return {"before": before, "after": after, "comparison": groups}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--before", type=Path, nargs="+", required=True)
    parser.add_argument("--after", type=Path, nargs="+", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = comparison([read_run(path) for path in args.before],
                        [read_run(path) for path in args.after])
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(result["comparison"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
