#!/usr/bin/env python3
"""
Vertical layout engine for stream clips.

Stream layouts differ per streamer and per segment, so there is no single correct crop:
caseoh_ often runs a fullscreen webcam, stableronaldo runs gameplay with a small corner
cam, and a just-chatting segment is different again. So layout is configuration, not
detection, with a preview command to check it by eye before committing.

Modes
  full   the whole frame is the subject (fullscreen webcam, just-chatting).
         Tight 9:16 crop at a horizontal focus point. Fills the phone screen.
  stack  gameplay + a corner facecam. Two panels: the cam scaled up across the top,
         the action cropped below. This is the format that reads on a phone.
  fill   the original frame centred over a blurred copy of itself. Loses no content but
         wastes about a third of the screen — use only when the others don't fit.

Usage
    python3 layout.py preview <source> [--at 8]      # one frame per mode, side by side
    python3 layout.py render <source> <out.mp4> [--mode stack] [--channel caseoh_]
"""
import os
import subprocess
import sys

W, H = 1080, 1920
CAM_H = 640                      # top panel height in stack mode
MAIN_H = H - CAM_H               # 1280

# Per-channel layout. Rects are (x, y, w, h) in SOURCE pixels.
# focus is 0..1 horizontal position for the 9:16 crop in full mode (0.5 = centre).
LAYOUTS = {
    "caseoh_":       {"mode": "full",  "focus": 0.58},
    # measured from real Backyard Breaks footage: two-panel source — reactions left ~68%,
    # card-cam right ~32%. Reactions on top (the hook), card reveal below (the payoff).
    "_cardbreak":    {"mode": "stack", "cam": (0.0, 0.0, 0.68, 1.0),
                      "main": (0.68, 0.0, 1.0, 1.0), "split": 760},
    # MEASURED off VOD 2844584602 (1280x720): facecam sits left-of-centre at x 0-352,
    # y 290-490, the content he is reacting to fills the middle, and his chat overlay
    # occupies the top right — so `main` stops short of it rather than burning a third of
    # the frame on someone else's chat. Rects are fractions, so this survives 1080p footage.
    # The rects the absolute-pixel version used put `cam` on blank wall and `main` on the
    # overlay; check a still after any scene change with `vodclip.py tune`.
    "stableronaldo": {"mode": "stack",
                      "cam":  (0.0, 0.40, 0.275, 0.68),
                      "main": (0.375, 0.0, 0.848, 1.0)},
    "jasontheween":  {"mode": "band",  "x0": 0.11, "x1": 0.93, "top": 0.22},
    "silky":         {"mode": "full",  "focus": 0.50},
    "plaqueboymax":  {"mode": "full",  "focus": 0.50},
    # Julian's own stream (selfclip.py). Placeholder until tuned against a real
    # OBS save — run `python3 selfclip.py tune <file>` and set this from the sheet.
    "_selfstream":   {"mode": "full",  "focus": 0.50},
    "_default":      {"mode": "full",  "focus": 0.50},
}


def cfg(channel):
    return LAYOUTS.get(channel, LAYOUTS["_default"])


def probe(src):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "csv=p=0", src],
        capture_output=True, text=True).stdout.strip().split(",")
    try:
        return int(out[0]), int(out[1])
    except Exception:
        return 1280, 720


def vf_full(sw, sh, focus=0.5):
    """Tight 9:16 crop. Keeps full source height, slides horizontally to the focus point."""
    cw = int(sh * 9 / 16)
    if cw > sw:                                  # source already narrower than 9:16
        return f"scale={W}:-2,crop={W}:{H}"
    x = int((sw - cw) * max(0.0, min(1.0, focus)))
    return f"crop={cw}:{sh}:{x}:0,scale={W}:{H}"


def _rect(r, sw, sh):
    """Accept a rect as absolute pixels (x, y, w, h) or as fractions of the frame
    (x0, y0, x1, y1) with every value <= 1. Fractions are resolution-independent, which
    matters because the same campaign's footage arrives at different sizes."""
    a, b, c_, d = r
    if max(r) <= 1.0:
        x0, y0, x1, y1 = a, b, c_, d
        return (int(sw * x0), int(sh * y0),
                max(16, int(sw * (x1 - x0))), max(16, int(sh * (y1 - y0))))
    return (int(a), int(b), int(c_), int(d))


def vf_stack(cam, main, sw, sh, split=CAM_H):
    """Two panels stacked. `cam` goes on top, `main` below, with `split` px given to the
    top. Each panel is scaled to cover then cropped, so neither is stretched.

    Used for any source that already contains two regions worth keeping — a gameplay feed
    plus a corner facecam, or a card-break set with reactions on one side and a card-cam on
    the other. A single 9:16 crop would throw one of them away.
    """
    top_h = max(120, min(int(split), H - 120))
    bot_h = H - top_h
    cx, cy, cw, ch = _rect(cam, sw, sh)
    mx, my, mw, mh = _rect(main, sw, sh)
    return (
        f"[0:v]crop={cw}:{ch}:{cx}:{cy},"
        f"scale={W}:{top_h}:force_original_aspect_ratio=increase,crop={W}:{top_h}[cam];"
        f"[0:v]crop={mw}:{mh}:{mx}:{my},"
        f"scale={W}:{bot_h}:force_original_aspect_ratio=increase,crop={W}:{bot_h}[main];"
        f"[cam][main]vstack=inputs=2[v]"
    )


def vf_band(sw, sh, x0=0.12, x1=0.92, top=0.20):
    """Two-person wide shots (a couch/desk cam with two people, no gameplay).

    A 9:16 crop can only fit one of them, and plain `fill` centres a small strip with waste
    above and below. So: crop the dead space off the sides, scale the remaining band to full
    width, and sit it high in the frame — the subjects come out as large as the aspect
    allows, and the space left underneath is where captions land instead of over faces.
    """
    cw = max(16, int(sw * (x1 - x0)))
    cx = int(sw * x0)
    return (
        "split=2[bg][fg];"
        f"[bg]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},"
        "gblur=sigma=30[bgb];"
        f"[fg]crop={cw}:{sh}:{cx}:0,scale={W}:-2[fgs];"
        f"[bgb][fgs]overlay=(W-w)/2:{int(H*top)}"
    )


def vf_fill():
    return ("split=2[bg][fg];"
            f"[bg]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},"
            "gblur=sigma=28[bgb];"
            f"[fg]scale={W}:-2[fgs];"
            "[bgb][fgs]overlay=(W-w)/2:(H-h)/2")


def ffmpeg_args(src, out, mode, params, sw, sh, extra=None, still=False, dur=None):
    """Complete ffmpeg argv. stack needs filter_complex + an explicit -map; the others
    use -vf. `still` builds a single-frame image job (no audio, no encoder settings) —
    doing that by post-filtering the video argv breaks the '-map [v]' pair."""
    base = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    if extra:
        base += extra
    base += ["-i", src]
    if mode == "stack":
        cam = params.get("cam") or (0.0, 0.6, 0.28, 0.93)
        main = params.get("main") or (0.22, 0.0, 0.78, 1.0)
        base += ["-filter_complex",
                 vf_stack(cam, main, sw, sh, params.get("split", CAM_H)), "-map", "[v]"]
        if not still:
            base += ["-map", "0:a?"]
    else:
        if mode == "full":
            vf = vf_full(sw, sh, params.get("focus", 0.5))
        elif mode == "band":
            vf = vf_band(sw, sh, params.get("x0", 0.12), params.get("x1", 0.92),
                         params.get("top", 0.20))
        else:
            vf = vf_fill()
        base += ["-vf", vf]
    if still:
        base += ["-frames:v", "1", out]
        return base
    base += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23"]
    if out.endswith(".mp4"):
        base += ["-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart"]
    if dur:
        base += ["-t", f"{dur:.2f}"]
    base += [out]
    return base


def render(src, out, mode=None, channel=None, params=None, ss=None, dur=None):
    """ss/dur trim while re-encoding, so a caller that has a long source and wants one
    window out of it doesn't need a separate lossy-cut pass first (see vodclip.py)."""
    sw, sh = probe(src)
    p = dict(cfg(channel)) if channel else {}
    if params:
        p.update(params)
    mode = mode or p.get("mode", "full")
    args = ffmpeg_args(src, out, mode, p, sw, sh,
                       extra=["-ss", f"{ss:.2f}"] if ss else None, dur=dur)
    r = subprocess.run(args, capture_output=True, text=True)
    if r.returncode != 0:
        return None, r.stderr[:250]
    return out, None


def preview(src, at=8.0):
    """Render one still per mode and stitch them side by side for comparison."""
    sw, sh = probe(src)
    d = os.path.dirname(os.path.abspath(src)) or "."
    stills = []
    variants = [
        ("full-0.50", "full", {"focus": 0.50}),
        ("full-0.58", "full", {"focus": 0.58}),
        ("band", "band", {"x0": 0.11, "x1": 0.93, "top": 0.22}),
        ("cardbreak", "stack", LAYOUTS["_cardbreak"]),
        ("stack", "stack", {}),
        ("fill", "fill", {}),
    ]
    for name, mode, p in variants:
        out = os.path.join(d, f"prev_{name}.jpg")
        args = ffmpeg_args(src, out, mode, p, sw, sh, extra=["-ss", str(at)], still=True)
        r = subprocess.run(args, capture_output=True, text=True)
        if r.returncode == 0 and os.path.exists(out):
            stills.append((name, out))
            print(f"  {name:<12} ok")
        else:
            print(f"  {name:<12} FAILED  {r.stderr.strip()[:130]}")
    if len(stills) < 2:
        return None
    sheet = os.path.join(d, "preview_sheet.jpg")
    args = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    for _, p in stills:
        args += ["-i", p]
    n = len(stills)
    args += ["-filter_complex",
             "".join(f"[{i}:v]scale=-2:900[s{i}];" for i in range(n))
             + "".join(f"[s{i}]" for i in range(n)) + f"hstack=inputs={n}[v]",
             "-map", "[v]", "-frames:v", "1", sheet]
    r = subprocess.run(args, capture_output=True, text=True)
    if r.returncode != 0:
        print(f"  sheet failed: {r.stderr[:160]}")
        return None
    print(f"\n  comparison sheet: {sheet}")
    print(f"  order: {' | '.join(n for n, _ in stills)}")
    return sheet


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "help"
    if cmd == "preview" and len(sys.argv) > 2:
        at = 8.0
        if "--at" in sys.argv:
            at = float(sys.argv[sys.argv.index("--at") + 1])
        preview(sys.argv[2], at)
    elif cmd == "render" and len(sys.argv) > 3:
        mode = ch = None
        if "--mode" in sys.argv:
            mode = sys.argv[sys.argv.index("--mode") + 1]
        if "--channel" in sys.argv:
            ch = sys.argv[sys.argv.index("--channel") + 1]
        out, err = render(sys.argv[2], sys.argv[3], mode=mode, channel=ch)
        print(f"wrote {out}" if out else f"failed: {err}")
    else:
        print(__doc__)
