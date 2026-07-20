#!/usr/bin/env python3
"""
RQA metrics per clue-giver attempt window.

For each trial: build ONE recurrence matrix from the trial's signal (so the threshold is
shared across the whole trial and attempts are directly comparable), then compute RQA
metrics (RR, DET, LAM, L_MAX) on the sub-matrix of each clue-giver attempt window — the
attempt intervals produced by turntaking_attempts.ipynb (clue_giver_attempts.csv).

Reuses the RQA core from dims/opt/step_RQA.py:
  - calculate_recurrence_matrix(series, target_recurrence=...)  -> matrix, threshold, actual RR
  - calculate_window_metrics(sub_matrix, dt, exclude_main_diagonal=True) -> RR, DET, LAM, L_MAX

Signals:
  wrist_speed  clue-giver wrist speed from motiontracking/Output_TimeSeries (30 fps)
  envelope     normalized speech envelope from TS_acoustics (500 Hz, decimated)

Usage:
  python rqa_attempts.py                             # wrist_speed, all trials
  python rqa_attempts.py --signal envelope
  python rqa_attempts.py --trial 103_203_15_1_20250113_152557_bacon_board
  python rqa_attempts.py --guessed-only --target-recurrence 0.05

Output: rqa_attempts_{signal}.csv — one row per attempt with its RQA metrics.
Deps: pandas, numpy, scipy
"""
import argparse
import contextlib
import io
import sys
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent          # .../BalanceCorpus/orangediamond
CORPUS = HERE.parent                            # .../BalanceCorpus
MOTION = CORPUS / "motiontracking" / "Output_TimeSeries"
TS_AC = CORPUS / "TS_acoustics"
ATTEMPTS_CSV = HERE / "clue_giver_attempts.csv"
SUMMARY_CSV = HERE / "trial_turntaking_summary.csv"

# Reuse the RQA implementation that already backs the DIMS dashboard.
sys.path.insert(0, str(HERE / "dims" / "opt"))
from step_RQA import calculate_recurrence_matrix, calculate_window_metrics  # noqa: E402

# A full recurrence matrix is O(n^2); cap the series length so 500 Hz signals stay tractable.
MAX_POINTS = 2000


# --- signal loaders: return (time_seconds, values) --------------------------

def load_wrist_speed(trial_id):
    p = MOTION / f"{trial_id}_clueGiver_cam01_body.csv"
    if not p.exists():
        return None
    body = pd.read_csv(p)
    t = body["time"].to_numpy() / 1000.0
    def sp(x, y):
        dx = np.diff(x, prepend=x[0]); dy = np.diff(y, prepend=y[0])
        return np.sqrt(dx * dx + dy * dy)
    dt = np.diff(t, prepend=t[0]); dt[dt == 0] = np.nan
    v = np.nan_to_num(
        (sp(body["X_LEFT_WRIST"].to_numpy(), body["Y_LEFT_WRIST"].to_numpy())
         + sp(body["X_RIGHT_WRIST"].to_numpy(), body["Y_RIGHT_WRIST"].to_numpy())) / 2 / dt)
    return t, v


def load_envelope(trial_id):
    p = TS_AC / f"env_{trial_id}.wav_norm.csv"
    if not p.exists():
        return None
    d = pd.read_csv(p, usecols=["time", "envelope_norm"])
    return d["time"].to_numpy() / 1000.0, d["envelope_norm"].to_numpy()


LOADERS = {"wrist_speed": load_wrist_speed, "envelope": load_envelope}


def decimate(t, v, max_points):
    """Uniformly thin the series so the n x n recurrence matrix stays tractable."""
    if len(t) <= max_points:
        return t, v
    step = int(np.ceil(len(t) / max_points))
    return t[::step], v[::step]


def rqa_for_trial(trial_id, trial_attempts, loader, target_recurrence, max_points=MAX_POINTS):
    """One recurrence matrix for the trial, then metrics per attempt sub-window."""
    sig = loader(trial_id)
    if sig is None:
        return [], f"{trial_id}: no signal file"
    t, v = decimate(*sig, max_points=max_points)
    if len(t) < 10 or np.std(v) == 0:
        return [], f"{trial_id}: signal too short/flat"

    # step_RQA prints progress lines; keep this script's output clean.
    with contextlib.redirect_stdout(io.StringIO()):
        matrix, threshold, actual_rr = calculate_recurrence_matrix(
            v, target_recurrence=target_recurrence)
    dt = float(np.median(np.diff(t))) if len(t) > 1 else 0.033

    rows = []
    for a in trial_attempts.itertuples(index=False):
        idx = np.where((t >= a.start) & (t < a.end))[0]
        if len(idx) < 2:
            continue                                  # window too short to score
        i0, i1 = idx[0], idx[-1] + 1
        w = matrix[i0:i1, i0:i1]
        # Exclude the line of identity: in single-series RQA it is trivially
        # recurrent and would otherwise dominate DET / L_MAX.
        rr, det, lam, l_max = calculate_window_metrics(w, dt, exclude_main_diagonal=True)
        rows.append({
            "trial_id": trial_id, "pair_id": a.pair_id, "condition": a.condition,
            "target_word": a.target_word, "clue_giver_id": a.clue_giver_id,
            "attempt_idx": a.attempt_idx, "n_attempts": a.n_attempts,
            "start": a.start, "end": a.end, "duration": round(a.end - a.start, 3),
            "n_points": len(idx), "dt": round(dt, 4),
            "trial_threshold": round(float(threshold), 4),
            "trial_recurrence_rate": round(float(actual_rr), 4),
            "RR": round(rr, 4), "DET": round(det, 4),
            "LAM": round(lam, 4), "L_MAX": round(l_max, 4),
        })
    return rows, None


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--signal", choices=sorted(LOADERS), default="wrist_speed")
    ap.add_argument("--trial", help="only this trial_id")
    ap.add_argument("--guessed-only", action="store_true",
                    help="only trials where the target word was actually guessed")
    ap.add_argument("--target-recurrence", type=float, default=0.07,
                    help="target recurrence rate used to pick the threshold (default 0.07)")
    ap.add_argument("--max-points", type=int, default=MAX_POINTS,
                    help=f"cap on series length before building the matrix (default {MAX_POINTS})")
    ap.add_argument("-o", "--out", help="output CSV (default rqa_attempts_{signal}.csv)")
    args = ap.parse_args()

    if not ATTEMPTS_CSV.exists():
        raise SystemExit(f"{ATTEMPTS_CSV} not found — run turntaking_attempts.ipynb first.")
    att = pd.read_csv(ATTEMPTS_CSV)

    if args.guessed_only:
        summ = pd.read_csv(SUMMARY_CSV)
        keep = set(summ.loc[summ["guessed"], "trial_id"])
        att = att[att.trial_id.isin(keep)]
    if args.trial:
        att = att[att.trial_id == args.trial]
        if att.empty:
            raise SystemExit(f"no attempts for trial {args.trial}")

    loader = LOADERS[args.signal]
    out_path = Path(args.out) if args.out else HERE / f"rqa_attempts_{args.signal}.csv"

    trials = list(att.groupby("trial_id", sort=True))
    print(f"signal={args.signal}  trials={len(trials)}  attempts={len(att)}  "
          f"target_recurrence={args.target_recurrence}")

    rows, skipped = [], []
    for i, (tid, sub) in enumerate(trials, 1):
        r, err = rqa_for_trial(tid, sub, loader, args.target_recurrence, args.max_points)
        rows.extend(r)
        if err:
            skipped.append(err)
        if i % 20 == 0 or i == len(trials):
            print(f"  {i}/{len(trials)} trials")

    out = pd.DataFrame(rows)
    out.to_csv(out_path, index=False)
    print(f"\nwrote {out_path.name}: {len(out)} attempt windows from {out.trial_id.nunique()} trials")
    if skipped:
        print(f"{len(skipped)} skipped:")
        for s in skipped[:10]:
            print("  ", s)
    if len(out):
        print("\nmean RQA metrics by attempt index:")
        print(out.groupby("attempt_idx")[["RR", "DET", "LAM", "L_MAX"]].mean().round(3).to_string())


if __name__ == "__main__":
    main()
