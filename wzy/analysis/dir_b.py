"""Direction B, minimal experiment: per-question schema budget with a calibrated recall guarantee.

Usage:
    python dir_b.py [--out dir_b.json]

Reads the recorded exp2b run (jev_minidev_no_mention_tab0.1). Column Scores are calibrated into
P(column is gold) with a cross-fitted isotonic map (5 folds by question). Two policies are compared
under the same cross-fitted risk-control procedure:

  crc     per question, keep the smallest set whose probability of containing every gold column is
          >= 1 - alpha' (independent columns; forced FK columns always kept). alpha' is chosen on the
          calibration folds with the CRC bound (n*risk + 1)/(n + 1) <= alpha.
  global  one score threshold for all questions, chosen on the calibration folds with the same bound.

Reported per alpha: empirical risk (share of questions missing a gold column) and average columns,
on the held-out folds. No API calls; gold is used only for evaluation and calibration labels.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

from sl_lib import OUTPUTS, RUNS, auroc, calibration, forced_columns, load_gold, load_run, scored_columns, set_metrics

HERE = Path(__file__).resolve().parent
FOLDS = 5
ALPHAS = (0.05, 0.1, 0.15, 0.2, 0.3)
THRESHOLDS = (0.0, 0.05, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.75, 0.9, 1.0, 1.25, 1.5, 1.75, 2.0, 2.25, 2.5, 2.75, 3.0)
ALPHA_GRID = tuple(i / 100 for i in range(1, 61))


def isotonic(xs: list[float], ys: list[int]) -> tuple[list[float], list[float]]:
    """Pool-adjacent-violators isotonic regression; returns (unique x ascending, fitted y)."""
    agg = {}
    for x, y in zip(xs, ys):
        pos, count = agg.get(x, (0, 0))
        agg[x] = (pos + y, count + 1)
    uniq = sorted(agg)
    blocks = []  # [sum_y, count, first_index, last_index]
    for i, x in enumerate(uniq):
        pos, count = agg[x]
        blocks.append([pos, count, i, i])
        while len(blocks) > 1 and blocks[-2][0] / blocks[-2][1] > blocks[-1][0] / blocks[-1][1]:
            pos, count, _, last = blocks.pop()
            blocks[-1][0] += pos
            blocks[-1][1] += count
            blocks[-1][3] = last
    fitted = [0.0] * len(uniq)
    for pos, count, first, last in blocks:
        value = max(1e-4, min(1 - 1e-4, pos / count))
        for i in range(first, last + 1):
            fitted[i] = value
    return uniq, fitted


def apply_isotonic(uniques: list[float], fitted: list[float], x: float) -> float:
    """Step function: value of the largest unique score <= x (nearest lower)."""
    if x < uniques[0]:
        return fitted[0]
    lo, hi = 0, len(uniques) - 1
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if uniques[mid] <= x:
            lo = mid
        else:
            hi = mid - 1
    return fitted[lo]


def folds_of(qids: list) -> dict:
    return {q: int(q) % FOLDS for q in qids}


def calibrate(records: dict, gold: dict, qids: list) -> dict:
    """question_id -> {(table, column): p_gold}, cross-fitted by question."""
    folds = folds_of(qids)
    probs = {q: {} for q in qids}
    for fold in range(FOLDS):
        train_x, train_y = [], []
        for q in qids:
            if folds[q] == fold:
                continue
            for key, (_, score) in scored_columns(records[q]).items():
                train_x.append(score)
                train_y.append(int(key in gold[q]["columns"]))
        uniques, fitted = isotonic(train_x, train_y)
        for q in qids:
            if folds[q] != fold:
                continue
            for key, (_, score) in scored_columns(records[q]).items():
                probs[q][key] = apply_isotonic(uniques, fitted, score)
    return probs


def keep_crc(record: dict, probs: dict, alpha: float) -> set:
    """Smallest keep set whose probability of containing every gold column is >= 1 - alpha.

    Forced FK columns are always kept. Among the other scored columns the lowest-p ones are dropped
    first; the product of (1 - p) over dropped columns must stay >= 1 - alpha.
    """
    forced = forced_columns(record)
    candidates = sorted((k for k in scored_columns(record) if k not in forced), key=lambda k: probs.get(k, 0.0))
    budget = math.log(1 - alpha)
    dropped = 0.0
    drop_set = set()
    for key in candidates:
        step = math.log(max(1e-9, 1 - probs.get(key, 0.0)))
        if dropped + step < budget:
            break
        dropped += step
        drop_set.add(key)
    return (set(scored_columns(record)) - drop_set) | forced


def keep_global(record: dict, threshold: float) -> set:
    return {k for k, (_, s) in scored_columns(record).items() if s >= threshold} | forced_columns(record)


def risk_cols(records: dict, gold: dict, qids: list, keep_fn) -> tuple[float, float]:
    misses = 0
    columns = 0
    for q in qids:
        keep = keep_fn(q)
        misses += not gold[q]["columns"] <= keep
        columns += len(keep)
    n = len(qids)
    return misses / n, columns / n


def crc_bound(risk: float, n: int) -> float:
    return (n * risk + 1) / (n + 1)


def pick_alpha_crc(records, probs, gold, qids, alpha) -> tuple[float, bool]:
    """Largest per-question budget that meets the CRC bound on the calibration questions.

    Returns (alpha', feasible). If even the most conservative budget misses the target (the floor
    risk is above alpha), returns the conservative budget and feasible=False.
    """
    n = len(qids)
    feasible = False
    chosen = ALPHA_GRID[0]
    for a2 in reversed(ALPHA_GRID):
        r, _ = risk_cols(records, gold, qids, lambda q, a2=a2: keep_crc(records[q], probs[q], a2))
        if crc_bound(r, n) <= alpha:
            chosen = a2
            feasible = True
            break
    return chosen, feasible


def pick_threshold_global(records, gold, qids, alpha) -> tuple[float, bool]:
    """Largest global threshold that meets the CRC bound on the calibration questions."""
    n = len(qids)
    best = THRESHOLDS[0]
    feasible = False
    for t in THRESHOLDS:
        r, _ = risk_cols(records, gold, qids, lambda q, t=t: keep_global(records[q], t))
        if crc_bound(r, n) <= alpha:
            best = t
            feasible = True
        else:
            break
    return best, feasible


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=str(HERE / "dir_b.json"))
    args = parser.parse_args()

    gold = load_gold()
    records = load_run(RUNS["exp2b_tab0.1"])[0]
    qids = sorted(set(gold) & set(records), key=int)
    probs = calibrate(records, gold, qids)
    folds = folds_of(qids)

    # calibration quality
    all_p = [probs[q][k] for q in qids for k in scored_columns(records[q])]
    all_y = [int(k in gold[q]["columns"]) for q in qids for k in scored_columns(records[q])]
    cal = calibration(all_p, all_y)
    out = {
        "n": len(qids),
        "calibration": {"auroc": auroc(all_p, all_y), "brier": cal.get("brier"), "ece": cal.get("ece")},
        "results": [],
    }

    for alpha in ALPHAS:
        crc_keep, glb_keep = {}, {}
        chosen = []
        for fold in range(FOLDS):
            cal_q = [q for q in qids if folds[q] != fold]
            test_q = [q for q in qids if folds[q] == fold]
            a2, crc_ok = pick_alpha_crc(records, probs, gold, cal_q, alpha)
            t, glb_ok = pick_threshold_global(records, gold, cal_q, alpha)
            chosen.append({"fold": fold, "alpha_prime": a2, "crc_feasible": crc_ok, "threshold": t, "global_feasible": glb_ok})
            for q in test_q:
                crc_keep[q] = keep_crc(records[q], probs[q], a2)
                glb_keep[q] = keep_global(records[q], t)
        crc_risk, crc_cols = risk_cols(records, gold, qids, lambda q: crc_keep[q])
        glb_risk, glb_cols = risk_cols(records, gold, qids, lambda q: glb_keep[q])
        mc = set_metrics([(crc_keep[q], gold[q]["columns"]) for q in qids])
        mg = set_metrics([(glb_keep[q], gold[q]["columns"]) for q in qids])
        out["results"].append({
            "alpha": alpha,
            "crc": {"risk": crc_risk, "cols": crc_cols, "precision": mc["precision"], "recall": mc["recall"]},
            "global": {"risk": glb_risk, "cols": glb_cols, "precision": mg["precision"], "recall": mg["recall"]},
            "per_fold": chosen,
        })
    Path(args.out).write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"calibration: AUROC {out['calibration']['auroc']:.3f}  Brier {out['calibration']['brier']:.3f}  ECE {out['calibration']['ece']:.3f}")
    print("alpha | CRC(risk, cols) | global-threshold(risk, cols) | thresholds chosen")
    for row in out["results"]:
        ts = sorted({f["threshold"] for f in row["per_fold"]})
        print(f"{row['alpha']:.2f}  | {100 * row['crc']['risk']:.1f}%, {row['crc']['cols']:.1f} | "
              f"{100 * row['global']['risk']:.1f}%, {row['global']['cols']:.1f} | t={ts}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
