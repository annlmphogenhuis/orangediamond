# orangediamond

Tooling built on top of the **BalanceCorpus** dyadic-interaction dataset:

- **`dims/`** — a [DIMS](https://github.com/dims-network/DIMS_dashboard) dashboard that plays the per-trial merged video (clue giver + guesser, side by side, with audio) alongside per-trial time-series channels (speech envelope, F0, balance-board sway).
- **`diarize_speakers.py`** / **`diarization.ipynb`** — speaker diarization (who spoke when) for the trial audio/video, via `pyannote.audio`.

This repository holds **only code** — the dataset itself (videos, audio, sensor CSVs, the merged clips) is large and lives **outside** the repo, in the surrounding BalanceCorpus folder.

---

## Where to clone it

The scripts locate the dataset by walking **up** from their own location until they find a directory that contains both `merged/` and `metadata.csv`. So this repo must be cloned **inside the BalanceCorpus dataset folder**:

```
Datasets/BalanceCorpus/              ← the dataset (the "corpus root")
├── metadata.csv                     trial ↔ participant ↔ condition table
├── demographics.csv
├── gyroscope.csv                    balance-board IMU (→ sway channel)
├── TS_acoustics/                    envelope + F0 per trial (→ audio channels)
├── videos/                          raw per-camera videos
├── audios/                          raw per-trial audio
├── merged/                          per-trial merged videos  ← NOT in this repo (see below)
│   ├── 103_203/*.mp4
│   └── 108_208/*.mp4
├── motiontracking/  gestureclassifications/  OpenFace/  ...   other modalities
└── orangediamond/                   ← clone THIS repo here
    ├── dims/
    ├── diarize_speakers.py
    └── ...
```

Clone:

```bash
cd Datasets/BalanceCorpus
git clone https://github.com/annlmphogenhuis/orangediamond.git
```

Because the corpus root is auto-detected, the dashboard folder can sit anywhere under the
corpus — but the layout above is the expected one.

---

## The `merged/` videos live at the corpus root, not in the repo

The merged per-trial clips (`merged/{dyad}/{trial_id}.mp4`) are ~180 MB of binary video and are
deliberately **not tracked** here (`/merged` and `*.mp4` are in `.gitignore`). They must sit at the
**corpus root**: `Datasets/BalanceCorpus/merged/`.

If a `merged/` folder ends up inside this repo (e.g. from an older checkout), move it up to the
corpus root:

```bash
mv orangediamond/merged/* ../merged/     # then remove the empty orangediamond/merged
```

`dims/build_assets.py` reads the videos from `<corpus root>/merged/`, so as long as they are there
the dashboard finds them regardless of this repo's contents.

---

## DIMS dashboard — quick start

```bash
cd orangediamond/dims
python build_assets.py     # discovers trials from ../../merged and fills dims/assets/ + config.json
python serve.py 8001       # serves the corpus root; open http://localhost:8001/dims/
```

`build_assets.py` writes, per trial: a symlink to the merged video, and `Time,<channel>` CSVs for
`envelope`, `f0` (from `TS_acoustics/`) and `sway` (from `gyroscope.csv`). It needs `pandas` and
`numpy`. See **[`dims/README.md`](dims/README.md)** for the full asset layout and how to enable the
optional RQA / cross-wavelet analyses.

---

## Speaker diarization

`diarize_speakers.py` extracts mono 16 kHz audio (via `ffmpeg`) and runs the gated
`pyannote/speaker-diarization-3.1` pipeline, writing a CSV of `time_start,time_end,speaker`.

```bash
pip install pyannote.audio soundfile     # ffmpeg must also be on PATH

export HF_TOKEN="hf_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"   # see next section

python diarize_speakers.py ../merged/103_203/103_203_15_1_20250113_152557_bacon_board.mp4 \
    --num-speakers 2 -o ./out/
```

It can also be imported: `from diarize_speakers import diarize_file`. The token is read from the
`HF_TOKEN` environment variable — it is **never** hard-coded in the script.

---

## Generating a HuggingFace token

The diarization model is **gated**, so you need a (free) HuggingFace account and a read token, and
you must accept the model terms first:

1. **Create/sign in** to an account at <https://huggingface.co>.
2. **Accept the gated-model terms** (one click each, while signed in):
   - <https://huggingface.co/pyannote/speaker-diarization-3.1>
   - <https://huggingface.co/pyannote/segmentation-3.0>  (a dependency of the pipeline)
3. **Create a token**: <https://huggingface.co/settings/tokens> → **New token** → type **Read** → copy it (it starts with `hf_`).
4. **Expose it to the script** via an environment variable:
   ```bash
   export HF_TOKEN="hf_your_token_here"          # current shell only
   echo 'export HF_TOKEN="hf_your_token_here"' >> ~/.zshrc   # persist across sessions
   ```

> **Never commit the token.** Keep it in `HF_TOKEN` (or a local, git-ignored `.env`). If a token is
> ever pasted into a file and pushed, treat it as compromised: revoke it at
> <https://huggingface.co/settings/tokens> and issue a new one. GitHub push protection will block a
> push that contains a token anywhere in its commits.
