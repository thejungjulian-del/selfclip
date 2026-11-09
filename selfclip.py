#!/usr/bin/env python3
"""
Turn OBS replay-buffer saves into post-ready vertical clips.

OBS already solves capture: its Replay Buffer keeps the last N seconds in RAM and
writes them to disk when you hit the hotkey, at source quality with no re-encode.
So this does NOT record anything. It watches the folder OBS writes into and
finishes each save:

    OBS replay hotkey -> raw 16:9 .mkv -> [ layout 9:16 ] -> [ captions ] -> ready/

Set up in OBS once:
    Settings > Output > Replay Buffer: enable, 60s (memory is cheap, hindsight isn't)
    Settings > Hotkeys > "Save Replay"  -> bind it
    Settings > Output > Recording Path  -> the folder you pass to `watch`

Why 60s: you hit the hotkey AFTER the moment lands, so the buffer has to reach
backwards far enough to contain it. 60s of slack costs nothing and saves the take.

Usage:
    python3 selfclip.py tune <file>        # pick your layout from a real frame FIRST
    python3 selfclip.py once <file>        # process one file
    python3 selfclip.py watch [folder]     # process every new save, until Ctrl-C
"""

import os
import subprocess
import sys
import time

import captions
import layout

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "ready")
# OBS on macOS defaults its recording path here.
DEFAULT_WATCH = os.path.expanduser("~/Movies")
EXTS = (".mkv", ".mp4", ".mov", ".flv")

# Your own stream's framing. Tune this with `tune` before a real session — the
# default centre-crop is a guess, and a wrong crop is the difference between a
# clip that reads and one that wastes half the frame. See layout.LAYOUTS.
LAYOUT_KEY = "_selfstream"

CAPTIONS = True
POLL = 3.0          # seconds between folder scans
STABLE_CHECKS = 2   # consecutive equal sizes before a file counts as fully written


def out_path(src):
    base = os.path.splitext(os.path.basename(src))[0]
    return os.path.join(OUT, f"{base}_9x16.mp4")


def process(src):
    """Raw OBS save -> vertical, captioned, in ready/. Returns (path, error)."""
    os.makedirs(OUT, exist_ok=True)
    final = out_path(src)
    if os.path.exists(final):
        return final, None

    t0 = time.time()
    staged = final if not CAPTIONS else final.replace("_9x16.mp4", "_9x16_raw.mp4")
    path, err = layout.render(src, staged, channel=LAYOUT_KEY)
    if err:
        return None, f"layout failed: {err}"

    if CAPTIONS:
        try:
            captions.add(staged, final)
            os.remove(staged)
        except Exception as e:
            # A clip without captions still beats losing the moment.
            os.replace(staged, final)
            print(f"    captions failed ({e}) — kept the uncaptioned cut", flush=True)

    return final, None if os.path.exists(final) else "no output produced"


def stable_files(folder, seen, sizes):
    """Yield files that have finished being written since the last scan."""
    ready = []
    for name in sorted(os.listdir(folder)):
        if not name.lower().endswith(EXTS) or name.startswith("."):
            continue
        p = os.path.join(folder, name)
        if p in seen:
            continue
        try:
            size = os.path.getsize(p)
        except OSError:
            continue
        prev, count = sizes.get(p, (-1, 0))
        if size == prev and size > 0:
            count += 1
            if count >= STABLE_CHECKS:
                ready.append(p)
                sizes.pop(p, None)
                continue
        else:
            count = 0
        sizes[p] = (size, count)
    return ready


def watch(folder=None):
    folder = os.path.expanduser(folder or DEFAULT_WATCH)
    if not os.path.isdir(folder):
        sys.exit(f"not a folder: {folder}")
    # Everything already there is history, not a new save.
    seen = {os.path.join(folder, n) for n in os.listdir(folder)}
    sizes = {}
    print(f"watching {folder}")
    print(f"  layout '{LAYOUT_KEY}' -> {layout.cfg(LAYOUT_KEY)}")
    print(f"  captions {'on' if CAPTIONS else 'off'}   output {OUT}/")
    print(f"  {len(seen)} existing file(s) ignored. Hit your OBS replay hotkey. Ctrl-C to stop.\n")
    try:
        while True:
            for p in stable_files(folder, seen, sizes):
                seen.add(p)
                print(f"  [{time.strftime('%H:%M:%S')}] {os.path.basename(p)} -> processing", flush=True)
                t0 = time.time()
                final, err = process(p)
                if final:
                    mb = os.path.getsize(final) / 1e6
                    print(f"      ready/{os.path.basename(final)}  "
                          f"({mb:.1f} MB, {time.time()-t0:.0f}s)", flush=True)
                else:
                    print(f"      failed: {err}", flush=True)
            time.sleep(POLL)
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "watch"
    if cmd == "watch":
        watch(sys.argv[2] if len(sys.argv) > 2 else None)
    elif cmd == "once":
        final, err = process(os.path.expanduser(sys.argv[2]))
        print(final if final else f"failed: {err}")
    elif cmd == "tune":
        sheet = layout.preview(os.path.expanduser(sys.argv[2]))
        print(sheet if sheet else "preview failed")
    else:
        print(__doc__)
