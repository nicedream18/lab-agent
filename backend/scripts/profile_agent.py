"""Profiling entry points. Run from backend; see docs/performance.md."""

import argparse
import cProfile
import json
import os
from pathlib import Path
import pstats
import shlex
import shutil
import statistics
import subprocess
import sys
import tempfile
import time


BACKEND = Path(__file__).resolve().parents[1]


def positive(value):
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return number


def timing_summary(samples):
    ordered = sorted(samples)
    # Nearest-rank P95; keep the individual samples for small runs.
    return {
        "count": len(samples),
        "total_ms": sum(samples),
        "mean_ms": statistics.mean(samples),
        "p50_ms": statistics.median(samples),
        "p95_ms": ordered[(95 * len(samples) + 99) // 100 - 1],
        "min_ms": ordered[0],
        "max_ms": ordered[-1],
    }


def write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def search_once(query):
    # Keep import/initialization inside the first cold sample.
    from app.services import kb_service

    if not kb_service.search(query).strip():
        raise RuntimeError("Knowledge base returned no context; check the local index")


def run_kb_queries(query, repeat):
    """Stable root for SnakeViz; run in the same thread as the searches."""
    samples = []
    for _ in range(repeat):
        started = time.perf_counter()
        search_once(query)
        samples.append((time.perf_counter() - started) * 1000)
    return samples


def validate_kb_profile(stats, repeat):
    issues = []
    for function, filename, expected in (
        ("run_kb_queries", "profile_agent.py", 1),
        ("search", "kb_service.py", repeat),
    ):
        rows = [row for key, row in stats.stats.items()
                if key[2] == function and Path(key[0]).name == filename]
        if len(rows) != 1 or rows[0][1] != expected:
            issues.append(f"{function}: expected {expected} calls, got "
                          f"{[row[1] for row in rows]}")
        elif function == "search" and not any(
            edge[0] > 0 for edge in rows[0][4].values()
        ):
            issues.append("search: missing caller relationship")
    empty_edges = sum(
        edge == (0, 0, 0.0, 0.0)
        for row in stats.stats.values() for edge in row[4].values()
    )
    if empty_edges:
        issues.append(f"{empty_edges} empty caller relationships")
    return {"valid": not issues, "issues": issues}


def kb_worker(args, output):
    if not args.cold:
        search_once(args.query)
    profiler = None
    try:
        started = time.perf_counter()
        if args._phase == "profile":
            # CPython 3.13.9 + this Torch stack loses active frames with C-call
            # events enabled. Native time still accrues to Python wrappers.
            profiler = cProfile.Profile(builtins=False)
            samples = profiler.runcall(run_kb_queries, args.query, args.repeat)
        else:
            samples = run_kb_queries(args.query, args.repeat)
        elapsed = (time.perf_counter() - started) * 1000
        result = {"wall_ms": elapsed, "samples_ms": samples,
                  "summary": timing_summary(samples)}
        if profiler is not None:
            stats = pstats.Stats(profiler)
            result["validation"] = validate_kb_profile(stats, args.repeat)
            result["recorded_ms"] = stats.total_tt * 1000
            if not result["validation"]["valid"]:
                raise RuntimeError("Incomplete profile: "
                                   + "; ".join(result["validation"]["issues"]))
            profiler.dump_stats(str(output))
        write_json(args._result, result)
    except BaseException:
        if profiler is not None:
            profiler.dump_stats(str(output.with_suffix(".failed.prof")))
        raise


def profile_kb(args, output):
    # Both passes use fresh processes, so the baseline cannot warm the cold
    # profile, and cProfile instrumentation cannot affect baseline timings.
    result = {"status": "running", "baseline_mode": "cold" if args.cold else "warm",
              "profile_mode": "warm",
              "repeat": args.repeat, "python": sys.version,
              "builtins": False}
    metadata = output.with_suffix(".json")
    try:
        with tempfile.TemporaryDirectory(prefix="lab-profile-") as directory:
            for phase in ("baseline", "profile"):
                phase_result = Path(directory) / f"{phase}.json"
                command = [sys.executable, str(Path(__file__).resolve()), "kb",
                           "--query", args.query, "--repeat", str(args.repeat),
                           "--output", str(output), "--_phase", phase,
                           "--_result", str(phase_result)]
                # Cold imports also produce corrupt call events in this native
                # stack. Time initialization without instrumentation; keep the
                # function profile warm and label both phases explicitly.
                if args.cold and phase == "baseline":
                    command.append("--cold")
                print(f"Running {phase} ({result[phase + '_mode']})…", flush=True)
                subprocess.run(command, check=True)
                result[phase] = json.loads(phase_result.read_text())
        result["status"] = "success"
    except BaseException:
        result["status"] = "failed"
        write_json(metadata, result)
        raise
    write_json(metadata, result)
    print_kb_summary(result)
    print(f"Profile: {output}\nMetrics: {metadata}")


def print_kb_summary(result):
    for phase in ("baseline", "profile"):
        if phase in result:
            summary = result[phase]["summary"]
            print(f"{phase} ({result.get(phase + '_mode', 'warm')}): "
                  f"mean={summary['mean_ms']:.3f} ms, "
                  f"P50={summary['p50_ms']:.3f} ms, "
                  f"P95={summary['p95_ms']:.3f} ms")
    if "profile" in result:
        print(f"Call graph validation: {result['profile']['validation']}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    kb = commands.add_parser("kb", help="cProfile real RAG searches")
    kb.add_argument("--query", default="实验室危险化学品如何管理？")
    kb.add_argument("--repeat", type=positive, help="default: 20 warm, 1 cold")
    kb.add_argument("--cold", action="store_true",
                    help="measure initialization in baseline; function profile stays warm")
    kb.add_argument("--output", default="profiles/kb.prof")
    kb.add_argument("--_phase", choices=("baseline", "profile"), help=argparse.SUPPRESS)
    kb.add_argument("--_result", help=argparse.SUPPRESS)
    report = commands.add_parser("report", help="print cProfile function statistics")
    report.add_argument("path")
    report.add_argument("--limit", type=positive, default=30)
    spy = commands.add_parser("spy", help="py-spy: attach or start single-worker API")
    spy.add_argument("--pid", type=positive)
    spy.add_argument("--duration", type=positive, default=60)
    spy.add_argument("--rate", type=positive, default=100)
    spy.add_argument("--port", type=positive, default=8000)
    spy.add_argument("--idle", action="store_true")
    spy.add_argument("--native", action="store_true")
    spy.add_argument("--output", default="profiles/agent.svg")
    chat = commands.add_parser("chat", help="measure authenticated Agent SSE requests")
    chat.add_argument("--base-url", default="http://127.0.0.1:8000")
    chat.add_argument("--username", help="login; password is prompted without echo")
    chat.add_argument("--query", default="实验室危险化学品如何管理？")
    chat.add_argument("--repeat", type=positive, default=1)
    chat.add_argument("--timeout", type=positive, default=180)
    chat.add_argument("--output", default="profiles/chat.json")
    args = parser.parse_args()
    if args.command == "kb":
        args.repeat = args.repeat or (1 if args.cold else 20)
        if Path(args.output).suffix != ".prof":
            parser.error("kb --output must end in .prof")
    os.chdir(BACKEND)
    sys.path.insert(0, str(BACKEND))

    if args.command == "report":
        metadata = Path(args.path).with_suffix(".json")
        if metadata.is_file():
            result = json.loads(metadata.read_text())
            if result.get("status") != "success":
                parser.error("Last profiling run failed; do not use this report as a baseline")
            print_kb_summary(result)
        stats = pstats.Stats(args.path).strip_dirs()
        if any(edge == (0, 0, 0.0, 0.0)
               for row in stats.stats.values() for edge in row[4].values()):
            print("WARNING: incomplete caller data; regenerate with the updated kb command")
        for order in ("cumulative", "tottime"):
            stats.sort_stats(order).print_stats(args.limit)
        return

    if args.command == "spy" and sys.platform == "darwin" and os.geteuid() != 0:
        parser.error(
            "macOS requires root for py-spy. Start the API as your normal user in "
            "another terminal, then attach to its Python worker PID:\n"
            "sudo " + shlex.quote(str(Path(sys.executable).parent / "py-spy"))
            + " record --pid <PID> --duration " + str(args.duration)
            + " --output /tmp/lab-agent-profile.svg"
        )

    output = Path(args.output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    if args.command == "chat":
        from scripts.measure_agent import run_chat

        return run_chat(args, output)
    if args.command == "spy":
        sibling = Path(sys.executable).parent / "py-spy"
        executable = str(sibling) if sibling.is_file() else shutil.which("py-spy")
        if not executable:
            parser.error("py-spy missing: install requirements-profile.txt in the active venv")
        command = [executable, "record", "--rate", str(args.rate),
                   "--duration", str(args.duration), "--output", str(output)]
        for flag in ("idle", "native"):
            if getattr(args, flag):
                command.append("--" + flag)
        if args.pid:
            command.extend(["--pid", str(args.pid)])
        else:
            command.extend(["--", sys.executable, "-m", "uvicorn", "app.main:app",
                            "--host", "127.0.0.1", "--port", str(args.port),
                            "--workers", "1", "--loop", "asyncio"])
        os.execv(executable, command)

    if args._phase:
        return kb_worker(args, output)
    profile_kb(args, output)


if __name__ == "__main__":
    raise SystemExit(main())
