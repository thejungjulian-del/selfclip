#!/usr/bin/env python3
"""
Burned-in captions for vertical clips.

Why images instead of ffmpeg's text filters: the installed ffmpeg (both 8.1 and 9.0.1 from
Homebrew) is built without libfreetype/libass, so `drawtext`, `subtitles` and `ass` are all
unavailable. Building ffmpeg from source to get them is a long detour, and rendering the
text ourselves gives better control over the heavy stroke that short-form captions need
anyway. So: whisper.cpp for timings, Pillow for typography, ffmpeg's `overlay` (which is
available) to composite.

    audio → whisper-cli → timed segments → one transparent PNG per segment
                                                 → overlay, time-gated

Usage:
    python3 captions.py add <in.mp4> <out.mp4> [--style pop|block] [--size 104]
    python3 captions.py text <in.mp4>            # just print the transcript
"""
import json
import os
import subprocess
import sys
import tempfile

from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
MODELS = os.path.join(HERE, "models")

W, H = 1080, 1920
FONT = "/System/Library/Fonts/Supplemental/Arial Bold.ttf"
LINE_GAP = 12
MAX_W = int(W * 0.86)          # keep clear of the screen edges

# Two caption styles, and the difference between them is timing, not typography.
#
#   block  one whole whisper segment held on screen for as long as it takes to say. Reads
#          as a subtitle: correct, and completely separate from the edit.
#   pop    1-3 words at a time, big, changing in step with the speech. This is the
#          short-form/gaming look. The text becomes part of the pacing instead of a
#          caption sitting underneath it.
#
# `max_len` is what does the work: whisper is asked for segments of at most that many
# characters, WITH --split-on-word. Passing -ml alone is what used to chop words in half
# ("absolute b" / "anger") — -sow is the half that was missing.
STYLES = {
    "block": {"size": 66,  "y": 0.70, "max_len": 0,  "max_lines": 3},
    "pop":   {"size": 104, "y": 0.62, "max_len": 16, "max_lines": 2},
}
DEFAULT_STYLE = "pop"

# Kept as module constants because the CLI and older callers reference them.
SIZE = STYLES["block"]["size"]
BASE_Y = int(H * STYLES["block"]["y"])
MAX_LINES = STYLES["block"]["max_lines"]


def style(name=None):
    return dict(STYLES.get(name or DEFAULT_STYLE, STYLES[DEFAULT_STYLE]))


def stroke_for(size):
    """Stroke has to grow with the type or big text loses its edge against bright gameplay."""
    return max(6, int(size / 9))


def model_path(name="base.en"):
    p = os.path.join(MODELS, f"ggml-{name}.bin")
    if not os.path.exists(p):
        sys.exit(f"model not found: {p}\n"
                 f"  download from https://huggingface.co/ggerganov/whisper.cpp/tree/main")
    return p


def transcribe(video, model="base.en", max_len=None):
    """Returns [(start_s, end_s, text)].

    max_len caps a segment's length in characters so the captions change in time with the
    speech instead of holding a whole sentence. It is paired with --split-on-word, without
    which whisper splits mid-word ('absolute b' / 'anger'). max_len=0 means whole segments.
    """
    if max_len is None:
        max_len = style()["max_len"]
    with tempfile.TemporaryDirectory() as td:
        wav = os.path.join(td, "a.wav")
        r = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", video,
             "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", wav],
            capture_output=True, text=True)
        if r.returncode != 0:
            sys.exit(f"audio extract failed: {r.stderr[:200]}")
        stem = os.path.join(td, "t")
        cmd = ["whisper-cli", "-m", model_path(model), "-f", wav, "-oj", "-of", stem]
        if max_len:
            cmd += ["-ml", str(int(max_len)), "-sow"]
        r = subprocess.run(cmd, capture_output=True, text=True)
        js = stem + ".json"
        if not os.path.exists(js):
            sys.exit(f"whisper produced no json: {r.stderr[-300:]}")
        d = json.load(open(js))
    out = []
    for s in d.get("transcription", []):
        o = s.get("offsets", {})
        txt = (s.get("text") or "").strip()
        if not txt:
            continue
        start, end = o.get("from", 0) / 1000.0, o.get("to", 0) / 1000.0
        # whisper repeats itself on unclear audio, emitting the same line as two
        # consecutive segments. Burned in, that reads as a stutter — extend the
        # previous caption instead of drawing it twice.
        if out and out[-1][2] == txt:
            out[-1] = (out[-1][0], end, txt)
            continue
        out.append((start, end, txt))
    return out


def wrap(draw, text, font, max_w, max_lines=MAX_LINES):
    words, lines, cur = text.split(), [], ""
    for w in words:
        trial = (cur + " " + w).strip()
        if draw.textlength(trial, font=font) <= max_w or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines[:max_lines]


def render_png(text, path, size=SIZE, y_frac=None, max_lines=MAX_LINES):
    """Transparent PNG, text centred horizontally, anchored so the block grows upward from
    its baseline — captions stay put instead of jumping as line count changes.

    A long word at a large size would run off the edges, so type shrinks to fit rather than
    being cropped or wrapped mid-word.
    """
    base_y = int(H * (y_frac if y_frac is not None else STYLES["block"]["y"]))
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    text = text.upper()
    while size > 40:
        font = ImageFont.truetype(FONT, size)
        if max(d.textlength(w, font=font) for w in text.split() or [""]) <= MAX_W:
            break
        size -= 6
    font = ImageFont.truetype(FONT, size)
    lines = wrap(d, text, font, MAX_W, max_lines)
    lh = size + LINE_GAP
    total = lh * len(lines)
    y = base_y - total // 2
    for ln in lines:
        x = (W - d.textlength(ln, font=font)) / 2
        d.text((x, y), ln, font=font, fill=(255, 255, 255, 255),
               stroke_width=stroke_for(size), stroke_fill=(0, 0, 0, 255))
        y += lh
    img.save(path)
    return path


def burn(video, out, segments, size=None, st=None):
    if not segments:
        return None, "nothing to caption (no speech detected)"
    st = st or style()
    size = size or st["size"]
    td = tempfile.mkdtemp(prefix="caps_")
    pngs = []
    for i, (a, b, txt) in enumerate(segments):
        p = os.path.join(td, f"c{i:03d}.png")
        render_png(txt, p, size, st["y"], st["max_lines"])
        pngs.append((a, b, p))

    args = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", video]
    for _, _, p in pngs:
        args += ["-i", p]
    chain, prev = [], "0:v"
    for i, (a, b, _) in enumerate(pngs, start=1):
        lbl = f"v{i}"
        chain.append(f"[{prev}][{i}:v]overlay=0:0:enable='between(t,{a:.2f},{b:.2f})'[{lbl}]")
        prev = lbl
    args += ["-filter_complex", ";".join(chain), "-map", f"[{prev}]", "-map", "0:a?",
             "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
             "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", out]
    r = subprocess.run(args, capture_output=True, text=True)
    for _, _, p in pngs:
        try:
            os.remove(p)
        except Exception:
            pass
    try:
        os.rmdir(td)
    except Exception:
        pass
    if r.returncode != 0 or not os.path.exists(out):
        return None, f"overlay failed: {r.stderr[:250]}"
    return out, None


def add(video, out, model="base.en", size=None, style_name=None):
    st = style(style_name)
    segs = transcribe(video, model, st["max_len"])
    print(f"  {len(segs)} caption segment(s), style '{style_name or DEFAULT_STYLE}'")
    for a, b, t in segs[:6]:
        print(f"    {a:6.2f}-{b:6.2f}  {t[:56]}")
    if len(segs) > 6:
        print(f"    ... +{len(segs)-6} more")
    p, err = burn(video, out, segs, size, st)
    print(f"  wrote {p}" if p else f"  failed: {err}")
    return p


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "help"
    if cmd == "add" and len(sys.argv) > 3:
        m = "base.en"
        s = None
        sty = None
        if "--model" in sys.argv:
            m = sys.argv[sys.argv.index("--model") + 1]
        if "--size" in sys.argv:
            s = int(sys.argv[sys.argv.index("--size") + 1])
        if "--style" in sys.argv:
            sty = sys.argv[sys.argv.index("--style") + 1]
        add(sys.argv[2], sys.argv[3], m, s, sty)
    elif cmd == "text" and len(sys.argv) > 2:
        for a, b, t in transcribe(sys.argv[2]):
            print(f"{a:7.2f}-{b:7.2f}  {t}")
    else:
        print(__doc__)
