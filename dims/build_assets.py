#!/usr/bin/env python3
"""
Fills the DIMS `assets/` folder and writes `config.json` for the BalanceCorpus
dashboard, from the raw corpus data.

Trials are discovered directly from the corpus `merged/` videos (no manifest
needed). The corpus root is located automatically by walking up from this file
until a directory containing `merged/` and `metadata.csv` is found, so the
dashboard folder can live anywhere under the corpus.

For every real (non-preparation) merged trial video it writes:
  - assets/videos/{trial_id}.mp4         symlink -> <corpus>/merged/{dyad}/{trial_id}.mp4
  - assets/timeseries/{trial_id}_{channel}.csv   (Time,<channel>) for each available channel

Channels: envelope, f0 (from TS_acoustics), sway (from gyroscope.csv).
A channel is skipped (and left out of that trial's config dataTypes) when its
source file is missing.

Usage: python build_assets.py
Deps:  pandas, numpy
"""
import json
import shutil
import sys
from pathlib import Path

import numpy as np
import pandas as pd


def find_corpus_root(start):
    """Walk up from `start` to the BalanceCorpus root (has merged/ and metadata.csv)."""
    for d in [start, *start.parents]:
        if (d / "merged").is_dir() and (d / "metadata.csv").exists():
            return d
    raise SystemExit(
        f"Could not locate the BalanceCorpus root (a parent directory containing "
        f"both merged/ and metadata.csv) above {start}")


DIMS_DIR = Path(__file__).resolve().parent
CORPUS = find_corpus_root(DIMS_DIR)
MERGED = CORPUS / "merged"
TS_AC = CORPUS / "TS_acoustics"
GYRO_CSV = CORPUS / "gyroscope.csv"
MOTION_DIR = CORPUS / "motiontracking"

ASSETS = DIMS_DIR / "assets"
TS_OUT = ASSETS / "timeseries"
VID_OUT = ASSETS / "videos"
ELAN_OUT = ASSETS / "elan"
RQA_OUT = ASSETS / "rqa"

TARGET_HZ = 50.0  # resample rate for the dense acoustic + gyro channels

# Which multidimensional-wrist-RQA files feed the dashboard's RQA tab. We only
# take the clue-giver's camera-01 HANDS view, and expose the position- and
# velocity-based wrist embeddings as two separate RQA data types / plots.
MDRQA_ROLE = "clueGiver"
MDRQA_CAM = "cam01"
MDRQA_LANDMARK = "hands"
MDRQA_SOURCES = {
    "wrists_hands": MOTION_DIR / "Output_mdRQA_wrists",
    "wrists_velocity_hands": MOTION_DIR / "Output_mdRQA_wrists_velocity",
}


def moving_average(x, w):
    if w <= 1:
        return x
    kernel = np.ones(w) / w
    return np.convolve(x, kernel, mode="same")


def write_ts(trial_id, channel, time, value):
    """Write a Time,<channel> CSV. NaN values become empty cells."""
    out = TS_OUT / f"{trial_id}_{channel}.csv"
    df = pd.DataFrame({"Time": np.round(time, 4), channel: value})
    df.to_csv(out, index=False, na_rep="")
    return True


# --- per-channel loaders: return (time_seconds, value) or None -------------

def load_envelope(trial_id):
    p = TS_AC / f"env_{trial_id}.wav_norm.csv"
    if not p.exists():
        return None
    df = pd.read_csv(p, usecols=["time", "envelope_norm"]).iloc[::10]  # 500 -> 50 Hz
    return df["time"].to_numpy() / 1000.0, df["envelope_norm"].to_numpy()


def load_f0(trial_id):
    p = TS_AC / f"f0_{trial_id}.wav.csv"
    if not p.exists():
        return None
    df = pd.read_csv(p, usecols=["time_ms", "f0"]).iloc[::10]  # 500 -> 50 Hz
    return df["time_ms"].to_numpy() / 1000.0, df["f0"].to_numpy()  # NaN kept for unvoiced


def load_sway(gyro_all, dyad, trial_num):
    g = gyro_all[(gyro_all["group_name"].astype(str) == dyad)
                 & (gyro_all["trial_number"] == trial_num)].copy()
    if len(g) < 2:
        return None
    ref = pd.to_datetime(g["time"])
    g["seconds"] = (ref - ref.iloc[0]).dt.total_seconds()
    g = g.sort_values("seconds").reset_index(drop=True)
    g["seconds"] = g["seconds"] - g["seconds"].iloc[0]  # rows arrive unsorted; re-zero

    angle_col = next(c for c in g.columns if c.startswith("AngleX"))
    ang = np.degrees(np.unwrap(np.radians(g[angle_col].to_numpy())))
    lean = np.abs(ang - np.median(ang))

    t = g["seconds"].to_numpy()
    if t[-1] <= 0:
        return None
    tt = np.arange(0.0, t[-1], 1.0 / TARGET_HZ)
    ll = moving_average(np.interp(tt, t, lean), int(0.5 * TARGET_HZ))  # 0.5 s smoothing
    return tt, ll


# --- multidimensional wrist RQA --------------------------------------------

def _mdrqa_to_dashboard(md):
    """Reshape one mdRQA_data block (from step_mdRQA_wrists.py) into the schema
    the dashboard's RQA tab expects.

    The dashboard draws a single 1-D marginal next to the recurrence plot, but
    the mdRQA embedding is multidimensional. We collapse it to one displayable
    series: the L2 norm of the per-dimension z-scored wrist coordinates (i.e. the
    overall wrist displacement in the normalized state space).
    """
    vis = md["visualization"]
    dims = list(vis["data"].keys())
    arr = np.array([vis["data"][d] for d in dims], dtype=float).T  # (T, d)
    mean = arr.mean(axis=0)
    std = arr.std(axis=0)
    std[std == 0] = 1.0
    marginal = np.linalg.norm((arr - mean) / std, axis=1)

    return {
        "data_type": None,  # filled in by the caller
        "dimensions": md["dimensions"],
        "threshold": md["threshold"],
        "recurrence_rate": md["recurrence_rate"],
        "time_range": md["time_range"],
        "windowed_metrics": md["windowed_metrics"],
        "visualization": {
            "time": vis["time"],
            "data": marginal.tolist(),
            "matrix_size": vis["matrix_size"],
            "sparse_matrix": vis["sparse_matrix"],
        },
        "full_data": md.get("full_data", {}),
    }


def load_mdrqa(trial_id):
    """Collect the clue-giver/cam01 hands wrist-mdRQA results for `trial_id`.

    Returns {data_type: plotData} for the position and velocity variants that
    exist, or an empty dict if none are present.
    """
    out = {}
    for data_type, src_dir in MDRQA_SOURCES.items():
        p = src_dir / f"{trial_id}_{MDRQA_ROLE}_{MDRQA_CAM}_{MDRQA_LANDMARK}_mdRQA.json"
        if not p.exists():
            continue
        try:
            md = json.loads(p.read_text())["mdRQA_data"]
        except (ValueError, KeyError) as e:
            print(f"  {trial_id}: malformed mdRQA file {p.name}: {e}")
            continue
        entry = _mdrqa_to_dashboard(md)
        entry["data_type"] = data_type
        out[data_type] = entry
    return out


# --- video symlink ---------------------------------------------------------

def link_video(dyad, trial_id):
    src = MERGED / dyad / f"{trial_id}.mp4"
    if not src.exists():
        return False
    dst = VID_OUT / f"{trial_id}.mp4"
    if dst.exists() or dst.is_symlink():
        dst.unlink()
    try:
        dst.symlink_to(src.resolve())
    except OSError:
        shutil.copy2(src, dst)
    return True


def discover_trials():
    """Find real (non-preparation) trials from the merged videos: (dyad, trial_num, trial_id)."""
    trials = []
    for mp4 in MERGED.glob("*/*.mp4"):
        tid = mp4.stem
        if tid.startswith("preparation_"):
            continue
        parts = tid.split("_")
        try:
            trial_num = int(parts[2])
        except (IndexError, ValueError):
            trial_num = 0
        trials.append((mp4.parent.name, trial_num, tid))
    trials.sort(key=lambda x: (x[0], x[1]))
    return trials


# --- main ------------------------------------------------------------------

def main():
    for d in (TS_OUT, VID_OUT, RQA_OUT):
        d.mkdir(parents=True, exist_ok=True)
    # Clear previously generated assets so removed channels / ELAN don't linger.
    for old in TS_OUT.glob("*.csv"):
        old.unlink()
    for old in RQA_OUT.glob("*_rqa_data.json"):
        old.unlink()
    if ELAN_OUT.exists():
        shutil.rmtree(ELAN_OUT)

    print(f"Corpus root: {CORPUS}")
    trials_flat = discover_trials()
    if not trials_flat:
        raise SystemExit(f"No merged trial videos found under {MERGED}")
    print(f"Found {len(trials_flat)} trials. Loading gyroscope.csv (this is the big one)...")
    gyro_all = pd.read_csv(GYRO_CSV)

    video_ids = []
    data_types = {}
    rqa_types = set()  # union of wrist-mdRQA data types actually written
    warnings = []

    for dyad, trial_num, tid in trials_flat:
        if not link_video(dyad, tid):
            warnings.append(f"{tid}: no merged video, skipping trial")
            continue

        channels = []

        def add(channel, result):
            if result is None:
                warnings.append(f"{tid}: no {channel}")
                return
            t, v = result
            write_ts(tid, channel, t, v)
            channels.append(channel)

        add("envelope", load_envelope(tid))
        add("f0", load_f0(tid))
        add("sway", load_sway(gyro_all, dyad, trial_num))

        # Wrist multidimensional-RQA (clue-giver / cam01 only).
        rqa = load_mdrqa(tid)
        if rqa:
            (RQA_OUT / f"{tid}_rqa_data.json").write_text(
                json.dumps({"video_id": tid, "rqa_data": rqa}, indent=2))
            rqa_types.update(rqa.keys())
        else:
            warnings.append(f"{tid}: no wrist mdRQA")

        video_ids.append(tid)
        data_types[tid] = channels

    config = {
        "videoIDs": video_ids,
        "dataTypes": data_types,
        "include_RQA": sorted(rqa_types),
        "include_elan": False,
        "defaultWindowSize": 5,
        "title": "BalanceCorpus — DIMS",
        "subtitle": "Dyadic taboo-game interactions",
        "authors": "Grace Qiyuan Miao, Wim Pouw, Michał Weiss",
        "contacts": "",
    }
    (DIMS_DIR / "config.json").write_text(json.dumps(config, indent=2))

    n_rqa = len(list(RQA_OUT.glob("*_rqa_data.json")))
    print(f"\nWrote config.json with {len(video_ids)} trials "
          f"({n_rqa} with wrist mdRQA: {sorted(rqa_types)}).")
    if warnings:
        print(f"\n{len(warnings)} warnings:")
        for w in warnings:
            print(f"  {w}")


if __name__ == "__main__":
    sys.exit(main())
