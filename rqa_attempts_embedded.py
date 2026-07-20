#!/usr/bin/env python3
"""
RQA metrics per clue-giver attempt window — WITH time-delay (Takens) embedding.

Same contract as rqa_attempts.py (identical function names/signatures, so the notebook can
swap between them with a one-line import), but the recurrence matrix is built in a
reconstructed phase space instead of on the raw 1-D signal:

    Y[i] = [x(i), x(i+tau), x(i+2*tau), ..., x(i+(m-1)*tau)]

so two time points are "recurrent" when the system was in a similar *state* (value AND its
recent trajectory), not merely at a similar amplitude. This is the classical RQA that
DET/LAM/L_MAX were defined for.

Parameters (per trial, per signal) are estimated automatically by default:
  tau (delay)     first local minimum of the average mutual information (fallback: the lag
                  where autocorrelation first drops below 1/e)
  m   (dimension) smallest dimension where the false-nearest-neighbour fraction (Kennel)
                  falls below `FNN_THRESHOLD`
Override with --delay / --dim to hold them fixed across trials (often preferable for
between-condition comparisons, since auto-selection makes m/tau vary per trial).

Signals:
  wrist_speed  clue-giver wrist speed from motiontracking/Output_TimeSeries (30 fps)
  envelope     normalized speech envelope from TS_acoustics (500 Hz, decimated)

Usage:
  python rqa_attempts_embedded.py                            # wrist_speed, auto m/tau
  python rqa_attempts_embedded.py --signal envelope
  python rqa_attempts_embedded.py --dim 3 --delay 5          # fixed embedding
  python rqa_attempts_embedded.py --trial 103_203_15_1_20250113_152557_bacon_board

Output: rqa_attempts_embedded_{signal}.csv — one row per attempt, plus the emb_dim /
emb_delay actually used.
Deps: pandas, numpy, scipy
"""
import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.spatial.distance import cdist

HERE = Path(__file__).resolve().parent          # .../BalanceCorpus/orangediamond
CORPUS = HERE.parent                            # .../BalanceCorpus
MOTION = CORPUS / "motiontracking" / "Output_TimeSeries"
TS_AC = CORPUS / "TS_acoustics"
ATTEMPTS_CSV = HERE / "clue_giver_attempts.csv"
SUMMARY_CSV = HERE / "trial_turntaking_summary.csv"

# Only the line-counting is reused; the matrix is built here (embedded).
sys.path.insert(0, str(HERE / "dims" / "opt"))
from step_RQA import calculate_window_metrics  # noqa: E402

MAX_POINTS = 2000        # cap series length: the recurrence matrix is O(n^2)
MAX_LAG = 50             # search range for tau
MAX_DIM = 8              # search range for m
FNN_THRESHOLD = 0.01     # stop raising m once <1% false nearest neighbours
FNN_RTOL, FNN_ATOL = 15.0, 2.0   # Kennel's criteria
FNN_MAX_POINTS = 800     # subsample for the O(n^2) FNN search


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


# def wrist_speed(trial_id):
#     """Summed wrist speed of BOTH participants (clue giver + guesser) from cam01 body
#     tracking. Normalized frame units / s; `time` in ms on the clue-giver's grid."""

#     def wrist_speed(path):
#         body = pd.read_csv(path)
#         t = body["time"].to_numpy() / 1000.0
#         def sp(x, y):
#             dx = np.diff(x, prepend=x[0]); dy = np.diff(y, prepend=y[0])
#             return np.sqrt(dx * dx + dy * dy)
#         dt = np.diff(t, prepend=t[0]); dt[dt == 0] = np.nan
#         v = np.nan_to_num(
#             (sp(body["X_LEFT_WRIST"].to_numpy(), body["Y_LEFT_WRIST"].to_numpy())
#              + sp(body["X_RIGHT_WRIST"].to_numpy(), body["Y_RIGHT_WRIST"].to_numpy())) / 2 / dt)
#         return t, v

#     p_gu = MOTION / f"{trial_id}_guesser_cam01_body.csv"
#     if not p_gu.exists():
#         raise FileNotFoundError(f"no guesser motion tracking for {trial_id}")

#     t_cg, v_cg = wrist_speed(MOTION / f"{trial_id}_clueGiver_cam01_body.csv")
#     t_gu, v_gu = wrist_speed(p_gu)
#     v_gu = np.interp(t_cg, t_gu, v_gu)        # grids often differ -> resample onto the CG's
#     return pd.DataFrame({"time": t_cg * 1000.0, "dyad_wrist_speed": v_cg + v_gu})



def load_envelope(trial_id):
    p = TS_AC / f"env_{trial_id}.wav_norm.csv"
    if not p.exists():
        return None
    d = pd.read_csv(p, usecols=["time", "envelope_norm"])
    return d["time"].to_numpy() / 1000.0, d["envelope_norm"].to_numpy()


LOADERS = {"wrist_speed": load_wrist_speed, "envelope": load_envelope}


def decimate(t, v, max_points=MAX_POINTS):
    """Uniformly thin the series so the n x n recurrence matrix stays tractable."""
    if len(t) <= max_points:
        return t, v
    step = int(np.ceil(len(t) / max_points))
    return t[::step], v[::step]


# --- embedding --------------------------------------------------------------

def embed(x, dim, delay):
    """Time-delay embedding: (n_vectors, dim), where n_vectors = len(x) - (dim-1)*delay.

    Row i is [x(i), x(i+delay), ..., x(i+(dim-1)*delay)] and is anchored at time t[i].
    """
    n = len(x) - (dim - 1) * delay
    if n <= 1:
        raise ValueError(f"series too short to embed with dim={dim}, delay={delay}")
    return np.column_stack([x[i * delay: i * delay + n] for i in range(dim)])


def mutual_information(x, lag, bins=16):
    """Average mutual information between x(t) and x(t+lag), via a joint histogram."""
    a, b = x[:len(x) - lag], x[lag:]
    pxy = np.histogram2d(a, b, bins)[0]
    pxy = pxy / pxy.sum()
    px, py = pxy.sum(axis=1), pxy.sum(axis=0)
    nz = pxy > 0
    outer = np.outer(px, py)
    return float(np.sum(pxy[nz] * np.log(pxy[nz] / outer[nz])))


def estimate_delay(x, max_lag=MAX_LAG, bins=16):
    """First local minimum of the AMI; fallback = first lag where autocorr < 1/e."""
    max_lag = min(max_lag, len(x) // 4)
    if max_lag < 2:
        return 1
    ami = [mutual_information(x, lag, bins) for lag in range(1, max_lag + 1)]
    for i in range(1, len(ami) - 1):
        if ami[i] < ami[i - 1] and ami[i] < ami[i + 1]:
            return i + 1                       # ami[i] is lag i+1
    xc = x - x.mean()
    ac = np.correlate(xc, xc, mode="full")[len(x) - 1:]
    if ac[0] == 0:
        return 1
    ac = ac / ac[0]
    below = np.where(ac < 1.0 / np.e)[0]
    return int(below[0]) if len(below) else 1


def estimate_dimension(x, delay, max_dim=MAX_DIM):
    """Smallest dim whose false-nearest-neighbour fraction < FNN_THRESHOLD (Kennel)."""
    sd = np.std(x)
    if sd == 0:
        return 1
    for m in range(1, max_dim + 1):
        try:
            Y, Y1 = embed(x, m, delay), embed(x, m + 1, delay)
        except ValueError:
            return m
        n = min(len(Y1), FNN_MAX_POINTS)
        Y, Y1 = Y[:n], Y1[:n]
        d = cdist(Y, Y)
        np.fill_diagonal(d, np.inf)
        nn = np.argmin(d, axis=1)
        d_m = d[np.arange(n), nn]
        d_m1 = np.linalg.norm(Y1 - Y1[nn], axis=1)
        with np.errstate(divide="ignore", invalid="ignore"):
            grew = np.sqrt(np.abs(d_m1 ** 2 - d_m ** 2)) / np.where(d_m == 0, np.nan, d_m) > FNN_RTOL
        far = d_m1 / sd > FNN_ATOL
        if np.nanmean(grew | far) < FNN_THRESHOLD:
            return m
    return max_dim


def embedded_recurrence_matrix(x, dim, delay, target_recurrence=0.07):
    """Recurrence matrix of the delay-embedded, z-scored series.

    Returns (matrix, threshold, actual_recurrence_rate, n_vectors).
    """
    sd = np.std(x)
    z = (x - np.mean(x)) / (sd if sd else 1.0)
    Y = embed(z, dim, delay)
    d = cdist(Y, Y, metric="euclidean")
    upper = d[np.triu_indices_from(d, k=1)]
    threshold = float(np.percentile(upper, target_recurrence * 100))
    R = (d <= threshold).astype(np.uint8)
    n = len(Y)
    actual = (int(R.sum()) - n) / (n * n - n) if n > 1 else 0.0
    return R, threshold, float(actual), n


# --- per-trial --------------------------------------------------------------

def rqa_for_trial(trial_id, trial_attempts, loader, target_recurrence, max_points=MAX_POINTS,
                  dim=None, delay=None):
    """One embedded recurrence matrix for the trial, then metrics per attempt sub-window.

    `dim` / `delay` default to None = estimate them from this trial's signal.
    """
    sig = loader(trial_id)
    if sig is None:
        return [], f"{trial_id}: no signal file"
    t, v = decimate(*sig, max_points=max_points)
    if len(t) < 20 or np.std(v) == 0:
        return [], f"{trial_id}: signal too short/flat"

    tau = delay if delay else estimate_delay(v)
    m = dim if dim else estimate_dimension(v, tau)
    try:
        matrix, threshold, actual_rr, n_vec = embedded_recurrence_matrix(
            v, m, tau, target_recurrence)
    except ValueError as e:
        return [], f"{trial_id}: {e}"

    # Embedded vector i is anchored at t[i]; the last (m-1)*tau samples have no vector.
    t_emb = t[:n_vec]
    dt = float(np.median(np.diff(t_emb))) if len(t_emb) > 1 else 0.033

    rows = []
    for a in trial_attempts.itertuples(index=False):
        idx = np.where((t_emb >= a.start) & (t_emb < a.end))[0]
        if len(idx) < 2:
            continue                                  # window too short to score
        i0, i1 = idx[0], idx[-1] + 1
        w = matrix[i0:i1, i0:i1]
        # Exclude the line of identity: trivially recurrent, would dominate DET / L_MAX.
        rr, det, lam, l_max = calculate_window_metrics(w, dt, exclude_main_diagonal=True)
        rows.append({
            "trial_id": trial_id, "pair_id": a.pair_id, "condition": a.condition,
            "target_word": a.target_word, "clue_giver_id": a.clue_giver_id,
            "attempt_idx": a.attempt_idx, "n_attempts": a.n_attempts,
            "start": a.start, "end": a.end, "duration": round(a.end - a.start, 3),
            "n_points": len(idx), "dt": round(dt, 4),
            "emb_dim": m, "emb_delay": tau, "emb_delay_sec": round(tau * dt, 4),
            "trial_threshold": round(threshold, 4),
            "trial_recurrence_rate": round(actual_rr, 4),
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
                    help=f"cap on series length before embedding (default {MAX_POINTS})")
    ap.add_argument("--dim", type=int, help="fixed embedding dimension (default: estimate per trial)")
    ap.add_argument("--delay", type=int, help="fixed embedding delay in samples (default: estimate)")
    ap.add_argument("-o", "--out", help="output CSV (default rqa_attempts_embedded_{signal}.csv)")
    args = ap.parse_args()

    if not ATTEMPTS_CSV.exists():
        raise SystemExit(f"{ATTEMPTS_CSV} not found — run turntaking_attempts.ipynb first.")
    att = pd.read_csv(ATTEMPTS_CSV)

    if args.guessed_only:
        summ = pd.read_csv(SUMMARY_CSV)
        att = att[att.trial_id.isin(set(summ.loc[summ["guessed"], "trial_id"]))]
    if args.trial:
        att = att[att.trial_id == args.trial]
        if att.empty:
            raise SystemExit(f"no attempts for trial {args.trial}")

    loader = LOADERS[args.signal]
    out_path = Path(args.out) if args.out else HERE / f"rqa_attempts_embedded_{args.signal}.csv"

    trials = list(att.groupby("trial_id", sort=True))
    emb = (f"dim={args.dim}" if args.dim else "dim=auto") + \
          (f", delay={args.delay}" if args.delay else ", delay=auto")
    print(f"signal={args.signal}  trials={len(trials)}  attempts={len(att)}  "
          f"target_recurrence={args.target_recurrence}  embedding: {emb}")

    rows, skipped = [], []
    for i, (tid, sub) in enumerate(trials, 1):
        r, err = rqa_for_trial(tid, sub, loader, args.target_recurrence, args.max_points,
                               dim=args.dim, delay=args.delay)
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
        print(f"\nembedding used — dim: {sorted(out.emb_dim.unique())}, "
              f"delay: {sorted(out.emb_delay.unique())}")
        print("\nmean RQA metrics by attempt index:")
        print(out.groupby("attempt_idx")[["RR", "DET", "LAM", "L_MAX"]].mean().round(3).to_string())


if __name__ == "__main__":
    main()
