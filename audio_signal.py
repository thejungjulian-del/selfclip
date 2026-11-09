#!/usr/bin/env python3
"""
Audio-loudness moment detection — the signal for platforms where chat isn't reachable.

Kick blocks its API *and* its plain pages with Cloudflare (403 on both, verified), so there
is no chatroom id to subscribe to and no chat to read. Chat-reaction detection simply cannot
work there. Audio can: when something happens on stream, the streamer reacts loudly, and
that is measurable straight off the recorded segments.

Measured on a live Kick capture, per-2s RMS ranged from about -45 dB (quiet) to -30 dB
(loud) — a ~14 dB spread, plenty to separate a reaction from room tone.

This is also a useful second signal on Twitch, where chat velocity turned out to be worse
than random (0.44x lift) against clip-calls.

    python3 audio_signal.py levels <file.ts>     # per-window RMS, and which windows spike
"""
import re
import statistics as st
import subprocess
import sys
from collections import deque

WINDOW = 2.0          # seconds per measurement window
BASE_WINDOW = 25      # windows of history for the rolling baseline
SPIKE_DB = 6.0        # dB above baseline to call a reaction
MIN_DB = -50.0        # ignore near-silence; a dead mic must not look like a spike


def levels(path, window=WINDOW):
    """Per-window RMS level in dB, in order. Empty list if the file has no readable audio."""
    try:
        r = subprocess.run(
            ["ffmpeg", "-hide_banner", "-nostats", "-i", path,
             "-af", f"astats=metadata=1:reset={window},"
                    "ametadata=print:key=lavfi.astats.Overall.RMS_level:file=-",
             "-f", "null", "-"],
            capture_output=True, text=True, timeout=120)
    except Exception:
        return []
    out = []
    for m in re.finditer(r"RMS_level=(-?[\d.]+)", r.stdout + r.stderr):
        try:
            v = float(m.group(1))
        except ValueError:
            continue
        # astats emits -inf style values as huge negatives on silent windows
        if v > -200:
            out.append(v)
    return out


def find_spikes(vals, spike_db=SPIKE_DB, min_db=MIN_DB):
    """Indices of windows whose level sits spike_db above the trailing median.

    Returns [(index, level, delta_vs_baseline)]. Uses a median rather than a mean so one
    very loud window doesn't raise the bar against itself.
    """
    spikes, hist = [], deque(maxlen=BASE_WINDOW)
    for i, v in enumerate(vals):
        base = st.median(hist) if len(hist) >= 5 else None
        if base is not None and v >= min_db and (v - base) >= spike_db:
            spikes.append((i, v, v - base))
        hist.append(v)
    return spikes


def summarise(path, window=WINDOW):
    vals = levels(path, window)
    if not vals:
        print("  no readable audio")
        return [], []
    sp = find_spikes(vals)
    print(f"  {len(vals)} windows of {window}s   "
          f"min {min(vals):.1f} dB   median {st.median(vals):.1f} dB   max {max(vals):.1f} dB")
    print(f"  spread {max(vals)-min(vals):.1f} dB   spikes (>= {SPIKE_DB} dB over baseline): {len(sp)}")
    for i, v, d in sp[:12]:
        print(f"    t={i*window:6.1f}s  {v:7.1f} dB  (+{d:.1f} over baseline)")
    return vals, sp


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "levels":
        summarise(sys.argv[2], float(sys.argv[3]) if len(sys.argv) > 3 else WINDOW)
    else:
        print(__doc__)
