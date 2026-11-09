"""Hands-off clip factory: drop LICENSED source footage in inbox/, get
post-ready videos in ready/ automatically. No filming, no manual editing.

inbox/ (campaign-provided VODs, streamers you clip for, your own downloads
you hold rights to)  ->  auto-detect highlights -> 9:16 + captions -> ready/
                      ->  queued with an AI-drafted caption + best-fit campaign

Legitimacy gate: this processes files YOU place in inbox/. It does not scrape
or download anyone's content - you supply footage you have the right to clip
(which is the whole point of a clipping campaign: the creator licenses it).
"""
import json, os, pathlib, subprocess, sys, time

HERE = pathlib.Path(__file__).parent
INBOX, READY, QUEUE = HERE / "inbox", HERE / "ready", HERE / "queue"
SEEN = HERE / "autoclip_seen.json"
sys.path.insert(0, str(HERE))


def seen():
    return set(json.loads(SEEN.read_text())) if SEEN.exists() else set()


def mark(s):
    SEEN.write_text(json.dumps(list(s)))


def best_campaign():
    f = HERE / "today_campaigns.txt"
    if f.exists():
        for line in f.read_text().splitlines():
            if line.strip().startswith("$"):
                return line.strip()
    return "check today_campaigns.txt"


def process_new():
    for d in (READY, QUEUE):
        d.mkdir(exist_ok=True)
    done = seen()
    vids = [p for p in INBOX.glob("*")
            if p.suffix.lower() in (".mp4", ".mkv", ".mov", ".webm")
            and str(p) not in done]
    if not vids:
        return 0
    import selfclip
    made = 0
    for v in vids:
        # selfclip.process cuts highlight, formats 9:16, captions -> ready/
        try:
            out, err = selfclip.process(str(v))
        except Exception as e:
            out, err = None, str(e)
        done.add(str(v))
        if out:
            made += 1
            meta = {"clip": os.path.basename(out), "source": v.name,
                    "campaign": best_campaign(), "made": time.strftime("%m-%d %H:%M"),
                    "status": "awaiting_approval"}
            (QUEUE / (pathlib.Path(out).stem + ".json")).write_text(json.dumps(meta, indent=1))
    mark(done)
    try:
        import caption; caption.run()
    except Exception:
        pass
    if made:
        subprocess.run(["osascript", "-e",
            f'display notification "{made} clip(s) auto-made, queued for 1-tap approval" '
            f'with title "CLIPS READY" sound name "Glass"'], capture_output=True)
    return made


if __name__ == "__main__":
    n = process_new()
    print(f"produced {n} clip(s) from inbox/")
