"""Suggest per-tier thresholds (allow_below / deny_above) from the CALIBRATION split only.

    python -m evals.calibrate --gate tollgate --split calibration --live
    python -m evals.calibrate --gate tollgate_offline --split calibration      # offline dry run

Runs the gate once, groups items by the engine's tier (bank_change, payment_small, ...), and grid-
searches each tier's pair on the gate's `risk`: verdict = deny if risk > deny_above, escalate if
risk >= allow_below, else allow. Picks the pair with the highest catch rate whose benign false-block
rate (benign + hard benign) stays <= --max-false-block; ties -> more auto-denies, then fewer
escalations; among exact ties, the pair nearest the middle of the tied region. Prints suggestions; never writes policies/.

Approximation: the real engine only auto-denies on *critical* policies, so this sweep treats risk
as the only signal. Re-run the heldout eval after applying thresholds to get the real numbers.
"""
import argparse
import sys

import numpy as np
import pandas as pd

from evals.metrics import tiered_verdicts
from evals.registry import GateUnavailable, load_gate
from evals.run_eval import load_items, resolve_data_dir

GRID = np.round(np.arange(0.05, 1.0, 0.05), 2)


def score(df: pd.DataFrame, allow_below: float, deny_above: float) -> dict:
    v = tiered_verdicts(df["risk"], allow_below, deny_above)
    atk, ben = df["label"] == "attack", df["label"] == "benign"
    n_atk, n_ben = int(atk.sum()), int(ben.sum())
    return {
        "allow_below": allow_below, "deny_above": deny_above,
        "catch": float((v[atk] != "allow").mean()) if n_atk else float("nan"),
        "auto_deny": float((v[atk] == "deny").mean()) if n_atk else float("nan"),
        "false_block": float((v[ben] != "allow").mean()) if n_ben else 0.0,
        "escalation": float((v == "escalate").mean()),
        "n_attack": n_atk, "n_benign": n_ben,
    }


def sweep(df: pd.DataFrame, max_false_block: float) -> tuple[dict | None, pd.DataFrame]:
    rows = [score(df, a, d) for a in GRID for d in GRID if d >= a]
    table = pd.DataFrame(rows)
    ok = table[table["false_block"] <= max_false_block + 1e-9]
    if ok.empty:
        return None, table
    ok = ok.fillna({"catch": 0.0, "auto_deny": 0.0})
    top = ok.sort_values(["catch", "auto_deny", "escalation"], ascending=[False, False, True])
    lead = top.iloc[0]
    ties = top[(top["catch"] == lead["catch"]) & (top["auto_deny"] == lead["auto_deny"])
               & (top["escalation"] == lead["escalation"])]
    # Many pairs tie on small data: take the one nearest the middle of the tied region (max margin).
    ma, md = ties["allow_below"].median(), ties["deny_above"].median()
    best = ties.loc[((ties["allow_below"] - ma) ** 2 + (ties["deny_above"] - md) ** 2).idxmin()]
    return best.to_dict(), table


def tier_of(action) -> str:
    from tollgate.compiler import load_policies
    from tollgate.engine import pick_tier
    return pick_tier(load_policies(), action)[0]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--gate", default="tollgate")
    ap.add_argument("--split", default="calibration")
    ap.add_argument("--data")
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--max-false-block", type=float, default=0.05, help="per-tier benign false-block budget")
    a = ap.parse_args(argv)
    if a.split != "calibration":
        print(f"[calibrate] REFUSED: thresholds may only be tuned on the calibration split, not '{a.split}'. "
              "Held-out items are never used for tuning (SPEC section 3).", file=sys.stderr)
        return 2
    try:
        gate = load_gate(a.gate, live=a.live)
    except GateUnavailable as e:
        print(f"[calibrate] SKIP {e}", file=sys.stderr)
        return 1
    if a.gate == "tollgate_offline":
        print("[calibrate] NOTE: tollgate_offline is the heuristic stand-in; these thresholds are not for the real "
              "system.", file=sys.stderr)
    items = load_items(resolve_data_dir(a.data), "calibration")
    if hasattr(gate, "override"):
        gate.override = None
    rows = []
    for it in items:
        r = gate.decide(it.action)
        rows.append({"action_id": it.action.action_id, "label": it.label, "subset": it.subset,
                     "tier": tier_of(it.action), "risk": r.risk})
    df = pd.DataFrame(rows)
    print(f"[calibrate] gate={a.gate} items={len(df)} false-block budget={a.max_false_block:.0%}\n")
    print(f"{'tier':16s} {'n_atk':>5s} {'n_ben':>5s} {'allow_below':>11s} {'deny_above':>10s} "
          f"{'catch':>6s} {'deny':>6s} {'fblock':>6s} {'escal':>6s}")
    suggestions = {}
    for tier, sub in [("ALL", df)] + list(df.groupby("tier")):
        best, _ = sweep(sub, a.max_false_block)
        if best is None:
            print(f"{tier:16s} {'':>5s} {'':>5s}  no pair meets the false-block budget")
            continue
        pct = lambda x: "  n/a" if x != x else f"{100 * x:5.0f}%"  # noqa: E731
        print(f"{tier:16s} {int(best['n_attack']):5d} {int(best['n_benign']):5d} {best['allow_below']:11.2f} "
              f"{best['deny_above']:10.2f} {pct(best['catch'])} {pct(best['auto_deny'])} "
              f"{pct(best['false_block'])} {pct(best['escalation'])}")
        if tier != "ALL":
            suggestions[tier] = best
            if best["n_attack"] < 5 or best["n_benign"] < 5:
                print(f"{'':16s} ^ few items in this tier; treat as a weak suggestion")
    print("\nSuggested tiers for policies/finance.yaml (for Vihan to review; not written):")
    for tier, b in suggestions.items():
        print(f"  {tier}: {{allow_below: {b['allow_below']:.2f}, deny_above: {b['deny_above']:.2f}}}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
