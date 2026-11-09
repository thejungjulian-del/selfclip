#!/usr/bin/env python3
"""
A Twitch VOD in, the 5 best moments out, post-ready.

live_clip.py handles a stream that is on right now; you have to be there. This handles the
archive, which is where the volume is — one 4-hour VOD holds more clippable moments than a
session you sat through, and the campaigns that allow VOD sourcing (Mizkif) don't care when
the footage was captured.

The awkward part is the ratio: the VOD is 4 hours and the moments are 30 seconds. Grabbing
the whole thing to go looking costs gigabytes and most of an hour, so the order is inverted.
Read the CHAT first — it's free, needs no video, and covers the whole timeline — score it,
and only then download the five ~66-second windows that won.

    VOD ──► chat replay (no download) ──► laugh / clip-call density, sampled across the VOD
                                                     │
                       top N non-overlapping moments ▼
        per moment:  yt-dlp --download-sections (66s) ──► audio peak picks the exact frame
                     ──► layout 9:16 ──► captions ──► ready/ + a row in production.db

Why the audio step: chat reacts *after* the thing happens and the lag varies, so chat is
only good for the neighbourhood. The loudest 2-second window inside that neighbourhood is
the moment itself. The clip is then cut PRE seconds ahead of it, so the payoff lands at
0:28 of 0:36 — the same shape as the clips already in clips/.

What is deliberately NOT a signal: raw message velocity. Measured on this data it ran 0.44x
lift against clip-calls — worse than random — so it is printed for eyeballing and carries
zero weight. Laughs and "clip that" calls carry all of it.

⚠️  Chat replay comes from Twitch's public web GraphQL endpoint, which is not a documented
    API. If Twitch rotates the query hash this returns nothing; `--audio` runs the whole
    thing off loudness alone instead (costs an audio-only download of the full VOD).

Usage:
    python3 vodclip.py vods <channel>              # recent VOD ids/urls for a channel
    python3 vodclip.py moments <vod> [--n 5]       # rank moments. Downloads no video.
    python3 vodclip.py tune <vod>                  # layout preview sheet from a real frame
    python3 vodclip.py make <vod> [--n 5] [--channel x] [--campaign k] [--audio]
"""
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import audio_signal
import captions
import layout
from live_clip import CLIPCALL, LAUGH, POST, PRE

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "ready")
RAW = os.path.join(HERE, "vodraw")
DB = os.path.join(HERE, "production.db")

GQL = "https://gql.twitch.tv/gql"
# Twitch's own public web client id, the one the site itself ships in every page.
GQL_CLIENT_ID = "kimne78kx3ncx6brgo4mv6wki5h1ko"
# Sent as a plain query, not a persistedQuery hash. Every hash published for
# VideoCommentsByOffsetOrCursor now answers PersistedQueryNotFound (verified 2026-08-17);
# the endpoint still accepts an ad-hoc query document, which cannot go stale the same way.
COMMENTS_QUERY = """
query($videoID: ID!, $offset: Int) {
  video(id: $videoID) {
    comments(contentOffsetSeconds: $offset) {
      edges { node { contentOffsetSeconds message { fragments { text } } } }
    }
  }
}
"""

# One request buys ~50 messages, which on a busy channel is only 5-8 SECONDS of chat
# (measured on stableronaldo: 42-60 messages spanning 5-8s, 0.59s per request). So the probe
# step is what decides how much of the VOD actually gets looked at — at 45s it was 13%.
SAMPLE_STEP = 15.0
WORKERS = 6             # probes run concurrently; ~10 req/s at 0.59s each
MIN_SPAN = 5.0          # floor on a sample's time span, so a 1s burst can't divide by ~0
W_CALL = 3.0            # a literal "clip that" is worth several laughs
MIN_GAP = 120.0         # keep picks apart, or one long bit becomes all five clips
PAD = 15.0              # extra seconds each side of the download, absorbing cut slop
CLIP_SECS = float(PRE + POST)
CAPTIONS = True


# ---------------------------------------------------------------- vod identity
def vod_id(s):
    m = re.search(r"(\d{6,})", str(s))
    if not m:
        sys.exit(f"no VOD id in {s!r} — pass a twitch.tv/videos/<id> url or the bare id")
    return m.group(1)


def vod_url(vid):
    return f"https://www.twitch.tv/videos/{vid}"


def meta(vid):
    """(duration_s, channel_login, title) straight off yt-dlp. One call, no download."""
    r = subprocess.run(
        ["yt-dlp", "--no-warnings", "--skip-download", "--print",
         "%(duration)s|%(uploader_id)s|%(title)s", vod_url(vid)],
        capture_output=True, text=True)
    line = (r.stdout or "").strip().splitlines()
    if r.returncode != 0 or not line:
        sys.exit(f"could not read VOD {vid}: {(r.stderr or '').strip()[:200]}")
    parts = line[-1].split("|", 2)
    try:
        dur = float(parts[0])
    except (ValueError, IndexError):
        dur = 0.0
    ch = (parts[1] if len(parts) > 1 else "").lstrip("@").lower()
    title = parts[2] if len(parts) > 2 else ""
    return dur, ch, title


def vods(channel, limit=10):
    """Recent VODs for a channel. Twitch expires these (14-60 days), so ids go stale."""
    r = subprocess.run(
        ["yt-dlp", "--no-warnings", "--flat-playlist", "--playlist-end", str(limit),
         "--print", "%(id)s|%(duration)s|%(title)s",
         f"https://www.twitch.tv/{channel}/videos"],
        capture_output=True, text=True)
    rows = [l for l in (r.stdout or "").strip().splitlines() if "|" in l]
    if not rows:
        print(f"no VODs listed for {channel}: {(r.stderr or '').strip()[:200]}")
        return
    print(f"{channel} — {len(rows)} recent VOD(s)\n")
    for l in rows:
        vid, d, title = (l.split("|", 2) + ["", ""])[:3]
        try:
            hrs = f"{float(d)/3600:.1f}h"
        except ValueError:
            hrs = "?"
        print(f"  {vid:<12} {hrs:>6}  {title[:58]}")
    print(f"\n  next: python3 vodclip.py moments {rows[0].split('|')[0]}")


# ---------------------------------------------------------------- chat replay
def gql_comments(vid, offset, tries=3):
    body = json.dumps({"query": COMMENTS_QUERY,
                       "variables": {"videoID": str(vid), "offset": int(offset)}}).encode()
    req = urllib.request.Request(GQL, data=body, headers={
        "Client-ID": GQL_CLIENT_ID, "Content-Type": "application/json"})
    for attempt in range(tries):
        try:
            d = json.loads(urllib.request.urlopen(req, timeout=20).read())
            return ((d.get("data") or {}).get("video") or {}).get("comments") or {}
        except Exception:
            if attempt == tries - 1:
                return {}
            time.sleep(1.5 * (attempt + 1))
    return {}


def sample_at(vid, offset):
    """One page of chat replay starting near `offset`.

    Returns (span_seconds, laughs, calls, msgs) or None if the page is unusable. A page is
    ~100 messages, so its span is itself informative: busy chat covers 5 seconds, dead chat
    covers 300.
    """
    c = gql_comments(vid, offset)
    edges = c.get("edges") or []
    if len(edges) < 8:
        return None
    times, laughs, calls = [], 0, 0
    for e in edges:
        n = e.get("node") or {}
        t = n.get("contentOffsetSeconds")
        if t is None:
            continue
        txt = "".join((f or {}).get("text") or ""
                      for f in ((n.get("message") or {}).get("fragments") or []))
        times.append(float(t))
        if LAUGH.search(txt):
            laughs += 1
        if CLIPCALL.search(txt):
            calls += 1
    if len(times) < 8:
        return None
    return max(MIN_SPAN, max(times) - min(times)), laughs, calls, len(times)


def scan_chat(vid, duration, step=SAMPLE_STEP):
    """Probe chat across the whole VOD. Returns [{t, score, laughs, calls, rate}] in time
    order.

    Sampling rather than paging start-to-finish is the point: a busy 4-hour VOD is tens of
    thousands of messages, and paging it in order would spend a thousand requests to cover
    the first hour. Sampling spends the same requests spread over the whole timeline.

    This does NOT see every second — it reports its coverage so the gap is visible rather
    than implied.
    """
    offsets = list(frange(0.0, max(duration - 30.0, step), step))
    print(f"  probing chat at {len(offsets)} points across {duration/3600:.1f}h "
          f"(every {step:.0f}s, {WORKERS} at a time, no video downloaded)")
    rows, empty, done = [], 0, 0
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for t, s in zip(offsets, ex.map(lambda o: sample_at(vid, o), offsets)):
            done += 1
            if done % 150 == 0:
                print(f"    {done}/{len(offsets)} probed, {len(rows)} usable", flush=True)
            if not s:
                empty += 1
                continue
            span, laughs, calls, msgs = s
            rows.append({"t": t, "score": (laughs + W_CALL * calls) / span,
                         "laughs": laughs, "calls": calls,
                         "rate": msgs / span, "span": span})
    seen = sum(r["span"] for r in rows)
    print(f"  {len(rows)} usable probe(s), {empty} empty · "
          f"saw {seen/60:.0f} min of chat = {100*seen/max(duration,1):.0f}% of the VOD")
    return rows


def frange(a, b, step):
    x = a
    while x < b:
        yield x
        x += step


# ---------------------------------------------------------------- audio fallback
def scan_audio(vid, duration):
    """Loudness-only ranking, for when chat replay is gone. Costs an audio-only download of
    the entire VOD — minutes and a few hundred MB, versus zero for the chat path."""
    os.makedirs(RAW, exist_ok=True)
    a = os.path.join(RAW, f"{vid}_audio.m4a")
    if not os.path.exists(a):
        print("  downloading audio-only track (no video) — this is the slow path")
        r = subprocess.run(["yt-dlp", "--no-warnings", "-f", "worstaudio/bestaudio",
                            "-o", a, vod_url(vid)], capture_output=True, text=True)
        if r.returncode != 0 or not os.path.exists(a):
            sys.exit(f"audio download failed: {(r.stderr or '').strip()[-200:]}")
    vals = audio_signal.levels(a)
    if not vals:
        sys.exit("no readable audio in the VOD")
    spikes = audio_signal.find_spikes(vals)
    w = audio_signal.WINDOW
    print(f"  {len(vals)} windows of {w}s, {len(spikes)} loudness spike(s)")
    return [{"t": i * w, "score": d, "laughs": 0, "calls": 0, "rate": 0.0, "span": w}
            for i, _v, d in spikes]


# ---------------------------------------------------------------- ranking
def pick(rows, n, min_gap=MIN_GAP):
    """Top n by score, greedily, never two within min_gap of each other."""
    chosen = []
    for r in sorted(rows, key=lambda r: -r["score"]):
        if all(abs(r["t"] - c["t"]) >= min_gap for c in chosen):
            chosen.append(r)
        if len(chosen) >= n:
            break
    return sorted(chosen, key=lambda r: r["t"])


def hms(t):
    t = int(t)
    return f"{t//3600}h{(t%3600)//60:02d}m{t%60:02d}s"


def show(rows, vid, n):
    top = pick(rows, n)
    print(f"\n  top {len(top)} moment(s) of {len(rows)} probe(s)\n")
    print(f"  {'#':>2}  {'at':>10}  {'score':>7}  {'laughs':>6} {'clip!':>5} "
          f"{'msg/s':>6}   link")
    for i, r in enumerate(top, 1):
        print(f"  {i:>2}  {hms(r['t']):>10}  {r['score']:>7.3f}  {r['laughs']:>6} "
              f"{r['calls']:>5} {r['rate']:>6.1f}   {vod_url(vid)}?t={hms(r['t'])}")
    print("\n  Open two or three of those links before running `make` — the score says chat")
    print("  reacted, not that the moment reads on its own.")
    return top


# ---------------------------------------------------------------- cutting
def download_window(vid, t):
    """The ~66s around t. Padded, because a section download lands on segment boundaries."""
    os.makedirs(RAW, exist_ok=True)
    a = max(0.0, t - PRE - PAD)
    b = t + POST + PAD
    out = os.path.join(RAW, f"{vid}_{int(t)}.mp4")
    if os.path.exists(out) and os.path.getsize(out) > 100_000:
        return out, None
    r = subprocess.run(
        ["yt-dlp", "--no-warnings", "-f", "best[height<=720]/best",
         "--download-sections", f"*{a:.0f}-{b:.0f}", "-o", out, vod_url(vid)],
        capture_output=True, text=True)
    if r.returncode != 0 or not os.path.exists(out):
        return None, (r.stderr or "").strip()[-200:] or "section download produced nothing"
    return out, None


def refine(raw, nominal):
    """Where in `raw` the clip should start.

    `nominal` is where the moment sits if the section download was exact. Chat's lag and the
    download's segment snapping both shift it, so look for the loudest window within +/-10s
    of nominal and hang the clip off that instead. Falls back to nominal if audio is unreadable.
    """
    vals = audio_signal.levels(raw)
    w = audio_signal.WINDOW
    total = len(vals) * w
    if not vals:
        return max(0.0, nominal - PRE), "no audio to refine against"
    lo, hi = max(0, int((nominal - 10) / w)), min(len(vals), int((nominal + 10) / w) + 1)
    if hi <= lo:
        return max(0.0, nominal - PRE), "refine window outside the file"
    peak = max(range(lo, hi), key=lambda i: vals[i])
    start = min(max(0.0, peak * w - PRE), max(0.0, total - CLIP_SECS))
    return start, f"peak {vals[peak]:.1f} dB at {peak*w:.0f}s -> start {start:.1f}s"


def finish(raw, start, out, channel):
    staged = out.replace(".mp4", "_raw.mp4") if CAPTIONS else out
    p, err = layout.render(raw, staged, channel=channel, ss=start, dur=CLIP_SECS)
    if err:
        return None, 0, f"layout failed: {err}"
    n_caps = 0
    if CAPTIONS:
        try:
            segs = captions.transcribe(staged)
            n_caps = len(segs)
            done, cerr = captions.burn(staged, out, segs)
            if done:
                os.remove(staged)
            else:
                os.replace(staged, out)          # an uncaptioned clip beats no clip
                n_caps = 0
        except Exception:
            os.replace(staged, out)
            n_caps = 0
    return out, n_caps, None


def register(campaign, src, out, n_caps, duration):
    import sqlite3
    c = sqlite3.connect(DB)
    c.execute("""CREATE TABLE IF NOT EXISTS clips (
        id INTEGER PRIMARY KEY AUTOINCREMENT, campaign TEXT NOT NULL, src TEXT NOT NULL,
        out TEXT NOT NULL, duration REAL, caption_count INTEGER, made_at TEXT,
        posted_at TEXT, post_url TEXT, views INTEGER DEFAULT 0, views_at TEXT,
        approved INTEGER, UNIQUE(campaign, src))""")
    try:
        c.execute("INSERT INTO clips (campaign,src,out,duration,caption_count,made_at) "
                  "VALUES (?,?,?,?,?,?)",
                  (campaign, src, out, duration, n_caps,
                   datetime.now(timezone.utc).isoformat()))
        c.commit()
    except Exception as e:
        return f"queue insert skipped: {e}"
    finally:
        c.close()
    return None


# ---------------------------------------------------------------- commands
def rank_rows(vid, duration, use_audio):
    if use_audio:
        return scan_audio(vid, duration)
    rows = scan_chat(vid, duration)
    if not rows:
        print("\n  chat replay returned nothing for this VOD. Either it is too old, or the\n"
              "  GraphQL query hash has rotated. Falling back to loudness.\n")
        return scan_audio(vid, duration)
    return rows


def cmd_moments(vid, n, use_audio):
    duration, ch, title = meta(vid)
    print(f"VOD {vid} — {ch} — {duration/3600:.1f}h — {title[:60]}")
    rows = rank_rows(vid, duration, use_audio)
    if not rows:
        sys.exit("no moments found")
    show(rows, vid, n)
    print(f"\n  next: python3 vodclip.py make {vid} --n {n}")


def cmd_tune(vid, use_audio):
    """Layout is per-streamer configuration and a wrong crop wastes half the frame, so pick
    it from a real frame of THIS VOD before making five clips with it."""
    duration, ch, title = meta(vid)
    print(f"VOD {vid} — {ch} — {duration/3600:.1f}h")
    rows = rank_rows(vid, duration, use_audio)
    top = pick(rows, 1)
    if not top:
        sys.exit("no moment to sample a frame from")
    t = top[0]["t"]
    print(f"  sampling a frame near {hms(t)}")
    raw, err = download_window(vid, t)
    if err:
        sys.exit(f"download failed: {err}")
    sheet = layout.preview(raw, at=min(20.0, PAD + PRE))
    if not sheet:
        sys.exit("preview failed")
    print(f"\n  Open that sheet, pick the mode that frames {ch} best, then set")
    print(f"  layout.LAYOUTS['{ch}'] to match. Current: {layout.cfg(ch)}")


def cmd_make(vid, n, channel, campaign, use_audio):
    duration, ch, title = meta(vid)
    channel = channel or ch
    print(f"VOD {vid} — {ch} — {duration/3600:.1f}h — {title[:60]}")
    print(f"  layout '{channel}' -> {layout.cfg(channel)}")
    if channel not in layout.LAYOUTS:
        print(f"  ⚠️  no preset for '{channel}', using _default (centre crop). "
              f"Run `tune` first or half the frame is wasted.")
    rows = rank_rows(vid, duration, use_audio)
    if not rows:
        sys.exit("no moments found")
    top = show(rows, vid, n)

    os.makedirs(OUT, exist_ok=True)
    made, failed = 0, 0
    for i, r in enumerate(top, 1):
        t = r["t"]
        out = os.path.join(OUT, f"{ch}_{vid}_{int(t)}_9x16.mp4")
        print(f"\n  [{i}/{len(top)}] {hms(t)} (score {r['score']:.3f})")
        if os.path.exists(out):
            print("      already made, skipping")
            continue
        raw, err = download_window(vid, t)
        if err:
            print(f"      download failed: {err}")
            failed += 1
            continue
        # download_window starts at max(0, t-PRE-PAD), so t lands this far into the file.
        nominal = min(t, PRE + PAD)
        start, note = refine(raw, nominal)
        print(f"      {note}")
        final, n_caps, err = finish(raw, start, out, channel)
        if err:
            print(f"      {err}")
            failed += 1
            continue
        qerr = register(campaign or ch, f"{vid}@{int(t)}", final, n_caps, CLIP_SECS)
        print(f"      ready/{os.path.basename(final)}  "
              f"({os.path.getsize(final)/1e6:.1f} MB, {n_caps} captions)"
              + (f"  [{qerr}]" if qerr else ""))
        made += 1

    print(f"\n  {made} ready · {failed} failed   ->  {OUT}/")
    print("  queue:  python3 batch.py queue")
    print("  after posting each:  python3 batch.py posted <id> <url>")
    print(f"  raw windows kept in {RAW}/ — delete when you're happy with the cuts.")


def arg(flag, default=None, cast=str):
    if flag in sys.argv:
        try:
            return cast(sys.argv[sys.argv.index(flag) + 1])
        except (IndexError, ValueError):
            sys.exit(f"{flag} needs a value")
    return default


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "help"
    audio = "--audio" in sys.argv
    if cmd == "vods" and len(sys.argv) > 2:
        vods(sys.argv[2], arg("--limit", 10, int))
    elif cmd == "moments" and len(sys.argv) > 2:
        cmd_moments(vod_id(sys.argv[2]), arg("--n", 5, int), audio)
    elif cmd == "tune" and len(sys.argv) > 2:
        cmd_tune(vod_id(sys.argv[2]), audio)
    elif cmd == "make" and len(sys.argv) > 2:
        cmd_make(vod_id(sys.argv[2]), arg("--n", 5, int),
                 arg("--channel"), arg("--campaign"), audio)
    else:
        print(__doc__)
