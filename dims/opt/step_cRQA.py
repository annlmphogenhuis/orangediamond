#!/usr/bin/env python3
"""
step_cRQA.py - Generate Cross-Recurrence Quantification Analysis data for the DIMS Dashboard.

Cross-RQA quantifies recurrence between TWO different time series (e.g. a teacher
and a student signal), as opposed to step_RQA.py which analyses a single series
against itself.

This step produces, per data-type pair:
  1. The FULL cross-recurrence plot (RP) - every recurrent point, downsampled to
     <=500x500 for the browser (same scheme as step_RQA.py). This is what the
     dashboard renders.
  2. Windowed RQA metrics (RR / DET / LAM / L_MAX) computed on the FULL-resolution
     matrix by sliding a square window along the line of synchronization (the main
     diagonal), so coupling strength can be tracked over time.

Reads 'videoIDs' and 'include_cRQA' from config.json. 'include_cRQA' is a list of
pairs, each a 2-element list of data-types to compare, e.g.
    "include_cRQA": [["bodysync", "neuralsync"]]
Loads assets/timeseries/{videoID}_{dataType}.csv and writes
    assets/crqa/{videoID}_crqa_data.json

Usage:
    python step_cRQA.py --config config.json --output-dir assets/crqa
"""

import numpy as np
import pandas as pd
from scipy.spatial.distance import cdist
import json
import os
import argparse

MAX_POINTS = 500  # visualization cap, matching step_RQA.py


# ============================================================================
# ALGORITHMS
# ============================================================================

def calculate_cross_recurrence_matrix(emb1, emb2, threshold=None, target_recurrence=0.07):
    """Cross-recurrence matrix between two (embedded) series.

    emb1/emb2 are (N, d) arrays. Returns (matrix, threshold, actual_recurrence).
    Threshold is auto-picked as the target_recurrence percentile of all distances.
    """
    distance_matrix = cdist(emb1, emb2, metric='euclidean')

    if threshold is None:
        threshold = np.percentile(distance_matrix, target_recurrence * 100)
        print(f"    > Calculated threshold: {threshold:.4f} (Target RR: {target_recurrence*100}%)")

    recurrence_matrix = (distance_matrix <= threshold).astype(np.uint8)
    actual_recurrence = np.sum(recurrence_matrix) / recurrence_matrix.size
    return recurrence_matrix, threshold, actual_recurrence


def matrix_to_sparse_format(matrix):
    """Convert a recurrence matrix to a sparse list of [row, col] pairs (all R==1).

    Unlike a banded variant, this keeps the COMPLETE recurrence plot so the full
    structure is visible in the dashboard.
    """
    rows, cols = np.where(matrix == 1)
    return [[int(r), int(c)] for r, c in zip(rows, cols)]


def downsample_for_visualization(ts1, ts2, time_values, recurrence_matrix, max_points=MAX_POINTS):
    """Downsample the full matrix (and the two series) for a lighter JSON payload."""
    n_points = len(time_values)
    if n_points <= max_points:
        return ts1, ts2, time_values, recurrence_matrix
    factor = n_points // max_points
    return (
        ts1[::factor],
        ts2[::factor],
        time_values[::factor],
        recurrence_matrix[::factor, ::factor],
    )


def get_line_lengths(matrix, direction='diagonal', min_len=2):
    """Lengths of consecutive recurrent runs along diagonals or columns."""
    lengths = []
    rows, cols = matrix.shape

    if direction == 'diagonal':
        for k in range(-rows + 1, cols):
            diag = matrix.diagonal(k)
            if len(diag) < min_len:
                continue
            padded = np.pad(diag, (1, 1), 'constant').astype(int)
            diff = np.diff(padded)
            starts = np.where(diff == 1)[0]
            ends = np.where(diff == -1)[0]
            seq_lens = ends - starts
            lengths.extend(seq_lens[seq_lens >= min_len])

    elif direction == 'vertical':
        for col_idx in range(cols):
            col = matrix[:, col_idx]
            if np.sum(col) < min_len:
                continue
            padded_col = np.pad(col, (1, 1), 'constant').astype(int)
            col_diff = np.diff(padded_col)
            starts = np.where(col_diff == 1)[0]
            ends = np.where(col_diff == -1)[0]
            l = ends - starts
            lengths.extend(l[l >= min_len])

    return np.array(lengths)


def calculate_window_metrics(matrix, dt, min_line=2):
    """RQA metrics (RR, DET, LAM, L_MAX) for one sub-window of the matrix."""
    total_points = matrix.size
    if total_points == 0:
        return 0.0, 0.0, 0.0, 0.0

    recurrence_count = np.sum(matrix)
    rr = recurrence_count / total_points
    if recurrence_count == 0:
        return float(rr), 0.0, 0.0, 0.0

    diag_lines = get_line_lengths(matrix, direction='diagonal', min_len=min_line)
    vert_lines = get_line_lengths(matrix, direction='vertical', min_len=min_line)

    det = np.sum(diag_lines) / recurrence_count if recurrence_count > 0 else 0.0
    lam = np.sum(vert_lines) / recurrence_count if recurrence_count > 0 else 0.0
    l_max = (np.max(diag_lines) * dt) if len(diag_lines) > 0 else 0.0

    return float(rr), float(det), float(lam), float(l_max)


# ============================================================================
# DATA HANDLING
# ============================================================================

def _load_series(path):
    """Read a {value, Time} CSV; return (time, value) arrays sorted by time, NaNs dropped."""
    df = pd.read_csv(path)
    # Accept the time column under any casing/whitespace -> canonical 'Time'.
    df = df.rename(columns={c: 'Time' for c in df.columns if str(c).strip().lower() == 'time'})
    if 'Time' not in df.columns:
        print(f"  [Error] '{path}' has no 'Time' column.")
        return None, None
    value_col = [c for c in df.columns if c != 'Time'][0]
    sub = df[['Time', value_col]].dropna().sort_values('Time')
    return sub['Time'].values.astype(float), sub[value_col].values.astype(float)


def load_and_align_data(video_id, type1, type2, input_dir="assets/timeseries"):
    """Load two CSVs and align them onto a common uniform time grid via linear
    interpolation (the two series rarely share identical timestamps), then
    z-normalize. Returns (s1_norm, s2_norm, common_time)."""
    path1 = os.path.join(input_dir, f"{video_id}_{type1}.csv")
    path2 = os.path.join(input_dir, f"{video_id}_{type2}.csv")

    if not os.path.exists(path1) or not os.path.exists(path2):
        print(f"  [Warning] Missing file(s) for pair {type1} <-> {type2}:")
        if not os.path.exists(path1):
            print(f"    Missing: {path1}")
        if not os.path.exists(path2):
            print(f"    Missing: {path2}")
        return None, None, None

    try:
        t1, v1 = _load_series(path1)
        t2, v2 = _load_series(path2)
    except Exception as e:
        print(f"  [Error] Failed to read CSVs: {e}")
        return None, None, None
    if t1 is None or t2 is None or len(t1) < 10 or len(t2) < 10:
        print("  [Error] Insufficient or malformed data in one of the series.")
        return None, None, None

    # Common overlapping time range, sampled on a uniform grid at the finer of the
    # two median sampling intervals.
    t_start = max(t1[0], t2[0])
    t_end = min(t1[-1], t2[-1])
    if t_end <= t_start:
        print(f"  [Error] Series time ranges do not overlap "
              f"([{t1[0]:.2f},{t1[-1]:.2f}] vs [{t2[0]:.2f},{t2[-1]:.2f}]).")
        return None, None, None

    dt = min(float(np.median(np.diff(t1))), float(np.median(np.diff(t2))))
    if dt <= 0:
        dt = (t_end - t_start) / 1000.0
    n = int(np.floor((t_end - t_start) / dt)) + 1
    if n < 10:
        print(f"  [Error] Insufficient overlapping samples ({n}).")
        return None, None, None
    common_time = t_start + np.arange(n) * dt

    raw_s1 = np.interp(common_time, t1, v1)
    raw_s2 = np.interp(common_time, t2, v2)

    s1_norm = (raw_s1 - np.mean(raw_s1)) / (np.std(raw_s1) + 1e-6)
    s2_norm = (raw_s2 - np.mean(raw_s2)) / (np.std(raw_s2) + 1e-6)
    return s1_norm, s2_norm, common_time


# ============================================================================
# MAIN
# ============================================================================

def main():
    parser = argparse.ArgumentParser(description='Generate cross-RQA data for the DIMS Dashboard')
    parser.add_argument('--config', default='config.json', help='Path to config.json')
    parser.add_argument('--output-dir', default='assets/crqa', help='Output directory')
    parser.add_argument('--window', type=float, default=20.0, help='Sliding window size in seconds')
    parser.add_argument('--step', type=float, default=1.0, help='Window step in seconds')
    args = parser.parse_args()

    try:
        with open(args.config, 'r') as f:
            config = json.load(f)
    except FileNotFoundError:
        print(f"Error: Config file '{args.config}' not found.")
        return

    if 'include_cRQA' not in config or not config['include_cRQA']:
        print("No cRQA requested in config (include_cRQA not found or empty)")
        return

    raw_pairs = config['include_cRQA']
    valid_pairs = []
    for item in raw_pairs:
        if isinstance(item, list) and len(item) == 2:
            valid_pairs.append(item)
        else:
            print(f"Warning: Skipping invalid include_cRQA entry: {item}. Expected [type1, type2].")
    if not valid_pairs:
        print("Error: No valid pairs found in include_cRQA.")
        return

    video_ids = config.get('videoIDs', [])
    if not video_ids:
        print("Error: 'videoIDs' list is empty in config.")
        return

    os.makedirs(args.output_dir, exist_ok=True)

    for vid in video_ids:
        print(f"\n{'='*60}")
        print(f"Processing video: {vid}")
        print(f"{'='*60}")

        video_results = {}
        for type1, type2 in valid_pairs:
            print(f"\nComparing: {type1} <-> {type2}")

            ts1, ts2, time_vals = load_and_align_data(vid, type1, type2)
            if ts1 is None:
                continue

            # Analyse the raw 1-D signals (column vectors for cdist).
            emb1, emb2 = ts1.reshape(-1, 1), ts2.reshape(-1, 1)
            ts1_1d, ts2_1d = ts1, ts2

            # Full-resolution cross-recurrence matrix (used for metrics).
            rec_matrix, threshold, global_rr = calculate_cross_recurrence_matrix(emb1, emb2)
            print(f"  > Global cross-recurrence rate: {global_rr*100:.2f}%")

            # Windowed metrics along the line of synchronization (main diagonal).
            dt = float(np.mean(np.diff(time_vals)))
            if dt <= 0:
                dt = 0.033
            n = len(emb1)
            win_points = max(2, int(args.window / dt))
            step_points = max(1, int(args.step / dt))
            # Short-series adaptation: cap the window to half the series and refine
            # the step so we always get several windows (otherwise a single trivial
            # window renders as a blank metric chart). Long series are unaffected.
            win_points = min(win_points, max(2, n // 2))
            n_eff = max(1, n - win_points)
            step_points = max(1, min(step_points, n_eff // 20))

            windowed_metrics = {'time': [], 'RR': [], 'DET': [], 'LAM': [], 'L_MAX': []}
            print(f"  > Windowed metrics (window={args.window}s, step={args.step}s)...")
            for start_idx in range(0, max(1, n - win_points), step_points):
                end_idx = start_idx + win_points
                w_matrix = rec_matrix[start_idx:end_idx, start_idx:end_idx]
                rr, det, lam, l_max = calculate_window_metrics(w_matrix, dt)
                center_time = time_vals[min(start_idx + win_points // 2, n - 1)]
                windowed_metrics['time'].append(float(center_time))
                windowed_metrics['RR'].append(rr)
                windowed_metrics['DET'].append(det)
                windowed_metrics['LAM'].append(lam)
                windowed_metrics['L_MAX'].append(l_max)

            # Full recurrence plot for visualization, downsampled to <=500x500.
            ts1_vis, ts2_vis, time_vis, matrix_vis = downsample_for_visualization(
                ts1_1d, ts2_1d, time_vals, rec_matrix
            )
            sparse_matrix = matrix_to_sparse_format(matrix_vis)

            pair_key = f"{type1}_vs_{type2}"
            video_results[pair_key] = {
                'pair_name': pair_key,
                'series_names': [type1, type2],
                'threshold': float(threshold),
                'global_recurrence_rate': float(global_rr),
                'time_range': [float(time_vals[0]), float(time_vals[-1])],
                'windowed_metrics': windowed_metrics,
                'visualization': {
                    'time': time_vis.tolist(),
                    'data_x': ts1_vis.tolist(),
                    'data_y': ts2_vis.tolist(),
                    'matrix_size': len(time_vis),
                    'sparse_matrix': sparse_matrix,  # full RP, downsampled
                },
                'full_stats': {
                    'n_points': int(n),
                    'window_size_sec': args.window,
                    'step_size_sec': args.step,
                },
            }
            print(f"  > {pair_key}: matrix {len(time_vis)}x{len(time_vis)}, "
                  f"{len(sparse_matrix)} recurrent points, {len(windowed_metrics['time'])} windows")

        if video_results:
            output_path = os.path.join(args.output_dir, f"{vid}_crqa_data.json")
            with open(output_path, 'w') as f:
                json.dump({'video_id': vid, 'crqa_data': video_results}, f, indent=2)
            print(f"\nSaved cRQA data to {output_path}")
        else:
            print(f"\n[INFO] No cRQA results generated for video {vid}")

    print("\ncRQA processing complete!")


if __name__ == "__main__":
    main()
