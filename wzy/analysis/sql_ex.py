"""Downstream check: generate SQL from each method's linked schema and measure execution accuracy (EX).

Usage:
    python sql_ex.py --variants full gold exp1_mention exp2_query exp3_ambiguous llm_direct+fk
    python sql_ex.py --variants full gold --limit 5 --summary <tmp.json>     (smoke test)
    python sql_ex.py --variants full gold ... --summary-only                  (recompute summary)

Variants:
    full              whole database schema (no schema linking)
    gold              tables/columns read from the gold SQL (oracle linking)
    <run name>        final columns of a run in sl_lib.RUNS (exp1_mention, exp2_query, exp2b_tab0.1, ...)
    llm_direct+fk     qwen one-call linking plus FK closure
    union_exp2_llmfk  union of exp2_query and llm_direct+fk
    <run>~s<thr>      a run's recorded column Scores re-thresholded at <thr>, plus its forced FK columns
    <name>@<tag>      same input as <name>, separate output file (repeat run for a noise floor)

The generator sees exactly the kept columns (type and one sample value) and the FK pairs among them.
It is the LLM set by OPENAI_API_MODEL / OPENAI_API_BASE / OPENAI_API_KEY in .env, temperature 0,
thinking disabled. At most MAX_API_CONCURRENCY (5, the key's limit) generator requests are in flight;
SDK retries happen inside a worker, so they never add concurrency. Local SQLite execution has its own pool.
EX follows BIRD: result rows compared as sets; each query gets a 30 s and 200k-row budget.

Outputs:
    outputs/runs/sql_ex/<variant>.jsonl    one record per question; reruns skip ok records
    outputs/runs/sql_ex/gold_results.pkl   cached gold SQL results
    <this dir>/sql_ex_summary.json, sql_ex_tables.md
"""

from __future__ import annotations

import argparse
import json
import pickle
import random
import re
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from sl_lib import (
    DB_DIR,
    OUTPUTS,
    QUESTIONS,
    RUNS,
    SCHEMA,
    fk_closure,
    final_columns,
    forced_columns,
    load_gold,
    load_run,
    scored_columns,
)

from jev.config import load_environment, require_env
from jev.utils import message_text
from langchain_openai import ChatOpenAI

MAX_API_CONCURRENCY = 5
OUT_DIR = OUTPUTS / "runs" / "sql_ex"
HERE = Path(__file__).resolve().parent
TIMEOUT = 30.0
MAX_ROWS = 200_000

PROMPT = """Write one SQLite query that answers the question, using only the schema below.
Quote identifiers that contain spaces or special characters with backticks. Return only the SQL, with no explanation.

Schema (table(column type [sample], ...)):
{schema}

Foreign keys:
{fks}

Evidence:
{evidence}

Question:
{question}"""


def schema_index() -> dict:
    """db_id -> ordered tables with (name, type, sample) columns and FK pairs keyed by casefolded names."""
    index = {}
    for db in json.loads(SCHEMA.read_text(encoding="utf-8"))["databases"]:
        tables, fks = [], set()
        for t in db["tables"]:
            tables.append((t["name"], [(c["name"], c["type"], c.get("sample")) for c in t["columns"]]))
            for adj in t.get("adjacent_tables") or []:
                for child, parent in adj.get("foreign_keys") or []:
                    ckey = tuple(x.casefold() for x in child.rsplit(".", 1))
                    pkey = tuple(x.casefold() for x in parent.rsplit(".", 1))
                    fks.add((ckey, pkey, child, parent))
        index[db["db_id"]] = {"tables": tables, "fks": fks}
    return index


def render(db: dict, keep: set | None) -> tuple[str, str, int]:
    """Render only the kept (table, column) keys; keep=None renders the whole database."""
    lines, shown = [], 0
    for table, columns in db["tables"]:
        cols = [
            f"{name} {dtype}" + (f" [{str(sample)[:40]}]" if sample is not None else "")
            for name, dtype, sample in columns
            if keep is None or (table.casefold(), name.casefold()) in keep
        ]
        if cols:
            lines.append(f"{table}({', '.join(cols)})")
            shown += len(cols)
    fks = sorted(
        f"{child} = {parent}"
        for ckey, pkey, child, parent in db["fks"]
        if keep is None or (ckey in keep and pkey in keep)
    )
    return "\n".join(lines) or "(no columns selected)", "\n".join(fks) or "(none)", shown


def run_sql(db_id: str, sql: str) -> tuple[frozenset | None, str | None]:
    """Execute read-only with a time and row budget; return (rows as a set, None) or (None, error)."""
    uri = f"{(DB_DIR / db_id / f'{db_id}.sqlite').resolve().as_uri()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
    deadline = time.monotonic() + TIMEOUT
    conn.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 10_000)
    try:
        rows = conn.execute(sql).fetchmany(MAX_ROWS + 1)
        if len(rows) > MAX_ROWS:
            return None, f"RowLimit: more than {MAX_ROWS} rows"
        return frozenset(rows), None
    except sqlite3.OperationalError as exc:
        message = "Timeout" if "interrupted" in str(exc) else f"OperationalError: {str(exc)[:120]}"
        return None, message
    except Exception as exc:  # syntax errors, unsupported functions, etc.
        return None, f"{type(exc).__name__}: {str(exc)[:120]}"
    finally:
        conn.close()


def extract_sql(text: str) -> str:
    """Take the SQL out of a reply that may be wrapped in a code fence."""
    fenced = re.search(r"```(?:sql|sqlite)?\s*(.*?)```", text, flags=re.S | re.I)
    sql = fenced.group(1) if fenced else text
    return sql.strip().rstrip(";").strip()

def keep_sets(variant: str, gold: dict) -> dict:
    """question_id -> set of (table, column) keys to show, or None for the full schema."""
    base = variant.split("@", 1)[0]
    if "~s" in base:  # <run>~s<threshold>: recorded Scores with a new hard threshold, plus forced FK columns
        run, threshold = base.split("~s", 1)
        records = load_run(RUNS[run])[0]
        return {
            q: {k for k, (_, s) in scored_columns(r).items() if s >= float(threshold)} | forced_columns(r)
            for q, r in records.items()
        }
    if base == "full":
        return {q: None for q in gold}
    if base == "gold":
        return {q: g["columns"] for q, g in gold.items()}
    if base == "llm_direct+fk":
        return {q: final_columns(r) for q, r in fk_closure(load_run(RUNS["llm_direct"])[0]).items()}
    if base == "union_exp2_llmfk":
        a = {q: final_columns(r) for q, r in load_run(RUNS["exp2_query"])[0].items()}
        b = {q: final_columns(r) for q, r in fk_closure(load_run(RUNS["llm_direct"])[0]).items()}
        return {q: a[q] | b[q] for q in a.keys() & b.keys()}
    if base in RUNS:
        return {q: final_columns(r) for q, r in load_run(RUNS[base])[0].items()}
    raise SystemExit(f"unknown variant: {variant}")


def gold_results(qids: list, questions: dict, gold: dict, workers: int) -> dict:
    """Execute every gold SQL once; cached on disk because a few BIRD gold queries take several seconds."""
    cache = OUT_DIR / "gold_results.pkl"
    results = pickle.loads(cache.read_bytes()) if cache.exists() else {}
    todo = [q for q in qids if q not in results]
    if todo:
        with ThreadPoolExecutor(workers) as pool:
            for q, res in zip(todo, pool.map(lambda q: run_sql(gold[q]["db_id"], questions[q]["SQL"]), todo)):
                results[q] = res
        cache.write_bytes(pickle.dumps(results))
    return results


class Generator:
    """SQL generator with a hard cap on in-flight API requests."""

    def __init__(self, max_inflight: int):
        self.llm = ChatOpenAI(
            model=require_env("OPENAI_API_MODEL"),
            base_url=require_env("OPENAI_API_BASE"),
            api_key=require_env("OPENAI_API_KEY"),
            temperature=0.0,
            timeout=120,
            max_retries=0,  # retries are done below, inside the semaphore
            extra_body={"enable_thinking": False},
        )
        self.slots = threading.BoundedSemaphore(max_inflight)
        self.lock = threading.Lock()
        self.inflight = self.peak = self.calls = 0
        self.tokens_in = self.tokens_out = 0

    def __call__(self, prompt: str) -> tuple[str, dict]:
        last = None
        for attempt in range(5):
            with self.slots:
                with self.lock:
                    self.inflight += 1
                    self.calls += 1
                    self.peak = max(self.peak, self.inflight)
                try:
                    reply = self.llm.invoke([("human", prompt)])
                except Exception as exc:  # 429 / 5xx / network: back off outside the slot, then retry
                    last = exc
                    reply = None
                finally:
                    with self.lock:
                        self.inflight -= 1
            if reply is not None:
                usage = (reply.response_metadata or {}).get("token_usage") or {}
                with self.lock:
                    self.tokens_in += usage.get("prompt_tokens") or 0
                    self.tokens_out += usage.get("completion_tokens") or 0
                return message_text(reply.content), usage
            time.sleep(min(30.0, 2 ** attempt) + random.random())
        raise RuntimeError(f"generation failed after retries: {type(last).__name__}: {last}")

def load_records(path: Path) -> dict:
    """question_id -> last ok record in a variant file."""
    out = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                record = json.loads(line)
                if record.get("status") == "ok":
                    out[record["question_id"]] = record
    return out


def run_variant(variant, qids, keeps, questions, golds, index, generate, workers) -> None:
    path = OUT_DIR / f"{variant}.jsonl"
    done = load_records(path)
    todo = [q for q in qids if q not in done]
    print(f"{variant}: {len(qids) - len(todo)} done, {len(todo)} to run", flush=True)
    if not todo:
        return

    def solve(q: str) -> dict:
        item = questions[q]
        schema_text, fk_text, shown = render(index[item["db_id"]], keeps[q])
        prompt = (PROMPT.replace("{schema}", schema_text).replace("{fks}", fk_text)
                  .replace("{evidence}", item.get("evidence") or "(none)").replace("{question}", item["question"]))
        started = time.perf_counter()
        try:
            text, usage = generate(prompt)
        except Exception as exc:  # recorded as failed; the next run retries it
            return {"question_id": q, "variant": variant, "status": "failed", "error": str(exc)[:300]}
        sql = extract_sql(text)
        rows, error = run_sql(item["db_id"], sql)
        gold_rows, gold_error = golds[q]
        return {
            "question_id": q,
            "variant": variant,
            "status": "ok",
            "sql": sql,
            "exec_error": error,
            "gold_error": gold_error,
            "correct": gold_error is None and error is None and rows == gold_rows,
            "columns_shown": shown,
            "prompt_tokens": usage.get("prompt_tokens"),
            "completion_tokens": usage.get("completion_tokens"),
            "seconds": round(time.perf_counter() - started, 3),
        }

    finished = failed = 0
    with path.open("a", encoding="utf-8") as handle, ThreadPoolExecutor(workers) as pool:
        for future in as_completed([pool.submit(solve, q) for q in todo]):
            record = future.result()
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            handle.flush()
            finished += 1
            failed += record["status"] != "ok"
            if finished % 100 == 0 or finished == len(todo):
                print(f"  {variant}: {finished}/{len(todo)} failed={failed} peak_inflight={generate.peak}", flush=True)

def bootstrap(a: list[int], b: list[int], rounds: int = 2000, seed: int = 0) -> dict:
    """Paired bootstrap over questions for EX(a) - EX(b)."""
    rng = random.Random(seed)
    n = len(a)
    diffs = sorted(sum(a[i] - b[i] for i in (rng.randrange(n) for _ in range(n))) / n for _ in range(rounds))
    return {
        "n": n,
        "diff": (sum(a) - sum(b)) / n,
        "ci95": [diffs[int(0.025 * rounds)], diffs[int(0.975 * rounds) - 1]],
        "a_only_correct": sum(1 for x, y in zip(a, b) if x and not y),
        "b_only_correct": sum(1 for x, y in zip(a, b) if y and not x),
    }


CONTRASTS = [
    ("gold", "full"), ("exp1_mention", "full"), ("exp2_query", "full"), ("exp3_ambiguous", "full"),
    ("llm_direct+fk", "full"), ("union_exp2_llmfk", "full"), ("exp2b_tab0.1", "full"),
    ("exp2_query", "exp1_mention"), ("exp3_ambiguous", "exp2_query"), ("llm_direct+fk", "exp3_ambiguous"),
    ("union_exp2_llmfk", "exp2_query"), ("exp2b_tab0.1", "exp2_query"), ("gold", "exp3_ambiguous"),
    ("exp2_query@r2", "exp2_query"),
    ("exp2b_tab0.1~s1.0", "full"), ("exp2b_tab0.1~s0.5", "full"),
    ("exp2b_tab0.1~s0.5", "exp2b_tab0.1"),
]


def summarize(variants: list, qids: list, gold: dict, keeps: dict, out_dir: Path) -> dict:
    records = {v: load_records(out_dir / f"{v}.jsonl") for v in variants}
    common = [q for q in qids if all(q in records[v] for v in variants)]
    scored = [q for q in common if records[variants[0]][q]["gold_error"] is None]
    out = {"n_questions": len(qids), "n_common": len(common), "n_scored": len(scored),
           "gold_errors": len(common) - len(scored), "variants": {}, "contrasts": []}
    for v in variants:
        rs = [records[v][q] for q in scored]
        row = {
            "ex": sum(r["correct"] for r in rs) / len(rs),
            "exec_error_rate": sum(bool(r["exec_error"]) for r in rs) / len(rs),
            "timeouts": sum((r["exec_error"] or "").startswith("Timeout") for r in rs),
            "avg_columns_shown": sum(r["columns_shown"] for r in rs) / len(rs),
            "avg_prompt_tokens": sum(r.get("prompt_tokens") or 0 for r in rs) / len(rs),
            "missing_ok_records": len(qids) - len(records[v]),
        }
        for level in ("simple", "moderate", "challenging"):
            sub = [r for r in rs if gold[r["question_id"]]["difficulty"] == level]
            row[level] = {"n": len(sub), "ex": sum(r["correct"] for r in sub) / len(sub) if sub else None}
        keep = keeps[v]
        full = [r for r in rs if keep[r["question_id"]] is None or gold[r["question_id"]]["columns"] <= keep[r["question_id"]]]
        miss = [r for r in rs if r not in full]
        row["full_recall_share"] = len(full) / len(rs)
        row["ex_if_full_recall"] = sum(r["correct"] for r in full) / len(full) if full else None
        row["ex_if_missing"] = sum(r["correct"] for r in miss) / len(miss) if miss else None
        out["variants"][v] = row
    for a, b in CONTRASTS:
        if a in records and b in records:
            res = bootstrap([int(records[a][q]["correct"]) for q in scored], [int(records[b][q]["correct"]) for q in scored])
            out["contrasts"].append({"a": a, "b": b, **res})
    return out

LABELS = {
    "full": "全 schema（不做 linking）",
    "gold": "gold schema（oracle）",
    "exp1_mention": "实验1 选出的列",
    "exp2_query": "实验2 选出的列",
    "exp2_query@r2": "实验2 选出的列（第二次生成）",
    "exp3_ambiguous": "实验3 选出的列",
    "exp2b_tab0.1": "2b 选出的列（表阈值 0.1）",
    "llm_direct+fk": "qwen 选列 + 外键补全",
    "union_exp2_llmfk": "实验2 ∪ qwen+外键",
    "exp2b_tab0.1~s1.0": "2b 记录，列阈值 1.0",
    "exp2b_tab0.1~s0.5": "2b 记录，列阈值 0.5",
    "exp2_query~s2.0": "实验2 记录，列阈值 2.0（应与实验2 相同）",
}


def render_tables(s: dict) -> str:
    pct = lambda x: "-" if x is None else f"{100 * x:.1f}"
    lines = [
        f"共同题数 {s['n_common']}，其中 gold SQL 能执行的 {s['n_scored']} 题参与计分（gold 执行失败 {s['gold_errors']} 题）。",
        "",
        "| 生成器看到的 schema | EX | simple | moderate | challenging | 执行报错率 | 超时 | 平均列数 | 平均 prompt tokens | gold 列全召回的题占比 | 全召回题的 EX | 漏列题的 EX |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for v, r in s["variants"].items():
        lines.append(
            f"| {LABELS.get(v, v)} | {pct(r['ex'])} | {pct(r['simple']['ex'])} | {pct(r['moderate']['ex'])} | "
            f"{pct(r['challenging']['ex'])} | {pct(r['exec_error_rate'])} | {r['timeouts']} | {r['avg_columns_shown']:.1f} | "
            f"{r['avg_prompt_tokens']:.0f} | {pct(r['full_recall_share'])} | {pct(r['ex_if_full_recall'])} | {pct(r['ex_if_missing'])} |"
        )
    lines += ["", "| 对比（A − B） | ΔEX（百分点） | 95% CI | 仅 A 对 | 仅 B 对 |", "|---|---:|---|---:|---:|"]
    for c in s["contrasts"]:
        lines.append(
            f"| {LABELS.get(c['a'], c['a'])} − {LABELS.get(c['b'], c['b'])} | {100 * c['diff']:+.1f} | "
            f"[{100 * c['ci95'][0]:+.1f}, {100 * c['ci95'][1]:+.1f}] | {c['a_only_correct']} | {c['b_only_correct']} |"
        )
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--variants", nargs="+", required=True)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--api-workers", type=int, default=MAX_API_CONCURRENCY)
    parser.add_argument("--sql-workers", type=int, default=6)
    parser.add_argument("--summary", default=str(HERE / "sql_ex_summary.json"))
    parser.add_argument("--summary-only", action="store_true")
    args = parser.parse_args()
    if not 1 <= args.api_workers <= MAX_API_CONCURRENCY:
        raise SystemExit(f"--api-workers must be within 1..{MAX_API_CONCURRENCY}")

    load_environment()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    gold = load_gold()
    questions = {}
    for item in json.loads(QUESTIONS.read_text(encoding="utf-8")):
        questions.setdefault(str(item["question_id"]), item)
    keeps = {v: keep_sets(v, gold) for v in args.variants}
    qids = sorted(set(gold).intersection(*(set(k) for k in keeps.values())), key=int)
    if args.limit:
        qids = qids[: args.limit]
    print(f"{len(qids)} questions x {len(args.variants)} variants; api cap {args.api_workers}", flush=True)

    if not args.summary_only:
        golds = gold_results(qids, questions, gold, args.sql_workers)
        print("gold SQL errors:", sum(1 for q in qids if golds[q][1]), flush=True)
        generate = Generator(args.api_workers)
        index = schema_index()
        for variant in args.variants:
            run_variant(variant, qids, keeps[variant], questions, golds, index, generate, args.api_workers)
        print(f"generator calls {generate.calls}, peak in-flight {generate.peak}, "
              f"tokens in/out {generate.tokens_in}/{generate.tokens_out}", flush=True)

    summary = summarize(args.variants, qids, gold, keeps, OUT_DIR)
    Path(args.summary).write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    Path(args.summary).with_name(Path(args.summary).stem.replace("summary", "tables") + ".md").write_text(
        render_tables(summary), encoding="utf-8")
    for v, r in summary["variants"].items():
        print(f"{v:<20} EX {100 * r['ex']:.1f}  exec_err {100 * r['exec_error_rate']:.1f}  cols {r['avg_columns_shown']:.1f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
