"""Real same-name column disambiguation with JEV Choice (research memo step 2).

Usage:
    python choice_disambig.py --model jev --limit 12 --out <pilot.jsonl>   (pilot)
    python choice_disambig.py --model jev                                  (all 498 questions)
    python choice_disambig.py --model qwen                                 (same questions, one qwen call per question)
    python choice_disambig.py --keeps --thresholds 0.5,0.8                 (keep sets for sql_ex.py)
    python choice_disambig.py --report                                     (decision quality + keep-set stats)

For every question, columns whose name occurs in >= 2 tables are grouped. Each group becomes one
JEV Choice question: pick the member column the SQL generator needs, "all" when several members are
needed, or "none" when no column with that name is needed. The qwen variant answers the same
questions in one call per question, for comparison with JEV.

Decisions are applied to the full database schema: chosen member -> keep it, "all" -> keep every
member, "none" -> drop the group, unparseable answer -> keep every member (safe fallback). Prompts
and options are English. At most MAX_API_CONCURRENCY (5) API requests are in flight, retries happen
inside the slot.

Outputs:
    outputs/runs/choice_disambig/<model>.jsonl   one record per question; reruns skip ok records
    outputs/runs/choice_disambig/report.json, report.md
"""

from __future__ import annotations

import argparse
import json
import random
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from sl_lib import OUTPUTS, QUESTIONS, SCHEMA, auroc, load_gold, set_metrics
from sql_ex import Generator, full_columns, oracle_disambiguate

from jev.config import load_environment
from jev.jev_client import call_jev

MAX_API_CONCURRENCY = 5
JEV_MODEL = "typesafe-ai/jev-1.14.0"
OUT_DIR = OUTPUTS / "runs" / "choice_disambig"
HERE = Path(__file__).resolve().parent


def schema_index() -> dict:
    """db_id -> {groups: {name: [member]}, summary: str}."""
    index = {}
    for db in json.loads(SCHEMA.read_text(encoding="utf-8"))["databases"]:
        groups, summary = {}, []
        for t in db["tables"]:
            pk = {p.casefold() for p in t["primary_key"]}
            labels = []
            for c in t["columns"]:
                member = {
                    "table": t["name"],
                    "column": c["name"],
                    "type": c["type"],
                    "sample": c.get("sample"),
                    "primary_key": c["name"].casefold() in pk,
                }
                groups.setdefault(c["name"].casefold(), []).append(member)
                labels.append(c["name"] + " " + c["type"] + (" PK" if member["primary_key"] else ""))
            summary.append(f"{t['name']}({', '.join(labels)})")
        groups = {
            name: sorted(members, key=lambda m: m["table"].casefold())
            for name, members in groups.items()
            if len(members) >= 2
        }
        index[db["db_id"]] = {"groups": groups, "summary": "; ".join(summary)}
    return index


def member_option(member: dict) -> str:
    text = f'{member["table"]}.{member["column"]} ({member["type"]}'
    if member.get("sample") is not None:
        text += f", e.g. {str(member['sample'])[:40]!r}"
    if member.get("primary_key"):
        text += ", primary key"
    return text + ")"


def build_groups(db_info: dict) -> dict:
    """group_id -> {column_name, members: [(option_key, member)]} in a deterministic order."""
    out = {}
    for i, (name, members) in enumerate(sorted(db_info["groups"].items()), start=1):
        out[f"g{i}"] = {"column_name": name, "members": [(f"c{j}", m) for j, m in enumerate(members, start=1)]}
    return out


def build_payload(item: dict, db_info: dict) -> tuple[dict, dict, dict]:
    """JEV state, questions, and the group table for one question."""
    groups = build_groups(db_info)
    questions = {}
    for gid, group in groups.items():
        criteria = {key: member_option(m) for key, m in group["members"]}
        criteria["all"] = "More than one of these columns is needed; keep all of them."
        criteria["none"] = "No column with this name is needed; drop all of them."
        questions[gid] = {
            "type": "choice",
            "instructions": (
                f'Which column named "{group["column_name"]}" should the SQL generator see? The name appears '
                f'in {len(group["members"])} tables. Choose the option for the column the question needs, '
                '"all" if several of them are needed, or "none" if no column with this name is needed.'
            ),
            "criteria": criteria,
        }
    state = {
        "task": "same_name_column_disambiguation",
        "instruction": (
            "The schema has columns that share one name across several tables. The SQL generator will see "
            "exactly the columns kept here. For each group, decide which member column(s) of that group the "
            "question needs; keep no member when the name is not used at all. Answer only the provided questions."
        ),
        "context": {
            "database": item["db_id"],
            "question": item["question"],
            "evidence": item.get("evidence") or "",
            "schema_summary": db_info["summary"],
        },
        "groups": {
            gid: {"column_name": group["column_name"], "members": {key: member_option(m) for key, m in group["members"]}}
            for gid, group in groups.items()
        },
        "output_schema": {"choice": "one option key of the question's criteria"},
    }
    return state, questions, groups


def qwen_prompt(item: dict, db_info: dict, groups: dict) -> str:
    lines = [
        "The database schema has columns that share one name across several tables. The SQL generator "
        "will see exactly the columns you keep. For each group, decide which member column(s) the "
        "question needs; keep no member when the name is not used at all.",
        "",
        f"Database: {item['db_id']}",
        f"Question: {item['question']}",
        f"Evidence: {item.get('evidence') or ''}",
        "",
        "Schema:",
        db_info["summary"],
        "",
        "Groups:",
    ]
    for gid, group in groups.items():
        options = "; ".join(f"{key} = {member_option(m)}" for key, m in group["members"])
        lines.append(f'- {gid}: column name "{group["column_name"]}"; {options}; all = keep every member; none = keep none')
    lines += [
        "",
        "Return only one JSON object that maps every group id to one option key listed for that group, "
        'for example {"g1": "c2", "g2": "all", "g3": "none"}. No explanation.',
    ]
    return "\n".join(lines)


def apply_decisions(db_id: str, groups: dict, answers: dict) -> tuple[list, int]:
    """Keep set from decisions on the full schema; returns (sorted keys, unparsed answer count)."""
    keep = set(full_columns(db_id))
    bad = 0
    for gid, group in groups.items():
        answer = answers.get(gid)
        chosen = answer.get("choice") if isinstance(answer, dict) else answer
        member_keys = {key for key, _ in group["members"]}
        members = {(m["table"].casefold(), m["column"].casefold()) for _, m in group["members"]}
        if chosen in member_keys:
            chosen_member = next(m for key, m in group["members"] if key == chosen)
            keep = (keep - members) | {(chosen_member["table"].casefold(), chosen_member["column"].casefold())}
        elif chosen == "none":
            keep -= members
        elif chosen == "all":
            pass
        else:
            bad += 1  # keep every member (safe fallback)
    return sorted([[t, c] for t, c in keep]), bad


def extract_json(text: str):
    """Parse the first JSON object in a reply (optionally wrapped in a code fence)."""
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, flags=re.S | re.I)
    body = fenced.group(1) if fenced else text
    match = re.search(r"\{.*\}", body, flags=re.S)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return None


def solve_jev(item: dict, db_info: dict, generate) -> dict:
    state, questions, groups = build_payload(item, db_info)
    answers = generate(state, questions)
    keep, bad = apply_decisions(item["db_id"], groups, answers)
    return {"keep": keep, "answers": answers, "unparsed": bad}


def solve_qwen(item: dict, db_info: dict, generate) -> dict:
    state, questions, groups = build_payload(item, db_info)
    text, usage = generate(qwen_prompt(item, db_info, groups))
    raw = extract_json(text)
    answers = {}
    for gid in groups:
        value = raw.get(gid) if isinstance(raw, dict) else None
        if isinstance(value, dict):  # tolerate {"option": "c2"} style replies
            value = value.get("choice") or value.get("option") or value.get("answer")
        answers[gid] = {"choice": value if isinstance(value, str) else None}
    keep, bad = apply_decisions(item["db_id"], groups, answers)
    return {
        "keep": keep,
        "answers": answers,
        "unparsed": bad,
        "prompt_tokens": usage.get("prompt_tokens"),
        "completion_tokens": usage.get("completion_tokens"),
    }


class JevGenerator:
    """JEV calls with a hard in-flight cap and retries inside the slot."""

    def __init__(self, max_inflight: int):
        self.config = {
            "model": JEV_MODEL,
            "endpoint_env": "JEV_API_BASE",
            "api_key_env": "AI_GATEWAY_API_KEY",
            "timeout": 120,
        }
        self.slots = threading.BoundedSemaphore(max_inflight)
        self.lock = threading.Lock()
        self.inflight = self.peak = self.calls = 0
        self.tokens_in = self.tokens_out = 0

    def __call__(self, state: dict, questions: dict) -> dict:
        last = None
        for attempt in range(5):
            with self.slots:
                with self.lock:
                    self.inflight += 1
                    self.calls += 1
                    self.peak = max(self.peak, self.inflight)
                try:
                    reply = call_jev(state, questions, self.config)
                except Exception as exc:
                    last = exc
                    reply = None
                finally:
                    with self.lock:
                        self.inflight -= 1
            if reply is not None:
                usage = (reply.get("raw") or {}).get("usage") or {}
                with self.lock:
                    self.tokens_in += usage.get("input_tokens") or 0
                    self.tokens_out += usage.get("output_tokens") or 0
                return reply["answers"]
            time.sleep(min(30.0, 2 ** attempt) + random.random())
        raise RuntimeError(f"JEV call failed after retries: {type(last).__name__}: {last}")


def load_items() -> dict:
    """question_id -> question record (first occurrence; 137 and 138 are duplicated identically)."""
    items = {}
    for item in json.loads(QUESTIONS.read_text(encoding="utf-8")):
        items.setdefault(str(item["question_id"]), item)
    return items


def load_records(path: Path) -> dict:
    out = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                record = json.loads(line)
                if record.get("status") == "ok":
                    out[record["question_id"]] = record
    return out


def run(model: str, limit: int, out: Path) -> None:
    load_environment()
    items = load_items()
    index = schema_index()
    gold = load_gold()
    qids = sorted(set(gold) & set(items), key=int)[: limit or None]
    done = load_records(out)
    todo = [q for q in qids if q not in done]
    print(f"{model}: {len(qids) - len(todo)} done, {len(todo)} to run", flush=True)

    generate = JevGenerator(MAX_API_CONCURRENCY) if model == "jev" else Generator(MAX_API_CONCURRENCY)
    finished = failed = 0
    with out.open("a", encoding="utf-8") as handle, ThreadPoolExecutor(MAX_API_CONCURRENCY) as pool:
        futures = {}
        for q in todo:
            item, db_info = items[q], index[items[q]["db_id"]]
            solver = solve_jev if model == "jev" else solve_qwen
            futures[pool.submit(solver, item, db_info, generate)] = q
        for future in as_completed(futures):
            q = futures[future]
            try:
                record = {"question_id": q, "db_id": items[q]["db_id"], "status": "ok", **future.result()}
            except Exception as exc:
                record = {"question_id": q, "db_id": items[q]["db_id"], "status": "error",
                          "error": f"{type(exc).__name__}: {exc}"}
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            handle.flush()
            finished += 1
            failed += record["status"] != "ok"
            if finished % 50 == 0 or finished == len(todo):
                print(f"  {model}: {finished}/{len(todo)} failed={failed} peak_inflight={generate.peak}", flush=True)
    print(
        f"{model}: calls {generate.calls}, peak in-flight {generate.peak}, "
        f"tokens in/out {generate.tokens_in}/{generate.tokens_out}",
        flush=True,
    )


def group_label_sets(groups: dict, gold_columns: set) -> dict:
    """group_id -> (gold option keys, all member (table, column) keys, gold (table, column) keys)."""
    out = {}
    for gid, group in groups.items():
        tables = {(m["table"].casefold(), m["column"].casefold()) for _, m in group["members"]}
        gold_keys = {key for key, m in group["members"] if (m["table"].casefold(), m["column"].casefold()) in gold_columns}
        out[gid] = (gold_keys, tables)
    return out


def kept_keys_for(group: dict, chosen) -> set:
    member_keys = {key for key, _ in group["members"]}
    if chosen == "all":
        return member_keys
    if chosen in member_keys:
        return {chosen}
    return set()


def keeps_for_policy(records: dict, gold: dict, index: dict, items: dict, qids: list, threshold: float | None) -> dict:
    """Keep sets for one policy: threshold=None uses the recorded argmax decisions.

    With a confidence threshold, a group decision is applied only when its confidence reaches the
    threshold; otherwise every member is kept (over-keeping is benign, dropping a gold column is not).
    """
    out = {}
    for q in qids:
        if threshold is None:
            out[q] = sorted([list(k) for k in records[q]["keep"]])
            continue
        groups = build_groups(index[items[q]["db_id"]])
        keep = set(full_columns(gold[q]["db_id"]))
        for gid, group in groups.items():
            answer = records[q]["answers"].get(gid) or {}
            chosen, confidence = answer.get("choice"), answer.get("confidence") or 0.0
            if confidence < threshold:
                continue
            members = {(m["table"].casefold(), m["column"].casefold()) for _, m in group["members"]}
            member_keys = {key for key, _ in group["members"]}
            if chosen == "none":
                keep -= members
            elif chosen in member_keys:
                member = next(m for key, m in group["members"] if key == chosen)
                keep = (keep - members) | {(member["table"].casefold(), member["column"].casefold())}
        out[q] = sorted([list(k) for k in keep])
    return out


def dump_keeps(thresholds: list[float]) -> int:
    """Write keep_<model>_<tag>.json for the argmax and thresholded policies (read by sql_ex.py)."""
    gold = load_gold()
    index = schema_index()
    items = load_items()
    for model in ("jev", "qwen"):
        records = load_records(OUT_DIR / f"{model}.jsonl")
        qids = sorted(set(gold) & set(records), key=int)
        if not qids:
            print(f"{model}: no records yet", flush=True)
            continue
        policies = [("argmax", None)] + [(f"t{t}", t) for t in thresholds]
        for tag, threshold in policies:
            keeps = keeps_for_policy(records, gold, index, items, qids, threshold)
            path = OUT_DIR / f"keep_{model}_{tag}.json"
            path.write_text(json.dumps(keeps, ensure_ascii=False), encoding="utf-8")
            pairs = [({tuple(k) for k in keeps[q]}, gold[q]["columns"]) for q in qids]
            m = set_metrics(pairs)
            print(f"{model} {tag}: n {len(qids)} full_recall {100 * m['full_recall']:.1f} avg_cols {m['avg_pred']:.1f}", flush=True)
    return 0


def report() -> int:
    gold = load_gold()
    index = schema_index()
    items = load_items()
    records = {name: load_records(OUT_DIR / f"{name}.jsonl") for name in ("jev", "qwen")}
    qids = sorted(set(gold) & set(records["jev"]) & set(records["qwen"]), key=int)
    if not qids:
        raise SystemExit("no overlapping ok records; run both models first")

    out = {"n": len(qids), "models": {}}
    for name in records:
        rows = []
        pairs = [({tuple(k) for k in records[name][q]["keep"]}, gold[q]["columns"]) for q in qids]
        for q in qids:
            db_info = index[items[q]["db_id"]]
            groups = build_groups(db_info)
            labels = group_label_sets(groups, gold[q]["columns"])
            answers = records[name][q]["answers"]
            for gid, group in groups.items():
                answer = answers.get(gid) or {}
                chosen = answer.get("choice")
                kept = kept_keys_for(group, chosen)
                gold_keys, _ = labels[gid]
                rows.append({
                    "exact": int(kept == gold_keys),
                    "safe": int(gold_keys <= kept),
                    "confidence": answer.get("confidence"),
                    "choice": chosen or "none",
                })
        m = set_metrics(pairs)
        conf = [r["confidence"] if isinstance(r["confidence"], (int, float)) else None for r in rows]
        have_conf = [i for i, c in enumerate(conf) if c is not None]
        auroc_exact = auroc([conf[i] for i in have_conf], [rows[i]["exact"] for i in have_conf]) if len(have_conf) > 1 else None
        auroc_safe = auroc([conf[i] for i in have_conf], [rows[i]["safe"] for i in have_conf]) if len(have_conf) > 1 else None
        out["models"][name] = {
            "questions": len(qids),
            "group_decisions": len(rows),
            "exact_rate": sum(r["exact"] for r in rows) / len(rows),
            "safe_rate": sum(r["safe"] for r in rows) / len(rows),
            "auroc_confidence_exact": auroc_exact,
            "auroc_confidence_safe": auroc_safe,
            "keep_full_recall": m["full_recall"],
            "keep_avg_columns": m["avg_pred"],
            "keep_precision": m["precision"],
            "keep_recall": m["recall"],
            "none_chosen": sum(1 for r in rows if r["choice"] == "none"),
            "all_chosen": sum(1 for r in rows if r["choice"] == "all"),
            "unparsed": sum(1 for q in qids if records[name][q].get("unparsed")),
        }
    pairs = [(oracle_disambiguate(full_columns(gold[q]["db_id"]), gold[q]["columns"]), gold[q]["columns"]) for q in qids]
    mo = set_metrics(pairs)
    out["oracle"] = {"keep_full_recall": mo["full_recall"], "keep_avg_columns": mo["avg_pred"]}
    same = total = 0
    for q in qids:
        groups = build_groups(index[items[q]["db_id"]])
        for gid in groups:
            a = (records["jev"][q]["answers"].get(gid) or {}).get("choice")
            b = (records["qwen"][q]["answers"].get(gid) or {}).get("choice")
            same += int(a == b)
            total += 1
    out["agreement_jev_qwen"] = same / total

    (OUT_DIR / "report.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    lines = [
        f"共同题数 {len(qids)}；同名列组级决策与 keep-set 统计。",
        "",
        "| 模型 | 组数 | 精确匹配 | 不丢 gold 列 | 置信度 AUROC（精确） | 置信度 AUROC（不丢） | keep 全召回 | keep 平均列数 | keep P | keep R |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for name, row in out["models"].items():
        lines.append(
            f"| {name} | {row['group_decisions']} | {100 * row['exact_rate']:.1f} | {100 * row['safe_rate']:.1f} | "
            f"{fmt(row['auroc_confidence_exact'])} | {fmt(row['auroc_confidence_safe'])} | "
            f"{100 * row['keep_full_recall']:.1f} | {row['keep_avg_columns']:.1f} | "
            f"{100 * row['keep_precision']:.1f} | {100 * row['keep_recall']:.1f} |"
        )
    lines += [
        "",
        f"oracle 同名消歧参考：keep 全召回 {100 * out['oracle']['keep_full_recall']:.1f}，平均列数 {out['oracle']['keep_avg_columns']:.1f}。",
        f"JEV 与 qwen 选择的选项一致率：{100 * out['agreement_jev_qwen']:.1f}。",
    ]
    (OUT_DIR / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    return 0


def fmt(x) -> str:
    return "-" if x is None else f"{x:.3f}"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", choices=["jev", "qwen"])
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--out", default="")
    parser.add_argument("--report", action="store_true")
    parser.add_argument("--keeps", action="store_true", help="write keep_<model>_<tag>.json for sql_ex.py")
    parser.add_argument("--thresholds", default="0.5,0.8")
    args = parser.parse_args()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    if args.report:
        return report()
    if args.keeps:
        return dump_keeps([float(t) for t in args.thresholds.split(",") if t])
    if not args.model:
        raise SystemExit("pass --model jev|qwen, --keeps or --report")
    out = Path(args.out) if args.out else OUT_DIR / f"{args.model}.jsonl"
    if not out.is_absolute():
        out = (HERE / out).resolve()
    run(args.model, args.limit, out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
