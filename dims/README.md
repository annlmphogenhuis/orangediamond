# BalanceCorpus — DIMS dashboard

A [DIMS](https://github.com/dims-network/DIMS_dashboard) instance for the BalanceCorpus
dyadic taboo-game recordings: the merged side-by-side trial video (clue giver + guesser,
with audio) synced to per-trial multimodal time series and gesture annotations.

## Run

```bash
cd Datasets/BalanceCorpus/dims
python3 build_assets.py   # fills assets/ and writes config.json from the corpus data
python3 serve.py 8001     # serves this folder; open http://localhost:8001
```

`serve.py` serves its own directory (not the corpus root) and supports HTTP Range
requests, required for video seeking. Port 8001 keeps it clear of the viewer on 8000.

Re-run `build_assets.py` after regenerating merged videos or changing the source data.
It reads `../viewer/manifest.json` for the trial list, so run the viewer's
`build_manifest.py` first if the trial set changed.

## What build_assets.py produces

For each of the ~80 real (non-`preparation_`) trials, `videoID` = the trial id
(e.g. `103_203_15_1_20250113_152557_bacon_board`):

| Asset | Source | Notes |
|---|---|---|
| `assets/videos/{id}.mp4` | symlink → `../merged/{dyad}/{id}.mp4` | merged clue-giver+guesser video |
| `assets/timeseries/{id}_envelope.csv` | `TS_acoustics/env_*` | amplitude envelope, 50 Hz |
| `assets/timeseries/{id}_f0.csv` | `TS_acoustics/f0_*` | pitch, blank when unvoiced, 50 Hz |
| `assets/timeseries/{id}_gesture_cg.csv` / `_gu.csv` | `gestureclassifications/*_predictions.csv` | LightGBM gesture probability, per person, 30 fps |
| `assets/timeseries/{id}_motion_cg.csv` / `_gu.csv` | `motiontracking/Output_TimeSeries/*_body.csv` | wrist movement speed, per person, 30 fps |
| `assets/timeseries/{id}_sway.csv` | `gyroscope.csv` | balance-board lean (unwrapped AngleX vs median), 50 Hz |
| `assets/elan/{id}.eaf` | `gestureclassifications/*.mp4.eaf` | clue-giver + guesser gesture tiers merged, prefixed `CG_` / `GU_` |

Each timeseries CSV is `Time,<channel>` with `Time` in seconds (the DIMS contract:
exactly a time column plus one measurement). A channel is omitted from a trial's
`config.json` `dataTypes` when its source file is missing, so the dashboard never
requests a nonexistent CSV. Two trials where neither person gestured have no `.eaf`;
opening the ELAN tab for those shows a harmless 404.

## Adding the optional analyses later

The template's precompute scripts are kept under `opt/` (`pip install -r opt/requirements.txt`):

- **RQA** — add `"include_RQA": ["motion_cg", ...]` to `config.json`, then
  `python opt/step_RQA.py --config config.json --output-dir assets/rqa`
- **Cross-wavelet** — add `"include_crosswavelet": [["motion_cg","motion_gu"], ...]`, then
  `python opt/step_crosswavelet.py --config config.json --output-dir assets/crosswavelet`
- **Cross-RQA** — add `"include_cRQA": [["gesture_cg","gesture_gu"], ...]`, then
  `python opt/step_cRQA.py --config config.json --output-dir assets/crqa`

The clue-giver vs guesser channel pairs (`motion_cg`/`motion_gu`, `gesture_cg`/`gesture_gu`)
are the natural inputs for the between-person coupling analyses.
