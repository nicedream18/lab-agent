"""Final snapshots, comparable workload scopes and endpoint percentile reporting."""

import csv
import json

import pytest

from scripts.summarize_load_tests import API_PATHS, comparison, read_run


def make_run(path, *, legacy=False):
    path.mkdir()
    manifest = {"host": "http://127.0.0.1:12345", "scenario": "api", "users": 5,
                "spawn_rate": 1, "seconds": 20, "think_time": 0.0,
                "target": "temporary SQLite API baseline", "real_llm": False, "exit_code": 0}
    (path / "run.json").write_text(json.dumps(manifest))
    routes = [{"method": "POST", "name": "/api/auth/login [setup]", "num_requests": 5,
               "num_failures": 0, "total_rps": 10, "avg_response_time": 180,
               "median_response_time": 180, "response_time_percentile_0.95": 200,
               "response_time_percentile_0.99": 210}]
    routes += [{"method": "GET", "name": name, "num_requests": 100, "num_failures": 0,
                "total_rps": 100, "avg_response_time": 3, "median_response_time": 3,
                "response_time_percentile_0.95": index + 4,
                "response_time_percentile_0.99": index + 6} for index, name in enumerate(API_PATHS)]
    if legacy:
        (path / "report.html").write_text("<script>window.templateArgs = " + json.dumps(
            {"requests_statistics": routes}) + ";</script>")
        # Must not use an earlier periodic snapshot instead of final HTML.
        (path / "results_stats.csv").write_text("stale periodic stats")
    else:
        headers = ("Type", "Name", "Request Count", "Failure Count", "Requests/s",
                   "Average Response Time", "50%", "95%", "99%")
        keys = ("method", "name", "num_requests", "num_failures", "total_rps",
                "avg_response_time", "median_response_time", "response_time_percentile_0.95",
                "response_time_percentile_0.99")
        with (path / "final-stats.csv").open("w", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(headers)
            writer.writerows([row[key] for key in keys] for row in routes)
    return read_run(path)


def test_legacy_html_and_final_csv_agree_and_exclude_login(tmp_path):
    old = make_run(tmp_path / "old", legacy=True)
    new = make_run(tmp_path / "new")
    assert old["source"] == "report.html" and new["source"] == "final-stats.csv"
    assert old["requests"] == new["requests"] == 300
    assert old["qps"] == new["qps"] == 300
    assert old["routes"] == new["routes"]


def test_comparison_reports_median_of_run_quantiles_per_endpoint(tmp_path):
    old = make_run(tmp_path / "old", legacy=True)
    runs = [make_run(tmp_path / f"new-{i}") for i in range(3)]
    for index, run in enumerate(runs):
        run["qps"] = [310, 330, 320][index]
        run["routes"][API_PATHS[0]]["p99_ms"] = [10, 5, 8][index]
    result = comparison([old], runs)["comparison"][0]
    assert result["after_qps"] == {"median": 320, "min": 310, "max": 330}
    assert result["routes"][API_PATHS[0]]["p99_ms"]["after"] == {"median": 8, "min": 5, "max": 10}
    assert "p99_ms" not in result  # Endpoint quantiles cannot be averaged into API P99.
    assert result["qps_change_percent"] == pytest.approx(6.6666667)


def test_mismatched_load_and_failed_run_are_rejected(tmp_path):
    old = make_run(tmp_path / "old")
    new = make_run(tmp_path / "new")
    new["config"]["seconds"] = 60
    with pytest.raises(ValueError, match="unmatched"):
        comparison([old], [new])
    config = new["config"]
    config["exit_code"] = 1
    (tmp_path / "new" / "run.json").write_text(json.dumps(config))
    with pytest.raises(ValueError, match="did not finish"):
        read_run(tmp_path / "new")
