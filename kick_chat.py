#!/usr/bin/env python3
"""
Kick chat reader — real-time, headless, no Cloudflare problem.

Getting here took some digging, so the findings are worth recording:

  * kick.com's REST API and even its plain HTML pages return 403 to any script.
    Cloudflare blocks curl/urllib regardless of headers. Both were verified dead.
  * But Kick's chat does not go through that API at all — it rides Pusher. Connecting
    straight to Pusher from Python works, needs no auth, and never touches kick.com.
    Verified live: 53 messages in 22 seconds.
  * The only thing still behind Cloudflare is the chatroom_id, which is NOT the same as
    the channel_id that yt-dlp reports (1202499 vs 1209488 for the same channel, so it
    can't be derived). It has to be looked up once per streamer and cached — chatroom ids
    are stable, so that's a one-off.

    To look one up: open kick.com/popout/<slug>/chat in a browser and run
        await fetch('/api/v2/channels/<slug>').then(r=>r.json()).then(j=>j.chatroom.id)
    A same-origin fetch inside the page carries the clearance cookie and returns 200.
    Then store it with:  python3 kick_chat.py set <slug> <chatroom_id>

Usage:
    python3 kick_chat.py set <slug> <chatroom_id>   # cache an id
    python3 kick_chat.py ids                        # show cached ids
    python3 kick_chat.py watch <slug> [seconds]     # live chat + spike detection
"""
import json
import os
import re
import statistics as st
import sys
import threading
import time
from collections import deque

HERE = os.path.dirname(os.path.abspath(__file__))
IDS = os.path.join(HERE, "kick_chatrooms.json")

# Public client-side Pusher app key used by kick.com's own web app (not a secret; it is
# shipped in the page bundle and documented by every open-source Kick chat client).
PUSHER_KEY = "32cbd69e4b950bf97679"
PUSHER_URL = (f"wss://ws-us2.pusher.com/app/{PUSHER_KEY}"
              "?protocol=7&client=js&version=8.4.0&flash=false")
CHAT_EVENT = "App\\Events\\ChatMessageEvent"

# Kick renders emotes inline as [emote:12345:NAME], so the emote NAME is present in the
# message text and a plain regex still catches it. KEKW exists on Kick too (verified).
LAUGH = re.compile(r"(kekw|lulw|omegalul|lmao|lmfao|kek\b|xdd|😭|💀|🤣|pepelaugh|icant"
                   r"|omegaluliguess|hahaa|lulhuh)", re.I)
CLIPCALL = re.compile(r"(clip (it|that|this)|clipped|someone clip|clip pls|clip please)", re.I)

BUCKET = 2.0
BASE_WINDOW = 30
SPIKE_RATIO = 2.2
MIN_RATE = 1.5
MIN_LAUGHS = 2
MIN_LAUGH_RATE = 1.5


def load_ids():
    if os.path.exists(IDS):
        try:
            return json.load(open(IDS))
        except Exception:
            pass
    return {}


def save_id(slug, chatroom_id):
    d = load_ids()
    d[slug.lower()] = int(chatroom_id)
    json.dump(d, open(IDS, "w"), indent=1, sort_keys=True)
    print(f"cached {slug} -> {chatroom_id}")


def chatroom_id(slug):
    return load_ids().get(slug.lower())


def chat_loop(slug, on_spike, stop_evt, on_message=None):
    """Same interface as the Twitch chat_loop in live_clip.py, so either can drive the
    same clip pipeline. on_spike(ts, rate, ratio, laughs, calls)."""
    try:
        import websocket
    except ImportError:
        print("  kick chat needs websocket-client:  pip3 install --user websocket-client")
        return
    room = chatroom_id(slug)
    if not room:
        print(f"  no cached chatroom_id for '{slug}' — see the docstring in kick_chat.py")
        return

    state = {"count": 0, "laughs": 0, "calls": 0, "start": time.time()}
    hist = deque(maxlen=BASE_WINDOW)

    def tick():
        while not stop_evt.is_set():
            time.sleep(BUCKET)
            now = time.time()
            rate = state["count"] / BUCKET
            lrate = state["laughs"] / BUCKET
            base = st.median(hist) if len(hist) >= 5 else None
            if base and base > 0:
                vol = rate >= MIN_RATE and rate / base >= SPIKE_RATIO
                if (vol and state["laughs"] >= MIN_LAUGHS) or lrate >= MIN_LAUGH_RATE \
                        or state["calls"]:
                    on_spike(now, rate, rate / base, state["laughs"], state["calls"])
            hist.append(rate)
            state["count"] = state["laughs"] = state["calls"] = 0

    threading.Thread(target=tick, daemon=True).start()

    def on_open(ws):
        ws.send(json.dumps({"event": "pusher:subscribe",
                            "data": {"auth": "", "channel": f"chatrooms.{room}.v2"}}))

    def on_msg(ws, raw):
        try:
            d = json.loads(raw)
        except Exception:
            return
        if d.get("event") != CHAT_EVENT:
            return
        try:
            p = json.loads(d.get("data") or "{}")
        except Exception:
            return
        txt = p.get("content") or ""
        state["count"] += 1
        if LAUGH.search(txt):
            state["laughs"] += 1
        if CLIPCALL.search(txt):
            state["calls"] += 1
        if on_message:
            on_message((p.get("sender") or {}).get("username", "?"), txt)

    while not stop_evt.is_set():
        try:
            ws = websocket.WebSocketApp(PUSHER_URL, on_open=on_open, on_message=on_msg)
            ws.run_forever(ping_interval=25, ping_timeout=10)
        except Exception:
            pass
        if not stop_evt.is_set():
            time.sleep(5)      # dropped — reconnect


def watch(slug, seconds=60):
    room = chatroom_id(slug)
    if not room:
        sys.exit(f"no cached chatroom_id for '{slug}'.\n"
                 f"  Look it up in a browser on kick.com/popout/{slug}/chat with:\n"
                 f"    await fetch('/api/v2/channels/{slug}').then(r=>r.json()).then(j=>j.chatroom.id)\n"
                 f"  then: python3 kick_chat.py set {slug} <id>")
    print(f"watching kick #{slug} (chatroom {room}) for {seconds}s\n")
    stop = threading.Event()
    stats = {"msgs": 0, "spikes": 0, "laughs": 0, "calls": 0}

    def spike(ts, rate, ratio, laughs, calls):
        stats["spikes"] += 1
        tag = "CLIPCALL" if calls else "laugh"
        print(f"  [{time.strftime('%H:%M:%S')}] {tag} SPIKE  {rate:.1f} msg/s "
              f"({ratio:.1f}x)  laughs={laughs} calls={calls}")

    def msg(who, txt):
        stats["msgs"] += 1
        if LAUGH.search(txt):
            stats["laughs"] += 1
        if CLIPCALL.search(txt):
            stats["calls"] += 1

    threading.Thread(target=chat_loop, args=(slug, spike, stop, msg), daemon=True).start()
    time.sleep(seconds)
    stop.set()
    time.sleep(0.5)
    d = seconds or 1
    print(f"\n  {stats['msgs']} msgs in {seconds}s ({stats['msgs']/d:.1f}/s)  "
          f"laughs {stats['laughs']}  clip-calls {stats['calls']}  spikes {stats['spikes']}")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "help"
    if cmd == "set" and len(sys.argv) > 3:
        save_id(sys.argv[2], sys.argv[3])
    elif cmd == "ids":
        for k, v in sorted(load_ids().items()):
            print(f"  {k:<20}{v}")
    elif cmd == "watch" and len(sys.argv) > 2:
        watch(sys.argv[2], int(sys.argv[3]) if len(sys.argv) > 3 else 60)
    else:
        print(__doc__)
