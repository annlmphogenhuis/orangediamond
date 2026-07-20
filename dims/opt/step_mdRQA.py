#!/usr/bin/env python3
"""
step_mdRQA.py - Multidimensional Recurrence Quantification Analysis (mdRQA) of the
wrists for the BalanceCorpus motion-tracking time series.

This merges the former step_mdRQA_wrists.py (coordinate/position embedding) and
step_mdRQA_wrists_velocity.py (velocity embedding) into a single pass. For each
input CSV the wrist landmarks (X/Y/Z_LEFT_WRIST and X/Y/Z_RIGHT_WRIST) are embedded
as a MULTIDIMENSIONAL state vector at each frame and recurrence is computed in that
joint space (mdRQA, Wallot, Roepstorff & Mønster, 2016) in TWO flavours:

  - "coordinates": the raw wrist positions
  - "velocity":    the per-frame velocity (d position / d time)

Both analyses are written into ONE JSON per input file, {stem}_mdRQA.json, under
`mdRQA_data` -> {"coordinates": {...}, "velocity": {...}}. Each block carries the
(downsampled) recurrence plot and sliding-window RQA metrics (RR, DET, LAM, L_MAX).

`_hands`/`_body` files are processed; `_face` files (no wrists) are skipped.

Default input/output live under <corpus>/motiontracking/ (this script lives in
orangediamond/dims/opt/ alongside the other step_*RQA scripts).
"""

import numpy as np
import pandas as pd
from scipy.spatial.distance import cdist
import json
import os
import glob
import argparse
from pathlib import Path

# Wrist landmarks used to build the multidimensional embedding. Any of these that
# are present as X_/Y_/Z_ columns in a file become embedding dimensions.
WRIST_LANDMARKS = ['LEFT_WRIST', 'RIGHT_WRIST']

# This script lives in <corpus>/orangediamond/dims/opt/ ; the motion-tracking data
# and its RQA outputs live under <corpus>/motiontracking/.
MOTION_DIR = Path(__file__).resolve().parents[3] / "motiontracking"


def find_wrist_columns(columns):
    """Return the ordered list of wrist coordinate columns present in `columns`.

    Looks for X_/Y_/Z_<LANDMARK> for each landmark in WRIST_LANDMARKS. Visibility
    columns are intentionally excluded so they don't inflate the state space.
    """
    cols = set(columns)
    selected = []
    for landmark in WRIST_LANDMARKS:
        for axis in ('X', 'Y', 'Z'):
            name = f"{axis}_{landmark}"
            if name in cols:
                selected.append(name)
    return selected


def calculate_recurrence_matrix(embedding, threshold=None, target_recurrence=0.07):
    """Multidimensional recurrence matrix for an (N, d) embedding.

    Each dimension is z-normalized independently, then recurrence is defined by
    the Euclidean distance in the joint d-dimensional space. If `threshold` is not
    given it is picked as the `target_recurrence` percentile of pairwise distances.
    """
    # Per-dimension z-normalization so no single axis (e.g. a large-range X)
    # dominates the joint distance.
    mean = np.mean(embedding, axis=0)
    std = np.std(embedding, axis=0)
    std[std == 0] = 1.0
    emb_norm = (embedding - mean) / std

    distance_matrix = cdist(emb_norm, emb_norm, metric='euclidean')

    if threshold is None:
        upper_triangle = distance_matrix[np.triu_indices_from(distance_matrix, k=1)]
        threshold = np.percentile(upper_triangle, target_recurrence * 100)
        print(f"    Calculated threshold: {threshold:.4f} for {target_recurrence*100}% recurrence")

    recurrence_matrix = (distance_matrix <= threshold).astype(np.uint8)

    n = embedding.shape[0]
    actual_recurrence = (np.sum(recurrence_matrix) - n) / (n * n - n) if n > 1 else 0.0
    print(f"    Actual recurrence rate: {actual_recurrence*100:.2f}%")

    return recurrence_matrix, threshold, actual_recurrence


def get_line_lengths(matrix, direction='diagonal', min_len=2, exclude_main_diagonal=False):
    """Lengths of consecutive recurrent runs along diagonals or columns.

    For single-signal (including mdRQA) recurrence set exclude_main_diagonal=True
    to skip the line of identity (k=0), which is trivially all-ones and would
    otherwise dominate DET / L_MAX.
    """
    lengths = []
    rows, cols = matrix.shape

    if direction == 'diagonal':
        for k in range(-rows + 1, cols):
            if exclude_main_diagonal and k == 0:
                continue
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


def calculate_window_metrics(matrix, dt, min_line=2, exclude_main_diagonal=False):
    """RQA metrics (RR, DET, LAM, L_MAX) for one sub-window of the matrix."""
    total_points = matrix.size
    if total_points == 0:
        return 0.0, 0.0, 0.0, 0.0
    recurrence_count = np.sum(matrix)
    rr = recurrence_count / total_points
    if recurrence_count == 0:
        return float(rr), 0.0, 0.0, 0.0
    diag_lines = get_line_lengths(matrix, direction='diagonal', min_len=min_line,
                                  exclude_main_diagonal=exclude_main_diagonal)
    vert_lines = get_line_lengths(matrix, direction='vertical', min_len=min_line)
    det = np.sum(diag_lines) / recurrence_count if recurrence_count > 0 else 0.0
    lam = np.sum(vert_lines) / recurrence_count if recurrence_count > 0 else 0.0
    l_max = (np.max(diag_lines) * dt) if len(diag_lines) > 0 else 0.0
    return float(rr), float(det), float(lam), float(l_max)


def compute_windowed_metrics(matrix, time_values, window_sec=20.0, step_sec=1.0):
    """Slide a square window along the main diagonal of `matrix`, returning
    {time, RR, DET, LAM, L_MAX} so structure can be tracked over time."""
    n = matrix.shape[0]
    dt = float(np.mean(np.diff(time_values))) if len(time_values) > 1 else 0.033
    if dt <= 0:
        dt = 0.033
    win_points = max(2, int(window_sec / dt))
    step_points = max(1, int(step_sec / dt))
    # Short-series adaptation: cap the window to half the series and refine the
    # step so we always get several windows (otherwise a single trivial window
    # renders as a blank metric chart). Long series keep the requested settings.
    win_points = min(win_points, max(2, n // 2))
    n_eff = max(1, n - win_points)
    step_points = max(1, min(step_points, n_eff // 20))
    out = {'time': [], 'RR': [], 'DET': [], 'LAM': [], 'L_MAX': []}
    for start_idx in range(0, max(1, n - win_points), step_points):
        end_idx = start_idx + win_points
        w = matrix[start_idx:end_idx, start_idx:end_idx]
        # Exclude the line of identity (k=0) — trivially recurrent for a signal
        # against itself and would otherwise dominate DET / L_MAX.
        rr, det, lam, l_max = calculate_window_metrics(w, dt, exclude_main_diagonal=True)
        center = time_values[min(start_idx + win_points // 2, n - 1)]
        out['time'].append(float(center))
        out['RR'].append(rr)
        out['DET'].append(det)
        out['LAM'].append(lam)
        out['L_MAX'].append(l_max)
    return out


def matrix_to_sparse_format(matrix):
    """Convert a recurrence matrix to a sparse list of [row, col] pairs (all R==1)."""
    rows, cols = np.where(matrix == 1)
    return [[int(r), int(c)] for r, c in zip(rows, cols)]


def downsample_for_visualization(embedding, time_values, recurrence_matrix, max_points=500):
    """Downsample the embedding, time axis and matrix for a lighter JSON payload."""
    n_points = len(time_values)
    if n_points <= max_points:
        return embedding, time_values, recurrence_matrix
    factor = n_points // max_points
    emb_ds = embedding[::factor]
    time_ds = time_values[::factor]
    matrix_ds = recurrence_matrix[::factor, ::factor]
    print(f"    Downsampled from {n_points} to {len(time_ds)} points for visualization")
    return emb_ds, time_ds, matrix_ds


def run_mdrqa(embedding, time_clean, dim_names, analysis_label, window_sec=20.0, step_sec=1.0):
    """Run one mdRQA flavour on an (N, d) embedding and return its result block."""
    rec_matrix_full, threshold, rec_rate = calculate_recurrence_matrix(embedding)

    emb_vis, time_vis, rec_matrix_vis = downsample_for_visualization(
        embedding, time_clean, rec_matrix_full
    )
    sparse_matrix = matrix_to_sparse_format(rec_matrix_vis)

    windowed_metrics = compute_windowed_metrics(
        rec_matrix_full, time_clean, window_sec=window_sec, step_sec=step_sec
    )

    return {
        'analysis': analysis_label,
        'dimensions': list(dim_names),
        'n_dimensions': len(dim_names),
        'threshold': float(threshold),
        'recurrence_rate': float(rec_rate),
        'time_range': [float(time_clean[0]), float(time_clean[-1])],
        'windowed_metrics': windowed_metrics,
        'visualization': {
            'time': time_vis.tolist(),
            # per-dimension embedding series, keyed by dimension name
            'data': {name: emb_vis[:, i].tolist() for i, name in enumerate(dim_names)},
            'matrix_size': len(time_vis),
            'sparse_matrix': sparse_matrix,  # list of [row, col] pairs
        },
        'full_data': {
            'n_points': len(embedding),
            'time_range': [float(time_clean[0]), float(time_clean[-1])],
        },
    }


def process_file(csv_path, window_sec=20.0, step_sec=1.0):
    """Run both coordinate and velocity wrist mdRQA on one CSV.

    Returns {"coordinates": {...}, "velocity": {...}} or None if the file has no
    usable wrist data.
    """
    print(f"\nProcessing {os.path.basename(csv_path)}")

    try:
        df = pd.read_csv(csv_path)
    except Exception as e:
        print(f"  Error reading file: {e}")
        return None

    # Accept the time column under any casing/whitespace -> canonical 'Time'.
    df = df.rename(columns={c: 'Time' for c in df.columns if str(c).strip().lower() == 'time'})
    if 'Time' not in df.columns:
        print("  Skipping: no 'Time' column")
        return None

    wrist_cols = find_wrist_columns(df.columns)
    if not wrist_cols:
        print("  Skipping: no wrist columns found")
        return None
    print(f"  Wrist dimensions ({len(wrist_cols)}): {', '.join(wrist_cols)}")

    # Drop frames where the time or ANY wrist dimension is missing so every state
    # vector is complete.
    sub = df[['Time'] + wrist_cols].dropna().sort_values('Time')
    time_clean = sub['Time'].values.astype(float)
    position_embedding = sub[wrist_cols].values.astype(float)

    # These corpus files store time in milliseconds (~33 ms/frame at 30 fps), but
    # --window/--step are specified in seconds. Detect the ms case (implausibly
    # large median frame interval) and convert to seconds so windowing is correct.
    if len(time_clean) > 1 and float(np.median(np.diff(time_clean))) > 1.0:
        time_clean = time_clean / 1000.0
        print("  Time column looks like milliseconds -> converted to seconds")

    if len(position_embedding) < 10:
        print(f"  Insufficient data points ({len(position_embedding)})")
        return None

    # Velocity embedding: per-frame gradient of position w.r.t. time (central diff).
    velocity_embedding = np.gradient(position_embedding, time_clean, axis=0)

    print(f"  Coordinates mdRQA: {len(position_embedding)} frames in {position_embedding.shape[1]}-D wrist space")
    coordinates = run_mdrqa(
        position_embedding, time_clean, wrist_cols, 'mdRQA',
        window_sec=window_sec, step_sec=step_sec,
    )

    print(f"  Velocity mdRQA:    {len(velocity_embedding)} frames in {velocity_embedding.shape[1]}-D velocity space")
    velocity = run_mdrqa(
        velocity_embedding, time_clean, [f"V_{c}" for c in wrist_cols], 'mdRQA_velocity',
        window_sec=window_sec, step_sec=step_sec,
    )

    return {'coordinates': coordinates, 'velocity': velocity}


def main():
    parser = argparse.ArgumentParser(description='Coordinate + velocity wrist mdRQA for the motion-tracking corpus')
    parser.add_argument('--input-dir', default=str(MOTION_DIR / 'Output_TimeSeries'),
                        help='Directory of time-series CSVs')
    parser.add_argument('--output-dir', default=str(MOTION_DIR / 'Output_mdRQA'),
                        help='Directory for the per-file combined mdRQA JSON output')
    parser.add_argument('--window', type=float, default=20.0, help='Windowed-metric window size in seconds')
    parser.add_argument('--step', type=float, default=1.0, help='Windowed-metric step in seconds')
    parser.add_argument('--overwrite', action='store_true',
                        help='Recompute files whose output JSON already exists')
    args = parser.parse_args()

    csv_files = sorted(glob.glob(os.path.join(args.input_dir, '*.csv')))
    if not csv_files:
        print(f"No CSV files found in {args.input_dir}")
        return

    os.makedirs(args.output_dir, exist_ok=True)
    print(f"Found {len(csv_files)} CSV files in {args.input_dir}")

    n_ok = n_skip = 0
    for i, csv_path in enumerate(csv_files, 1):
        stem = os.path.splitext(os.path.basename(csv_path))[0]
        output_path = os.path.join(args.output_dir, f"{stem}_mdRQA.json")
        print(f"\n{'='*60}\n[{i}/{len(csv_files)}] {stem}\n{'='*60}")

        if os.path.exists(output_path) and not args.overwrite:
            print("  Output exists, skipping (use --overwrite to recompute)")
            n_skip += 1
            continue

        result = process_file(csv_path, window_sec=args.window, step_sec=args.step)
        if result is None:
            n_skip += 1
            continue

        with open(output_path, 'w') as f:
            json.dump({'source_file': os.path.basename(csv_path), 'mdRQA_data': result}, f, indent=2)
        print(f"  Saved -> {output_path}")
        n_ok += 1

    print(f"\n{'='*60}")
    print(f"mdRQA complete: {n_ok} processed, {n_skip} skipped, {len(csv_files)} total")
    print(f"Output directory: {args.output_dir}")


if __name__ == "__main__":
    main()
