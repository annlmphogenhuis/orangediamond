#!/usr/bin/env python3
"""
Speaker Diarization Script (diarize_speakers.py)

Uses pyannote/speaker-diarization-community-1 to identify speakers in a
video or audio file. Outputs a CSV with columns: time_start, time_end, speaker

Requirements:
    pip install pyannote.audio

Usage (standalone):
    python diarize_speakers.py <video_or_audio> [options]

    Examples:
        python diarize_speakers.py session.mp4
        python diarize_speakers.py session.mp4 --num-speakers 2
        python diarize_speakers.py session.wav -o ./output/

Can also be imported:
    from diarize_speakers import diarize_file
    csv_path = diarize_file("session.mp4", output_dir="./out/", num_speakers=2)
"""

import csv
import os
import subprocess
import tempfile
import argparse
import sys
from pathlib import Path

# PyTorch 2.6 changed torch.load to default weights_only=True.
# pyannote checkpoint files contain many custom classes (Specifications, TorchVersion,
# etc.) that cannot all be individually allowlisted. The safest complete fix is to
# intercept every torch.load call made by pyannote/speechbrain and force
# weights_only=False. These models come from HuggingFace and are trusted sources.
import torch as _torch

_orig_torch_load = _torch.load

def _patched_torch_load(*args, **kwargs):
    kwargs["weights_only"] = False   # force — overrides any explicit True passed by caller
    return _orig_torch_load(*args, **kwargs)

_torch.load = _patched_torch_load
_torch.serialization.load = _patched_torch_load

# Newer huggingface_hub (>=0.28) removed the `use_auth_token` argument from
# hf_hub_download in favour of `token`. pyannote 3.1 still forwards
# `use_auth_token=...` into hf_hub_download, which raises
# TypeError: hf_hub_download() got an unexpected keyword argument 'use_auth_token'.
# Patch hf_hub_download to translate the old kwarg. This must run BEFORE
# pyannote.audio is imported (it is — pyannote is imported lazily inside
# run_diarization), so pyannote binds to the patched function.
import huggingface_hub as _hfh

_orig_hf_hub_download = _hfh.hf_hub_download

def _patched_hf_hub_download(*args, **kwargs):
    if "use_auth_token" in kwargs:
        tok = kwargs.pop("use_auth_token")
        kwargs.setdefault("token", tok)
    return _orig_hf_hub_download(*args, **kwargs)

_hfh.hf_hub_download = _patched_hf_hub_download

MODEL_NAME = "pyannote/speaker-diarization-3.1"

# The token is read from the HF_TOKEN environment variable (see README).
# Never hard-code it here — it must not be committed.
_HF_TOKEN = os.environ.get("HF_TOKEN")

def extract_audio(video_path: str, audio_path: str) -> None:
    """Extract mono 16 kHz WAV from a video using ffmpeg."""
    print(f"  [Audio] Extracting from: {Path(video_path).name}")
    cmd = [
        "ffmpeg", "-y", "-i", str(video_path),
        "-vn", "-ac", "1", "-ar", "16000", "-f", "wav", str(audio_path),
        "-loglevel", "error",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg failed:\n{result.stderr.strip()}")
    print(f"  [Audio] Extracted to: {audio_path}")


def run_diarization(audio_path: str, num_speakers=None, min_speakers=None, max_speakers=None):
    """Run pyannote diarization. Returns a pyannote.core.Annotation."""
    from pyannote.audio import Pipeline

    if _HF_TOKEN:
        os.environ["HUGGINGFACE_HUB_TOKEN"] = _HF_TOKEN
    else:
        raise ValueError(
            "HF_TOKEN not set. This model is gated:\n"
            "  1. Accept terms at https://huggingface.co/pyannote/speaker-diarization-3.1\n"
            "  2. Create token at https://huggingface.co/settings/tokens\n"
            "  3. export HF_TOKEN=\"§§_your_token\""
        )

    print(f"  [Diarization] Loading: {MODEL_NAME}")
    pipeline = Pipeline.from_pretrained(MODEL_NAME, use_auth_token=_HF_TOKEN)

    if _torch.cuda.is_available():
        pipeline = pipeline.to(_torch.device("cuda"))
        print("  [Diarization] Using CUDA")
    elif hasattr(_torch.backends, "mps") and _torch.backends.mps.is_available():
        pipeline = pipeline.to(_torch.device("mps"))
        print("  [Diarization] Using MPS (Apple Silicon)")

    kwargs = {}
    if num_speakers is not None:
        kwargs["num_speakers"] = num_speakers
    if min_speakers is not None:
        kwargs["min_speakers"] = min_speakers
    if max_speakers is not None:
        kwargs["max_speakers"] = max_speakers

    # Load audio via soundfile to bypass torchcodec dependency
    import soundfile as sf
    import numpy as np
    print(f"  [Diarization] Loading audio via soundfile: {audio_path}")
    data, sample_rate = sf.read(str(audio_path))
    waveform = _torch.from_numpy(data).float()
    if waveform.ndim == 1:
        waveform = waveform.unsqueeze(0)
    else:
        waveform = waveform.T  # (samples, channels) -> (channels, samples)
    audio_input = {"waveform": waveform, "sample_rate": sample_rate}

    print(f"  [Diarization n_speakers={num_speakers}] Processing... (this may take a while)")
    return pipeline(audio_input, **kwargs)


# def drop_least_active_speaker(diarization, max_speakers=2):
#     """
#     If the diarization contains more than max_speakers speakers, remove all
#     segments belonging to the speaker with the least total speaking time.
#     Repeats until at most max_speakers remain.
#     Returns the filtered pyannote Annotation.
#     """
#     # pyannote.audio 4.x returns DiarizeOutput; extract the Annotation from it
#     if hasattr(diarization, 'speaker_diarization'):
#         diarization = diarization.speaker_diarization

#     from pyannote.core import Annotation

#     while True:
#         speakers = diarization.labels()
#         if len(speakers) <= max_speakers:
#             break
#         print("Removing quiet speaker")
#         # Compute total speaking time per speaker
#         totals = {s: sum(seg.duration for seg, _, sp in diarization.itertracks(yield_label=True) if sp == s)
#                   for s in speakers}
#         quietest = min(totals, key=totals.get)
#         print(f"  [Diarization] {len(speakers)} speakers detected — dropping '{quietest}' "
#               f"({totals[quietest]:.1f}s total speech)")

#         filtered = Annotation(uri=diarization.uri)
#         for turn, track, speaker in diarization.itertracks(yield_label=True):
#             if speaker != quietest:
#                 filtered[turn, track] = speaker
#         diarization = filtered

#     return diarization


def save_diarization_csv(diarization, output_path: str) -> int:
    """Save annotation to CSV. Returns number of segments written.

    Accepts either a pyannote Annotation or a DiarizeOutput (pyannote.audio >= 4.x).
    """
    # pyannote.audio 4.x returns DiarizeOutput; extract the Annotation from it
    if hasattr(diarization, 'speaker_diarization'):
        diarization = diarization.speaker_diarization
    rows = [
        (f"{turn.start:.4f}", f"{turn.end:.4f}", speaker)
        for turn, _, speaker in diarization.itertracks(yield_label=True)
    ]
    with open(output_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["time_start", "time_end", "speaker"])
        writer.writerows(rows)
    print(f"  [Diarization] Saved {len(rows)} segments → {output_path}")
    return len(rows)


def diarize_file(input_path: str, output_dir: str = None, output_stem: str = None,
                 num_speakers=3, min_speakers=None, max_speakers=None) -> str:
    """Extract audio (if needed) → diarize → save CSV. Returns path to CSV."""
    input_path = Path(input_path)
    if not input_path.exists():
        raise FileNotFoundError(f"Input not found: {input_path}")

    output_dir = Path(output_dir) if output_dir else input_path.parent
    output_dir.mkdir(parents=True, exist_ok=True)

    if output_stem is None:
        output_stem = f"{input_path.stem}_diarization"

    audio_extensions = {".wav", ".mp3", ".flac", ".ogg", ".m4a", ".aac"}
    temp_audio = None

    if input_path.suffix.lower() in audio_extensions:
        audio_path = str(input_path)
    else:
        tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
        tmp.close()
        audio_path = tmp.name
        temp_audio = audio_path
        extract_audio(str(input_path), audio_path)

    try:
        diarization = run_diarization(audio_path, num_speakers, min_speakers, max_speakers)
        # diarization = drop_least_active_speaker(diarization)
    finally:
        if temp_audio and Path(temp_audio).exists():
            Path(temp_audio).unlink()

    output_csv = str(output_dir / f"{output_stem}.csv")
    save_diarization_csv(diarization, output_csv)
    return output_csv


def main():
    parser = argparse.ArgumentParser(description="Speaker diarization via pyannote-audio.")
    parser.add_argument("input", help="Path to video or audio file")
    parser.add_argument("-o", "--output-dir", help="Output directory (default: same as input)")
    parser.add_argument("--num-speakers", type=int, help="Exact number of speakers")
    parser.add_argument("--min-speakers", type=int, help="Minimum speakers")
    parser.add_argument("--max-speakers", type=int, help="Maximum speakers")
    args = parser.parse_args()

    print(f"Input : {args.input}")
    print(f"Output: {args.output_dir or '(same as input)'}")
    try:
        csv_path = diarize_file(
            args.input,
            output_dir=args.output_dir,
            num_speakers=args.num_speakers,
            min_speakers=args.min_speakers,
            max_speakers=args.max_speakers,
        )
        print(f"\nDone → {csv_path}")
    except Exception as e:
        print(f"\n[Error] {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
