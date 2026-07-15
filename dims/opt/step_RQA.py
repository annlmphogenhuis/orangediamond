#!/usr/bin/env python3
"""
generate_rqa.py - Generate Recurrence Quantification Analysis data for DIMS Dashboard

Usage:
    python generate_rqa.py --config config.json --output-dir assets/rqa
"""

import numpy as np
import pandas as pd
from scipy.spatial.distance import cdist
from scipy.sparse import csr_matrix
import json
import os
import argparse

def calculate_recurrence_matrix(time_series, threshold=None, target_recurrence=0.07):
    """
    Calculate recurrence matrix for a time series.
    
    Parameters:
    - time_series: 1D array of time series data
    - threshold: fixed threshold (if None, will be calculated for target_recurrence)
    - target_recurrence: target recurrence rate (default 5%)
    """
    # Normalize the time series
    ts_normalized = (time_series - np.mean(time_series)) / np.std(time_series)
    
    # Reshape for distance calculation
    ts_reshaped = ts_normalized.reshape(-1, 1)
    
    # Calculate distance matrix
    distance_matrix = cdist(ts_reshaped, ts_reshaped, metric='euclidean')
    
    # If threshold not provided, calculate it to achieve target recurrence rate
    if threshold is None:
        # Flatten upper triangle of distance matrix (excluding diagonal)
        upper_triangle = distance_matrix[np.triu_indices_from(distance_matrix, k=1)]
        # Find threshold that gives target recurrence rate
        threshold = np.percentile(upper_triangle, target_recurrence * 100)
        print(f"  Calculated threshold: {threshold:.4f} for {target_recurrence*100}% recurrence")
    
    # Create recurrence matrix
    recurrence_matrix = (distance_matrix <= threshold).astype(np.uint8)
    
    # Calculate actual recurrence rate
    n = len(time_series)
    actual_recurrence = (np.sum(recurrence_matrix) - n) / (n * n - n)
    print(f"  Actual recurrence rate: {actual_recurrence*100:.2f}%")
    
    return recurrence_matrix, threshold, actual_recurrence

def get_line_lengths(matrix, direction='diagonal', min_len=2, exclude_main_diagonal=False):
    """Lengths of consecutive recurrent runs along diagonals or columns.

    For single-series RQA set exclude_main_diagonal=True to skip the line of
    identity (k=0), which is trivially all-ones and would otherwise dominate
    DET / L_MAX.
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
    {time, RR, DET, LAM, L_MAX} so coupling/structure can be tracked over time."""
    n = matrix.shape[0]
    dt = float(np.mean(np.diff(time_values))) if len(time_values) > 1 else 0.033
    if dt <= 0:
        dt = 0.033
    win_points = max(2, int(window_sec / dt))
    step_points = max(1, int(step_sec / dt))
    # Short-series adaptation: if the requested window doesn't fit, a single
    # trivial window is produced and the metric chart renders blank. Cap the
    # window to half the series and refine the step so we always get several
    # windows. Long series keep the requested window/step unchanged.
    win_points = min(win_points, max(2, n // 2))
    n_eff = max(1, n - win_points)
    step_points = max(1, min(step_points, n_eff // 20))
    out = {'time': [], 'RR': [], 'DET': [], 'LAM': [], 'L_MAX': []}
    for start_idx in range(0, max(1, n - win_points), step_points):
        end_idx = start_idx + win_points
        w = matrix[start_idx:end_idx, start_idx:end_idx]
        # Exclude the line of identity (k=0) — in single-series RQA it is
        # trivially recurrent and would otherwise dominate DET / L_MAX.
        rr, det, lam, l_max = calculate_window_metrics(w, dt, exclude_main_diagonal=True)
        center = time_values[min(start_idx + win_points // 2, n - 1)]
        out['time'].append(float(center))
        out['RR'].append(rr)
        out['DET'].append(det)
        out['LAM'].append(lam)
        out['L_MAX'].append(l_max)
    return out

def matrix_to_sparse_format(matrix):
    """
    Convert recurrence matrix to sparse format for efficient storage.
    Returns list of [row, col] pairs where recurrence is 1.
    """
    # Get indices where matrix is 1
    rows, cols = np.where(matrix == 1)
    
    # Combine into list of pairs
    sparse_data = [[int(r), int(c)] for r, c in zip(rows, cols)]
    
    return sparse_data

def downsample_for_visualization(time_series, time_values, recurrence_matrix, max_points=500):
    """
    Downsample data for visualization if too large.
    """
    n_points = len(time_series)
    
    if n_points <= max_points:
        return time_series, time_values, recurrence_matrix
    
    # Calculate downsampling factor
    factor = n_points // max_points
    
    # Downsample time series
    time_ds = time_values[::factor]
    data_ds = time_series[::factor]
    
    # Downsample recurrence matrix
    matrix_ds = recurrence_matrix[::factor, ::factor]
    
    print(f"  Downsampled from {n_points} to {len(time_ds)} points for visualization")
    
    return data_ds, time_ds, matrix_ds

def process_rqa_for_datatype(video_id, data_type, window_sec=20.0, step_sec=1.0):
    """
    Process RQA for a specific data type.
    """
    csv_path = f"assets/timeseries/{video_id}_{data_type}.csv"
    
    if not os.path.exists(csv_path):
        print(f"Warning: File not found: {csv_path}")
        return None
    
    print(f"\nProcessing {video_id} - {data_type}")
    
    # Load data
    df = pd.read_csv(csv_path)
    # Accept the time column under any casing/whitespace -> canonical 'Time'.
    df = df.rename(columns={c: 'Time' for c in df.columns if str(c).strip().lower() == 'time'})

    # Get time column
    if 'Time' not in df.columns:
        print(f"Error: No 'Time' column in {csv_path}")
        return None
    
    # Get all non-time columns
    data_cols = [col for col in df.columns if col != 'Time']
    
    # If multiple columns we take the first one
    data_col = data_cols[0]
    
    # Clean data
    mask = ~pd.isna(df[data_col])
    time_clean = df['Time'][mask].values
    data_clean = df[data_col][mask].values
    
    if len(data_clean) < 10:
        print(f"  Insufficient data points ({len(data_clean)})")
        return None
    
    print(f"  Processing {len(data_clean)} data points")
    
    # Calculate full recurrence matrix
    rec_matrix_full, threshold, rec_rate = calculate_recurrence_matrix(data_clean)
    
    # Downsample for visualization
    data_vis, time_vis, rec_matrix_vis = downsample_for_visualization(
        data_clean, time_clean, rec_matrix_full
    )
    
    # Convert to sparse format
    sparse_matrix = matrix_to_sparse_format(rec_matrix_vis)

    # Windowed metrics on the full-resolution matrix (sliding window along the diagonal)
    windowed_metrics = compute_windowed_metrics(
        rec_matrix_full, time_clean, window_sec=window_sec, step_sec=step_sec
    )

    # Prepare output data
    result = {
        'data_type': data_type,
        'threshold': float(threshold),
        'recurrence_rate': float(rec_rate),
        'time_range': [float(time_clean[0]), float(time_clean[-1])],
        'windowed_metrics': windowed_metrics,
        'visualization': {
            'time': time_vis.tolist(),
            'data': data_vis.tolist(),
            'matrix_size': len(time_vis),
            'sparse_matrix': sparse_matrix  # List of [row, col] pairs
        },
        'full_data': {
            'n_points': len(data_clean),
            'time_range': [float(time_clean[0]), float(time_clean[-1])]
        }
    }

    return result

def main():
    parser = argparse.ArgumentParser(description='Generate RQA data for DIMS Dashboard')
    parser.add_argument('--config', default='config.json', help='Path to config.json')
    parser.add_argument('--output-dir', default='assets/rqa', help='Output directory for RQA data')
    parser.add_argument('--window', type=float, default=20.0, help='Windowed-metric window size in seconds')
    parser.add_argument('--step', type=float, default=1.0, help='Windowed-metric step in seconds')
    args = parser.parse_args()
    
    # Load config
    with open(args.config, 'r') as f:
        config = json.load(f)
    
    # Check if RQA is requested
    if 'include_RQA' not in config or not config['include_RQA']:
        print("No RQA requested in config (include_RQA not found or empty)")
        return
    
    # Create output directory
    os.makedirs(args.output_dir, exist_ok=True)
    
    # Process each video
    for video_id in config['videoIDs']:
        print(f"\n{'='*50}")
        print(f"Processing video: {video_id}")
        print(f"{'='*50}")
        
        # Get data types to process for RQA (remove duplicates)
        rqa_data_types = list(dict.fromkeys(config['include_RQA']))
        
        # Process each data type
        rqa_results = {}
        for data_type in rqa_data_types:
            result = process_rqa_for_datatype(video_id, data_type,
                                              window_sec=args.window, step_sec=args.step)
            if result:
                rqa_results[data_type] = result
        
        # Save combined data
        if rqa_results:
            output_path = os.path.join(args.output_dir, f"{video_id}_rqa_data.json")
            with open(output_path, 'w') as f:
                json.dump({
                    'video_id': video_id,
                    'rqa_data': rqa_results
                }, f, indent=2)
            print(f"\nSaved RQA data to {output_path}")
            
            # Print summary
            print("\nSummary:")
            for data_type, result in rqa_results.items():
                print(f"  {data_type}:")
                print(f"    - Recurrence rate: {result['recurrence_rate']*100:.2f}%")
                print(f"    - Matrix size: {result['visualization']['matrix_size']}x{result['visualization']['matrix_size']}")
                print(f"    - Sparse points: {len(result['visualization']['sparse_matrix'])}")
    
    print("\nRQA processing complete!")

if __name__ == "__main__":
    main()