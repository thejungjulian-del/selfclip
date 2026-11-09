#!/usr/bin/env python3
"""
Twitch chat-velocity moment detector.

Reads chat anonymously (no API key, no OAuth) and looks for the moments worth clipping.
Three signals, in increasing order of how much they mean:

  velocity  — messages/sec spiking above a rolling baseline. Cheap, always available.
  laughter  — emote/laugh density (KEKW, LULW, OMEGALUL, LMAO, 💀). Confirms it was funny.
  clip-call — chat literally typing "clip it" / "clip that". A human-labelled clip moment.

The clip-call signal is the interesting one: it's both a detector AND a free label. If
velocity spikes line up with clip-calls, velocity is a validated proxy and we can act on
it in real time instead of waiting for chat to ask.

Usage:  python3 chat_watch.py <seconds> <channel> [channel ...]
"""
import re
import socket
import ssl
import statistics as st
import sys
import time
from collections import Counter, defaultdict, deque

HOST, PORT = "irc.chat.twitch.tv", 6697
BUCKET = 2.0            # seconds per bucket
BASELINE_WINDOW = 30    # buckets of history for the rolling baseline
SPIKE_RATIO = 2.2       # bucket rate / baseline to call a spike
MIN_RATE = 1.5          # msgs/sec floor, so dead chat can't "spike"

LAUGH = re.compile(
    r"(kekw|lulw|omegalul|lmao|lmfao|kek\b|xdd|😭|💀|🤣|joel\b|pepelaugh|icant)", re.I
)
CLIPCALL = re.compile(r"(clip (it|that|this)|clipped|someone clip|clip pls|clip please)", re.I)


def connect(channels):
    ctx = ssl.create_default_context()
    s = ctx.wrap_socket(
        socket.create_connection((HOST, PORT), timeout=15), server_hostname=HOST
    )
    s.settimeout(1.0)
    s.sendall(b"CAP REQ :twitch.tv/tags\r\n")
    s.sendall(b"PASS SCHMOOPIIE\r\nNICK justinfan" + str(int(time.time()) % 99999).encode() + b"\r\n")
    time.sleep(1.0)
    s.sendall(("JOIN " + ",".join("#" + c.lower() for c in channels) + "\r\n").encode())
    return s


MSG = re.compile(r"PRIVMSG #(?P<chan>[^ ]+) :(?P<text>.*)$")


def run(duration, channels, logpath=None):
    """Collect chat. Raw messages are appended to a JSONL log so analysis can be
    re-run later without re-collecting — collection and analysis stay separable."""
    import json

    log = open(logpath, "a", encoding="utf-8") if logpath else None
    s = connect(channels)
    start = time.time()
    buf = ""
    msgs = defaultdict(list)        # (t, text)
    last_reconnect = start
    print(f"listening to {', '.join(channels)} for {duration}s ...", flush=True)
    if log:
        print(f"logging raw messages to {logpath}", flush=True)
    while time.time() - start < duration:
        try:
            chunk = s.recv(8192).decode("utf-8", errors="replace")
        except socket.timeout:
            continue
        except Exception:
            # long runs drop; reconnect rather than lose the rest of the session
            if time.time() - last_reconnect > 20:
                try:
                    s.close()
                except Exception:
                    pass
                try:
                    s = connect(channels)
                    last_reconnect = time.time()
                    print(f"  [reconnected at t={time.time()-start:.0f}s]", flush=True)
                except Exception:
                    break
            continue
        if not chunk:
            continue
        buf += chunk
        while "\r\n" in buf:
            line, buf = buf.split("\r\n", 1)
            if line.startswith("PING") or " PING " in line:
                s.sendall(b"PONG :tmi.twitch.tv\r\n")
                continue
            m = MSG.search(line)
            if m:
                t = time.time() - start
                chan, text = m.group("chan"), m.group("text")
                msgs[chan].append((t, text))
                if log:
                    log.write(json.dumps({"t": round(t, 2), "ch": chan, "x": text}) + "\n")
                    if len(msgs[chan]) % 200 == 0:
                        log.flush()
    try:
        s.close()
    except Exception:
        pass
    if log:
        log.flush()
        log.close()
    return msgs, duration


def analyse(msgs, duration):
    if not any(msgs.values()):
        print("No messages received. The channels may be offline — try live ones.")
        return

    for chan, rows in sorted(msgs.items(), key=lambda kv: -len(kv[1])):
        n = len(rows)
        if n < 20:
            print(f"--- #{chan}: only {n} messages (offline or slow) — skipping\n")
            continue

        nb = int(duration // BUCKET) + 1
        rate = [0] * nb
        texts = defaultdict(list)
        for t, txt in rows:
            b = int(t // BUCKET)
            if b < nb:
                rate[b] += 1
                texts[b].append(txt)
        rps = [r / BUCKET for r in rate]

        # rolling-baseline spike detection
        spikes, hist = [], deque(maxlen=BASELINE_WINDOW)
        for b, r in enumerate(rps):
            base = st.median(hist) if len(hist) >= 5 else None
            if base and base > 0 and r >= MIN_RATE and r / base >= SPIKE_RATIO:
                spikes.append((b, r, r / base))
            hist.append(r)

        laughs = sum(1 for _, txt in rows if LAUGH.search(txt))
        calls = [(t, txt) for t, txt in rows if CLIPCALL.search(txt)]

        print(f"--- #{chan}")
        print(f"    {n} msgs in {duration:.0f}s   avg {n/duration:.1f}/s   peak {max(rps):.1f}/s")
        print(f"    laughter density {100*laughs/n:.1f}% of messages")
        print(f"    clip-calls: {len(calls)}")
        print(f"    spikes detected: {len(spikes)}")

        for b, r, ratio in spikes[:6]:
            sample = [t for t in texts[b] if len(t) < 60][:3]
            had_call = any(CLIPCALL.search(t) for t in texts[b])
            had_laugh = sum(1 for t in texts[b] if LAUGH.search(t))
            print(f"      t={b*BUCKET:5.0f}s  {r:5.1f}/s  ({ratio:.1f}x baseline)"
                  f"  laugh={had_laugh:<3} clipcall={'YES' if had_call else '-'}")
            for smp in sample:
                print(f"            | {smp}")

        # --- the validation: do clip-calls concentrate inside spikes? ---
        if calls and spikes:
            spike_buckets = {b for b, _, _ in spikes}
            # widen by +/-1 bucket: chat reacts a beat late
            near = set()
            for b in spike_buckets:
                near |= {b - 1, b, b + 1}
            inside = sum(1 for t, _ in calls if int(t // BUCKET) in near)
            cov = 100 * len(near) / nb
            hit = 100 * inside / len(calls)
            lift = (hit / cov) if cov else 0
            print(f"    VALIDATION: spikes cover {cov:.0f}% of airtime but contain "
                  f"{hit:.0f}% of clip-calls  ->  {lift:.1f}x lift")
            if lift >= 1.5:
                print("      => velocity is a usable proxy for clippable moments")
            else:
                print("      => velocity did NOT concentrate clip-calls here (weak/no signal)")
        elif not calls:
            print("    VALIDATION: no clip-calls in this window — need a longer sample")
        print()


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)
    dur = float(sys.argv[1])
    args = sys.argv[2:]
    logpath = None
    if "--log" in args:
        i = args.index("--log")
        logpath = args[i + 1]
        args = args[:i] + args[i + 2:]
    m, d = run(dur, args, logpath)
    print()
    analyse(m, d)
