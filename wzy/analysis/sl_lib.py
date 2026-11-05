"""Shared helpers for offline analysis of jev-sql schema-linking runs."""

from __future__ import annotations

import json
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REPO = ROOT
OUTPUTS = ROOT / "outputs"
QUESTIONS = REPO / "BIRD/MINIDEV/mini_dev_sqlite.json"
DB_DIR = REPO / "BIRD/MINIDEV/dev_databases"
SCHEMA = OUTPUTS / "schema/minidev_schema.json"
sys.path.insert(0, str(REPO))

from jev.evaluation import extract_sql_schema  # noqa: E402

RUNS = {
    "exp1_mention": "jev_minidev_table_first",
    "exp2_query": "jev_minidev_table_first_no_mention",
    "exp3_ambiguous": "jev_minidev_table_first_ambiguous",
    "llm_direct": "llm_direct_qwen",
    "exp2b_tab0.1": "jev_minidev_no_mention_tab0.1",
}


def load_gold() -> dict[str, dict]:
    """question_id -> gold tables/columns (casefolded), extracted with jev-sql's SQLite authorizer."""
    questions = json.loads(QUESTIONS.read_text(encoding="utf-8"))
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    db_columns = {db["db_id"]: sum(len(t["columns"]) for t in db["tables"]) for db in schema["databases"]}
    gold = {}
    for q in questions:
        tables, columns, error = extract_sql_schema(q["SQL"], DB_DIR / q["db_id"] / f"{q['db_id']}.sqlite")
        if error:
            continue
        gold[str(q["question_id"])] = {
            "db_id": q["db_id"],
            "difficulty": q["difficulty"],
            "tables": set(tables),
            "columns": set(columns),
            "db_columns": db_columns[q["db_id"]],
        }
    return gold


def load_run(run_dir: str) -> tuple[dict[str, dict], int]:
    """Last ok record per question_id; returns (records, ids with no ok record).

    The jev-sql evaluator keeps the last record instead. The two differ only when a duplicated
    question_id (137 and 138 appear twice in mini-dev) has an ok record followed by a failed one.
    """
    path = OUTPUTS / "runs" / run_dir / "batch_answers.jsonl"
    seen: set[str] = set()
    ok: dict[str, dict] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                record = json.loads(line)
                qid = str(record.get("question_id"))
                seen.add(qid)
                if record.get("status") == "ok":
                    ok[qid] = record
    return ok, len(seen - set(ok))


def key(table: str, column: str) -> tuple[str, str]:
    return table.casefold(), column.casefold()


def final_columns(record: dict) -> set[tuple[str, str]]:
    return {key(i["table"], i["column"]) for i in record.get("linked_columns_union") or []}


def forced_columns(record: dict) -> set[tuple[str, str]]:
    return {
        key(i["table"], i["column"])
        for i in record.get("linked_columns_union") or []
        if "forced_foreign_key" in (i.get("sources") or [])
    }


def scored_columns(record: dict) -> dict[tuple[str, str], tuple[str, float]]:
    """(table, column) -> (candidate_id, best Score over spans) for every column scored in step 2."""
    stage = record.get("stage_column") or {}
    spans = [m["span_id"] for m in record.get("mentions") or []]
    answers = stage.get("answers") or {}
    out = {}
    for info in (stage.get("tables") or {}).values():
        for column in info["columns"]:
            cid = column["candidate_id"]
            best = 0.0
            for span in spans:
                answer = answers.get(f"s2_{span}_{cid}")
                if isinstance(answer, dict) and isinstance(answer.get("score"), (int, float)):
                    best = max(best, float(answer["score"]))
            out[key(info["table"], column["column"])] = (cid, best)
    return out


def table_nouls(record: dict) -> dict[str, float]:
    return {c["table"].casefold(): float(c["noul"]) for c in (record.get("stage_table") or {}).get("candidates") or []}


def selected_tables(record: dict) -> set[str]:
    return {c["table"].casefold() for c in (record.get("stage_table") or {}).get("selected_tables") or []}


def ambiguous_nouls(record: dict) -> dict[tuple[str, str], float]:
    """Columns sent to the extra Noul round (exp3) -> their Noul probability."""
    stage = record.get("stage_ambiguous") or {}
    answers = stage.get("answers") or {}
    out = {}
    for c in stage.get("candidates") or []:
        value = (answers.get(f"s3_{c['candidate_id']}") or {}).get("noul")
        out[key(c["table"], c["column"])] = float(value) if isinstance(value, (int, float)) else 0.0
    return out


def fk_closure(records: dict) -> dict:
    """Add both columns of every schema FK between predicted tables (same rule the JEV pipeline forces)."""
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    links = {}
    for db in schema["databases"]:
        rows = set()
        for table in db["tables"]:
            for adj in table.get("adjacent_tables") or []:
                for child, parent in adj.get("foreign_keys") or []:
                    rows.add((tuple(child.rsplit(".", 1)), tuple(parent.rsplit(".", 1))))
        links[db["db_id"]] = rows
    out = {}
    for qid, record in records.items():
        union = list(record.get("linked_columns_union") or [])
        have = final_columns(record)
        tables = {t for t, _ in have}
        for (ct, cc), (pt, pc) in links[record["db_id"]]:
            if ct.casefold() in tables and pt.casefold() in tables and ct.casefold() != pt.casefold():
                for t, c in ((ct, cc), (pt, pc)):
                    if key(t, c) not in have:
                        have.add(key(t, c))
                        union.append({"table": t, "column": c, "sources": ["forced_foreign_key"]})
        out[qid] = {**record, "linked_columns_union": union}
    return out


def set_metrics(pairs: list[tuple[set, set]]) -> dict:
    """Micro P/R/F1 plus per-question full-recall and exact-match rates over (pred, gold) pairs."""
    tp = sum(len(p & g) for p, g in pairs)
    fp = sum(len(p - g) for p, g in pairs)
    fn = sum(len(g - p) for p, g in pairs)
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    n = len(pairs) or 1
    return {
        "n": len(pairs),
        "precision": precision,
        "recall": recall,
        "f1": 2 * precision * recall / (precision + recall) if precision + recall else 0.0,
        "full_recall": sum(g <= p for p, g in pairs) / n,
        "exact": sum(p == g for p, g in pairs) / n,
        "avg_pred": sum(len(p) for p, _ in pairs) / n,
        "avg_gold": sum(len(g) for _, g in pairs) / n,
    }


def auroc(scores: list[float], labels: list[int]) -> float | None:
    """Mann-Whitney AUROC with average ranks for ties."""
    order = sorted(range(len(scores)), key=lambda i: scores[i])
    ranks = [0.0] * len(scores)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and scores[order[j + 1]] == scores[order[i]]:
            j += 1
        for k in range(i, j + 1):
            ranks[order[k]] = (i + j) / 2 + 1
        i = j + 1
    pos = sum(labels)
    neg = len(labels) - pos
    if not pos or not neg:
        return None
    return (sum(r for r, y in zip(ranks, labels) if y) - pos * (pos + 1) / 2) / (pos * neg)


def calibration(probs: list[float], labels: list[int], bins: int = 10) -> dict:
    """ECE / Brier and a reliability table for probabilities against binary labels."""
    rows, ece = [], 0.0
    for b in range(bins):
        idx = [i for i, p in enumerate(probs) if b / bins <= p < (b + 1) / bins or (b == bins - 1 and p >= 1.0)]
        if not idx:
            continue
        conf = sum(probs[i] for i in idx) / len(idx)
        acc = sum(labels[i] for i in idx) / len(idx)
        ece += len(idx) / len(probs) * abs(acc - conf)
        rows.append({"bin": f"[{b / bins:.1f},{(b + 1) / bins:.1f})", "n": len(idx), "mean_prob": conf, "positive_rate": acc})
    brier = sum((p - y) ** 2 for p, y in zip(probs, labels)) / len(probs)
    return {"ece": ece, "brier": brier, "bins": rows}


def bootstrap_diff(pairs_a: list, pairs_b: list, metrics: tuple, rounds: int = 1000, seed: int = 0) -> dict:
    """Paired bootstrap over questions for metric(A) - metric(B); point estimate and 95% CI per metric."""
    rng = random.Random(seed)
    n = len(pairs_a)
    diffs = {m: [] for m in metrics}
    for _ in range(rounds):
        idx = [rng.randrange(n) for _ in range(n)]
        a = set_metrics([pairs_a[i] for i in idx])
        b = set_metrics([pairs_b[i] for i in idx])
        for m in metrics:
            diffs[m].append(a[m] - b[m])
    point_a, point_b = set_metrics(pairs_a), set_metrics(pairs_b)
    out = {}
    for m in metrics:
        values = sorted(diffs[m])
        out[m] = {"diff": point_a[m] - point_b[m], "ci95": [values[int(0.025 * rounds)], values[int(0.975 * rounds) - 1]]}
    return out
