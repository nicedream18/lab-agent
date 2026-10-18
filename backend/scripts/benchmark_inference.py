"""Warm local inference benchmark: singleton vs micro-batched requests, no LLM/DB."""

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import platform
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def measure(predict, inputs, batch_size, workers, wait_ms, repeat):
    from app.common.batching import MicroBatcher

    # Warm each shape outside measurements. This also excludes model loading.
    predict(inputs[:min(batch_size, workers, len(inputs))])
    batcher = MicroBatcher(predict, batch_size=batch_size, wait_ms=wait_ms,
                          max_pending=max(512, batch_size), name="benchmark")
    latencies = []
    outputs = []
    seconds = 0.0
    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for _ in range(repeat):
                def request(item):
                    started = time.perf_counter()
                    result = batcher.map([item])[0]
                    return result, (time.perf_counter() - started) * 1000

                started = time.perf_counter()
                results = list(pool.map(request, inputs))
                seconds += time.perf_counter() - started
                latencies.extend(elapsed for _, elapsed in results)
                outputs = [result for result, _ in results]
        import numpy as np

        stats = batcher.stats()
        return {
            "batch_size_limit": batch_size, "workers": workers,
            "items": len(inputs) * repeat, "seconds": seconds,
            "items_per_second": len(inputs) * repeat / seconds,
            "p50_ms": float(np.percentile(latencies, 50)),
            "p99_ms": float(np.percentile(latencies, 99)),
            **stats,
        }, outputs
    finally:
        batcher.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kind", choices=("embedding", "reranker", "both"), default="both")
    parser.add_argument("--items", type=int, default=32)
    parser.add_argument("--repeat", type=int, default=2)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--wait-ms", type=float, default=3)
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--embedding-model", default=os.getenv("LAB_EMBEDDING_MODEL", "BAAI/bge-small-zh-v1.5"))
    parser.add_argument("--reranker-model", default=os.getenv("KB_RERANKER_MODEL", "BAAI/bge-reranker-v2-m3"))
    parser.add_argument("--allow-download", action="store_true")
    parser.add_argument("--output", type=Path, default=Path("profiles/inference-batching.json"))
    args = parser.parse_args()
    if min(args.items, args.repeat, args.workers, args.batch_size) < 1 or args.wait_ms < 0:
        parser.error("counts must be positive and wait-ms nonnegative")
    if not args.allow_download:
        os.environ["HF_HUB_OFFLINE"] = "1"
        os.environ["TRANSFORMERS_OFFLINE"] = "1"
    os.environ.setdefault("ANONYMIZED_TELEMETRY", "false")
    from evals.offline_env import configure_offline

    configure_offline()
    import importlib.metadata
    import numpy as np
    import torch
    from sentence_transformers import CrossEncoder, SentenceTransformer

    queries = ["实验室危险化学品如何保管？", "预约实验室需要哪些手续？",
               "实验室发生火灾应该怎样处理？", "设备使用后有哪些检查要求？"]
    paragraphs = [
        "危险化学品应分类存放，专人管理，领用应登记。管制化学品实行双人验收、双人保管、双人领用、双把锁、双本账。保持通风，远离火源，实验结束清点归还。",
        "实验室预约应提前提交申请，说明使用时间、设备和实验目的。申请人完成安全培训后，由管理员审核。使用结束按时归还设备，清理场地并关闭水电。",
        "发现火灾应立即报告管理员并报警，优先组织人员沿安全通道撤离。确保自身安全后切断电源，根据火源选择适用的消防器材，不得盲目使用水灭火。",
        "使用仪器前检查电源、接地及防护装置，按操作规程使用，发现异常立即停机报告。实验结束清洁设备，填写使用记录，并确认设备、电源和门窗状态。",
    ]
    report = {"created_at": datetime.now(timezone.utc).isoformat(),
              "python": platform.python_version(), "platform": platform.platform(),
              "sentence_transformers": importlib.metadata.version("sentence-transformers"),
              "torch": torch.__version__, "torch_threads": torch.get_num_threads(),
              "device": args.device, "wait_ms": args.wait_ms,
              "inputs_per_repeat": args.items, "repeat": args.repeat,
              "latency_scope": "worker entry to inference result; excludes executor backlog",
              "results": {}}
    for kind in ("embedding", "reranker"):
        if args.kind not in (kind, "both"):
            continue
        if kind == "embedding":
            model = SentenceTransformer(args.embedding_model, device=args.device,
                                        local_files_only=not args.allow_download)
            inputs = [queries[index % len(queries)] for index in range(args.items)]
            report["embedding_model"] = args.embedding_model

            def make_predict(size):
                return lambda texts: model.encode(texts, batch_size=size,
                    normalize_embeddings=True, convert_to_numpy=True, show_progress_bar=False)
        else:
            model = CrossEncoder(args.reranker_model, device=args.device, max_length=512,
                                 local_files_only=not args.allow_download, trust_remote_code=False)
            inputs = [(queries[index % 4], paragraphs[(index // 4) % 4]) for index in range(args.items)]
            report["reranker_model"] = args.reranker_model
            report["reranker_max_length"] = 512

            def make_predict(size):
                return lambda pairs: model.predict(pairs, batch_size=size,
                                                    convert_to_numpy=True, show_progress_bar=False)

        baseline, expected = measure(make_predict(1), inputs, 1, args.workers, args.wait_ms, args.repeat)
        batched, actual = measure(make_predict(args.batch_size), inputs, args.batch_size,
                                  args.workers, args.wait_ms, args.repeat)
        equivalent = bool(np.allclose(expected, actual, rtol=1e-4, atol=1e-5))
        report["results"][kind] = {"baseline": baseline, "batched": batched,
            "speedup": batched["items_per_second"] / baseline["items_per_second"],
            "outputs_allclose": equivalent}
        print(json.dumps({kind: report["results"][kind]}, ensure_ascii=False), flush=True)
        # Keep only one kind of model resident during the benchmark.
        del model
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(f"Report: {args.output}")
    return 0 if all(row["outputs_allclose"] for row in report["results"].values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
