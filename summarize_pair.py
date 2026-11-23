"""Compute attempt-level results and token usage from saved Stage 1 traces."""
import argparse
from collections import defaultdict
import json
from pathlib import Path
import random
from statistics import median

from retrieval import json_request
from scale_xerj import save
from stage1_benchmark import LLAMA, stats

TOKENIZE = LLAMA.replace("/v1/chat/completions", "/tokenize")


def paired_ci(groups, draws=10000):
    """Resample task clusters; each cluster can contain repeats and translations."""
    if len(groups) < 2:
        return None
    rng = random.Random(73)
    output = []
    values = list(groups.values())
    for _ in range(draws):
        sample = [pair for group in rng.choices(values, k=len(values)) for pair in group]
        output.append(median(x for x, _ in sample) - median(y for _, y in sample))
    output.sort()
    return [output[draws//40], output[draws-draws//40-1]]


def score(inputs, tokenize=False):
    rows = []
    corpus_search = defaultdict(list)
    for path in inputs:
        report = json.loads(path.read_text())
        for query in report["search"]:
            for platform, event in query["arms"].items():
                corpus_search[platform].append(event)
        for platform, tasks in report["arms"].items():
            for name, attempt in tasks.items():
                phases = [attempt] if name == "read" else [attempt["writer"], attempt["reader"]]
                usage = {key: sum(call.get(key, 0) for phase in phases for call in phase["usage"])
                         for key in ("prompt_tokens", "completion_tokens")}
                event_payloads = defaultdict(lambda: {"calls": 0, "tokens": 0})
                for phase in phases:
                    for event in phase["trace"]:
                        measurements = event_payloads[event["tool"]]
                        measurements["calls"] += 1
                        if tokenize:
                            value = event.get("result", {"error": event.get("error")})
                            content = json.dumps(value, ensure_ascii=False)
                            measurements["tokens"] += len(json_request(
                                TOKENIZE, {"content": content, "add_special": False})["tokens"])
                        else:
                            measurements["tokens"] = None
                rows.append({"fixture": report["fixture"], "case": report.get("case", "orbit-eu"),
                             "source_file": path.name, "arm": platform, "task": name,
                             "passed": attempt["passed"], "ms": attempt["ms"], **usage,
                             "tool_payloads": dict(event_payloads)})
    search = {platform: {"queries": len(items), "errors": sum("error" in e for e in items),
                         "hit5": sum(e["hit5"] for e in items),
                         "mean_recall5": sum(e["recall5"] for e in items) / len(items),
                         **stats(e["ms"] for e in items)} for platform, items in corpus_search.items()}
    results = {}
    for task in ("read", "edit"):
        current = [r for r in rows if r["task"] == task]
        by_arm = {}
        for platform in ("xerj", "confluence"):
            entries = [r for r in current if r["arm"] == platform]
            success = sum(r["passed"] for r in entries)
            prompt = sum(r["prompt_tokens"] for r in entries)
            generated = sum(r["completion_tokens"] for r in entries)
            by_arm[platform] = {"attempts": len(entries), "successful": success,
                                "prompt_tokens": prompt, "completion_tokens": generated,
                                "tokens_per_success": (prompt+generated)/success if success else None,
                                **stats(r["ms"] for r in entries)}
        pairs = defaultdict(dict)
        for item in current:
            pairs[(item["case"], item["fixture"])][item["arm"]] = item
        groups = defaultdict(list)
        pair_tokens = defaultdict(int)
        for (case, _), pair in pairs.items():
            if all(pair.get(arm, {}).get("passed") for arm in ("xerj", "confluence")):
                groups[case].append((pair["xerj"]["ms"], pair["confluence"]["ms"]))
                for arm, item in pair.items():
                    pair_tokens[arm] += item["prompt_tokens"] + item["completion_tokens"]
        differences = [t for group in groups.values() for t in group]
        results[task] = {"arms": by_arm, "shared_successes": len(differences),
                         "token_totals_shared_successes": dict(pair_tokens),
                         "shared_success_xerj": stats(x for x, _ in differences),
                         "shared_success_confluence": stats(y for _, y in differences),
                         "p50_difference_ci95_ms": paired_ci(groups)}
    return {"tasks": results, "search": search, "attempts": rows,
            "tool_payload_tokens_counted_separately": tokenize}


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("inputs", type=Path, nargs="+")
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--tokenize", action="store_true")
    args = p.parse_args()
    save(args.output, score(args.inputs, args.tokenize))
