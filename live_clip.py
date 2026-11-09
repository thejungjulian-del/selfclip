#!/usr/bin/env python3
"""
Live clip pipeline: detect a stream going live, keep a rolling recording, and cut a
vertical clip the moment chat reacts.

The ordering problem this solves: chat reacts *after* the funny thing happens, so by the
time you know a moment occurred it is already in the past. A live recorder that only starts
when chat spikes has missed the moment. So we keep a bounded rolling buffer on disk and cut
backwards out of it.

    go live  →  yt-dlp | ffmpeg  →  rolling .ts segments (bounded, wraps)
                                          ▲
    chat spike at T  ──────────────────────┘   cut [T-PRE, T+POST], make 9:16

Live status and chat both come from public endpoints — no OAuth, no API key, no Discord.

Usage:
    python3 live_clip.py check                     # who's live right now
    python3 live_clip.py run <channel> [minutes]   # record + auto-clip on chat spikes
    python3 live_clip.py await <channel> [min] [poll]  # wait for live, then record
    python3 live_clip.py clips                     # clips produced so far
"""
import json
import os
import re
import shutil
import signal
import socket
import ssl
import statistics as st
import subprocess
import sys
import threading
import time
import urllib.request
from collections import deque

import captions
import layout

HERE = os.path.dirname(os.path.abspath(__file__))
BUF = os.path.join(HERE, "buffer")
OUT = os.path.join(HERE, "clips")
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0 Safari/537.36")

TRACK = ["caseoh_", "stableronaldo", "jasontheween", "jynxzi", "lacy", "adapt", "silky"]

SEG_SECS = 6            # segment length
SEG_WRAP = 100          # 100 x 6s = ~10 min rolling window, bounded disk
PRE, POST = 28, 8       # clip window around the reaction: mostly before it
CAPTIONS = True        # burn captions (whisper.cpp + Pillow overlay); see captions.py
COOLDOWN = 75           # min seconds between clips, so one long reaction isn't 8 clips

# chat spike detection
BUCKET = 2.0
BASE_WINDOW = 30
SPIKE_RATIO = 2.2
MIN_RATE = 1.5
MIN_LAUGHS = 2          # a volume spike must carry real reaction, not just chatter
MIN_LAUGH_RATE = 1.5    # laughs/sec that fires on its own (steady-but-busy chat problem)
LAUGH = re.compile(r"(kekw|lulw|omegalul|lmao|lmfao|kek\b|xdd|😭|💀|🤣|pepelaugh|icant)", re.I)
CLIPCALL = re.compile(r"(clip (it|that|this)|clipped|someone clip|clip pls)", re.I)


def is_live(channel):
    try:
        req = urllib.request.Request(f"https://www.twitch.tv/{channel}", headers={"User-Agent": UA})
        html = urllib.request.urlopen(req, timeout=20).read().decode(errors="replace")
        return '"isLiveBroadcast":true' in html
    except Exception:
        return None


def check():
    print(f"{'CHANNEL':<18}STATUS")
    print("-" * 30)
    for ch in TRACK:
        s = is_live(ch)
        print(f"{ch:<18}{'LIVE' if s else ('offline' if s is False else 'unknown')}")


# ---------------------------------------------------------------- recorder
class Recorder:
    """Rolling segment recorder. Keeps a wall-clock index of each segment so we can map
    a chat timestamp back to the files that contain it."""

    def __init__(self, channel):
        self.channel = channel
        self.dir = os.path.join(BUF, channel)
        shutil.rmtree(self.dir, ignore_errors=True)
        os.makedirs(self.dir, exist_ok=True)
        self.proc = None
        self.index = {}          # filename -> (mtime seen, size)
        self.stop_flag = False
        self.restarts = 0
        self.ended = False

    def _spawn(self):
        pattern = os.path.join(self.dir, "buf%03d.ts")
        cmd = (
            f'yt-dlp -q --no-warnings -f "best[height<=720]/best" '
            f'-o - "https://www.twitch.tv/{self.channel}" | '
            f'ffmpeg -hide_banner -loglevel error -i - -c copy -f segment '
            f'-segment_time {SEG_SECS} -segment_wrap {SEG_WRAP} -reset_timestamps 1 "{pattern}"'
        )
        return subprocess.Popen(cmd, shell=True, preexec_fn=os.setsid)

    def start(self):
        self.proc = self._spawn()
        threading.Thread(target=self._index_loop, daemon=True).start()
        threading.Thread(target=self._supervise, daemon=True).start()

    def _supervise(self):
        """yt-dlp drops on Twitch ad breaks and transient network hiccups — observed live,
        it died 3 minutes in while the stream was still up. One drop used to end the whole
        session, so restart it instead. Segment files are reused by -segment_wrap and the
        index is keyed on mtime, so a restart just continues the rolling window."""
        backoff = 4
        while not self.stop_flag:
            time.sleep(backoff)
            if self.stop_flag:
                return
            if self.proc and self.proc.poll() is not None:
                # A drop and a finished stream look identical from the pipe's exit code, so
                # ask twitch which it was. Without this check the supervisor retried a dead
                # stream 45 times in three minutes — verified live.
                if is_live(self.channel) is False:
                    print("  [stream ended — recorder stopping]", flush=True)
                    self.ended = True
                    return
                self.restarts += 1
                print(f"  [recorder dropped, still live — restart #{self.restarts}]", flush=True)
                try:
                    self.proc = self._spawn()
                    backoff = 4
                except Exception as e:
                    print(f"  [restart failed: {e}]", flush=True)
                    return
                if self.restarts % 3 == 0:
                    backoff = min(backoff * 2, 30)   # stop hammering if it keeps failing

    def _index_loop(self):
        """Watch the buffer dir and stamp each completed segment with wall-clock time."""
        while not self.stop_flag:
            try:
                for f in os.listdir(self.dir):
                    if not f.endswith(".ts"):
                        continue
                    p = os.path.join(self.dir, f)
                    sz = os.path.getsize(p)
                    mt = os.path.getmtime(p)
                    prev = self.index.get(f)
                    # a segment is "done" when its size stops growing
                    if prev is None or prev[1] != sz:
                        self.index[f] = (mt, sz)
            except FileNotFoundError:
                pass
            time.sleep(1.0)

    def segments_covering(self, t_start, t_end):
        """Segments whose written window overlaps [t_start, t_end], in order."""
        segs = []
        for f, (mt, sz) in self.index.items():
            if sz < 1000:
                continue
            seg_end = mt
            seg_start = mt - SEG_SECS
            if seg_end >= t_start and seg_start <= t_end:
                segs.append((mt, f))
        segs.sort()
        return [f for _, f in segs]

    def stop(self):
        self.stop_flag = True
        if self.proc:
            try:
                os.killpg(os.getpgid(self.proc.pid), signal.SIGTERM)
            except Exception:
                pass


# ---------------------------------------------------------------- clip cutting
def make_clip(rec, moment_ts, label):
    os.makedirs(OUT, exist_ok=True)
    segs = rec.segments_covering(moment_ts - PRE, moment_ts + POST)
    if len(segs) < 2:
        return None, "buffer did not cover the moment yet"
    listfile = os.path.join(rec.dir, f"cat_{label}.txt")
    with open(listfile, "w") as f:
        for s in segs:
            f.write(f"file '{os.path.join(rec.dir, s)}'\n")

    joined = os.path.join(rec.dir, f"join_{label}.ts")
    r = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0",
         "-i", listfile, "-c", "copy", joined],
        capture_output=True, text=True)
    if r.returncode != 0 or not os.path.exists(joined):
        return None, f"concat failed: {r.stderr[:160]}"

    # where in the joined file does the moment sit? oldest segment's start is our zero
    oldest_mt = min(rec.index[s][0] for s in segs)
    joined_start = oldest_mt - SEG_SECS
    offset = max(0.0, moment_ts - PRE - joined_start)
    dur = PRE + POST

    out = os.path.join(OUT, f"{rec.channel}_{label}.mp4")
    # Layout is per-channel config (see layout.py): a fullscreen-webcam channel wants a
    # tight crop, a gameplay+corner-cam channel wants the two-panel stack, and the old
    # blurred fill is a last resort — it leaves the subject as a small box with a third of
    # the screen wasted. Compared side by side, the crop is not a close call.
    sw, sh = layout.probe(joined)
    p = dict(layout.cfg(rec.channel))
    args = layout.ffmpeg_args(joined, out, p.get("mode", "full"), p, sw, sh,
                              extra=["-ss", f"{offset:.2f}"])
    # insert the duration limit right before the output path
    args = args[:-1] + ["-t", f"{dur}"] + args[-1:]
    r = subprocess.run(args, capture_output=True, text=True)
    try:
        os.remove(joined)
        os.remove(listfile)
    except Exception:
        pass
    if r.returncode != 0 or not os.path.exists(out):
        return None, f"encode failed: {r.stderr[:160]}"

    if CAPTIONS:
        # Captions are the biggest retention lever in short-form, so they're on by default.
        # If transcription or burn-in fails, keep the uncaptioned clip rather than losing
        # the moment — a plain clip is still postable.
        capped = out.replace(".mp4", "_cap.mp4")
        try:
            segs = captions.transcribe(out)
            done, cerr = captions.burn(out, capped, segs)
            if done:
                os.remove(out)
                return done, None
            print(f"      (captions skipped: {cerr})", flush=True)
        except Exception as e:
            print(f"      (captions skipped: {type(e).__name__}: {e})", flush=True)
    return out, None


# ---------------------------------------------------------------- chat
def chat_loop(channel, on_spike, stop_evt):
    ctx = ssl.create_default_context()
    while not stop_evt.is_set():
        try:
            s = ctx.wrap_socket(socket.create_connection(("irc.chat.twitch.tv", 6697), timeout=15),
                                server_hostname="irc.chat.twitch.tv")
            s.settimeout(1.0)
            s.sendall(b"PASS SCHMOOPIIE\r\nNICK justinfan"
                      + str(int(time.time()) % 99999).encode() + b"\r\n")
            time.sleep(0.8)
            s.sendall(f"JOIN #{channel.lower()}\r\n".encode())
        except Exception:
            time.sleep(5)
            continue

        buf, bucket_start, count, laughs, calls = "", time.time(), 0, 0, 0
        hist = deque(maxlen=BASE_WINDOW)
        try:
            while not stop_evt.is_set():
                try:
                    chunk = s.recv(8192).decode("utf-8", errors="replace")
                except socket.timeout:
                    chunk = ""
                except Exception:
                    break
                buf += chunk
                while "\r\n" in buf:
                    line, buf = buf.split("\r\n", 1)
                    if line.startswith("PING") or " PING " in line:
                        s.sendall(b"PONG :tmi.twitch.tv\r\n")
                        continue
                    m = re.search(r"PRIVMSG #[^ ]+ :(.*)$", line)
                    if m:
                        txt = m.group(1)
                        count += 1
                        if LAUGH.search(txt):
                            laughs += 1
                        if CLIPCALL.search(txt):
                            calls += 1
                now = time.time()
                if now - bucket_start >= BUCKET:
                    rate = count / BUCKET
                    lrate = laughs / BUCKET
                    base = st.median(hist) if len(hist) >= 5 else None
                    # 45-min validation (71k msgs, 5 channels): raw VELOCITY scored 0.44x
                    # lift against chat's own "clip it" calls — worse than random airtime,
                    # and 0.00x on two of three channels. Busy chat is often just steady.
                    # LAUGH density did better (1.81x on the only channel with a usable
                    # label count, n=10). So gate on laughter, not volume: a spike must
                    # carry actual reaction, and a laughter burst alone can fire too.
                    if base and base > 0:
                        vol_spike = rate >= MIN_RATE and rate / base >= SPIKE_RATIO
                        laugh_spike = lrate >= MIN_LAUGH_RATE
                        if (vol_spike and laughs >= MIN_LAUGHS) or laugh_spike or calls:
                            on_spike(now, rate, rate / base, laughs, calls)
                    hist.append(rate)
                    bucket_start, count, laughs, calls = now, 0, 0, 0
        finally:
            try:
                s.close()
            except Exception:
                pass


# ---------------------------------------------------------------- run
def run(channel, minutes=30):
    if is_live(channel) is not True:
        print(f"{channel} is not live right now — nothing to record.")
        return
    os.makedirs(BUF, exist_ok=True)
    rec = Recorder(channel)
    rec.start()
    print(f"recording {channel} into a {SEG_WRAP*SEG_SECS//60}-min rolling buffer")
    print(f"clip window: {PRE}s before the reaction, {POST}s after   cooldown {COOLDOWN}s")
    print("waiting for the buffer to fill before clipping ...\n")

    stop_evt = threading.Event()
    state = {"last": 0.0, "n": 0, "started": time.time()}

    def on_spike(ts, rate, ratio, laughs, calls):
        if ts - state["started"] < PRE + SEG_SECS * 2:
            return                          # buffer not deep enough yet
        if ts - state["last"] < COOLDOWN:
            return
        state["last"] = ts
        state["n"] += 1
        label = f"{int(ts)}_{state['n']:02d}"
        tag = "CLIPCALL" if calls else ("laugh" if laughs >= 2 else "spike")
        print(f"  [{time.strftime('%H:%M:%S')}] {tag}  {rate:.1f} msg/s ({ratio:.1f}x)  "
              f"laughs={laughs} calls={calls}  -> cutting ...", flush=True)
        time.sleep(POST + 2)                # let the tail of the moment land in the buffer
        path, err = make_clip(rec, ts, label)
        if path:
            mb = os.path.getsize(path) / 1e6
            print(f"      saved {os.path.basename(path)}  ({mb:.1f} MB)", flush=True)
        else:
            print(f"      skipped: {err}", flush=True)

    t = threading.Thread(target=chat_loop, args=(channel, on_spike, stop_evt), daemon=True)
    t.start()
    try:
        deadline = time.time() + minutes * 60
        while time.time() < deadline:
            time.sleep(5)
            # the supervisor restarts transient drops and sets .ended when the stream is
            # genuinely over, so there is nothing to re-probe here
            if rec.ended:
                print("  channel went offline — stopping")
                break
    except KeyboardInterrupt:
        print("\nstopping")
    finally:
        stop_evt.set()
        rec.stop()
        time.sleep(1)
    print(f"\ndone. {state['n']} clip(s) attempted. Output in {OUT}/")


def await_live(channel, minutes=30, poll=60, max_wait_min=240):
    """Poll until the channel goes live, then record. This is the piece that makes the
    pipeline unattended: you point it at a streamer and walk away."""
    print(f"waiting for {channel} to go live (checking every {poll}s, giving up after "
          f"{max_wait_min} min)")
    waited, streak = 0, 0
    while waited < max_wait_min * 60:
        s = is_live(channel)
        # twitch.tv occasionally serves a stale/cached page that reads as live — observed
        # firing once then failing immediately inside run(). Require two consecutive
        # confirmations before committing to a recording session.
        streak = streak + 1 if s is True else 0
        if streak >= 2:
            print(f"  [{time.strftime('%H:%M:%S')}] {channel} is LIVE (confirmed x2) — starting\n")
            return run(channel, minutes)
        if s is True:
            print(f"  [{time.strftime('%H:%M:%S')}] reads live — confirming ...", flush=True)
            time.sleep(min(poll, 10)); waited += min(poll, 10)
            continue
        print(f"  [{time.strftime('%H:%M:%S')}] "
              f"{'offline' if s is False else 'check failed'}", flush=True)
        time.sleep(poll)
        waited += poll
    print("gave up waiting")


def clips():
    if not os.path.isdir(OUT):
        print("no clips yet")
        return
    files = sorted(f for f in os.listdir(OUT) if f.endswith(".mp4"))
    if not files:
        print("no clips yet")
        return
    print(f"{len(files)} clip(s) in {OUT}/\n")
    for f in files:
        p = os.path.join(OUT, f)
        d = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                            "-of", "default=noprint_wrappers=1:nokey=1", p],
                           capture_output=True, text=True).stdout.strip()
        print(f"  {f:<44}{os.path.getsize(p)/1e6:>7.1f} MB   {d}s")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "help"
    if cmd == "check":
        check()
    elif cmd == "run":
        run(sys.argv[2], float(sys.argv[3]) if len(sys.argv) > 3 else 30)
    elif cmd == "await":
        await_live(sys.argv[2],
                   float(sys.argv[3]) if len(sys.argv) > 3 else 30,
                   int(sys.argv[4]) if len(sys.argv) > 4 else 60)
    elif cmd == "clips":
        clips()
    else:
        print(__doc__)
