"""Auto-caption + hashtag generation for staged clips.

Reads the transcript selfclip already produced (captions/<clip>.txt if present)
or the source name, and writes a platform-ready caption + hashtags into each
queue item, so the Clip Desk shows finished copy - not a placeholder.

No filming, no writing: the human just approves and taps post.
"""
import json, pathlib, re, subprocess, sys

HERE = pathlib.Path(__file__).parent
Q = HERE / "queue"


def hook_from_text(txt):
    # first punchy line of the transcript = the hook
    line = re.split(r"[.!?\n]", txt.strip())[0][:80] if txt.strip() else ""
    return line.strip().capitalize()


def make_caption(meta):
    clip = meta.get("clip", "")
    tpath = HERE / "captions" / (pathlib.Path(clip).stem + ".txt")
    txt = tpath.read_text()[:600] if tpath.exists() else ""
    hook = hook_from_text(txt) or "You have to see this 👀"
    # campaign line tells us the topic/creator for tags
    camp = (meta.get("campaign") or "").lower()
    tags = ["#fyp", "#viral", "#clips"]
    for kw in ("crypto", "trading", "stocks", "gaming", "football", "ai", "tech"):
        if kw in camp:
            tags.append("#" + kw)
    caption = f"{hook}\n\n{' '.join(dict.fromkeys(tags))}"
    return caption


def run():
    n = 0
    for f in Q.glob("*.json"):
        m = json.loads(f.read_text())
        if m.get("caption"):
            continue
        m["caption"] = make_caption(m)
        f.write_text(json.dumps(m, indent=1))
        n += 1
    return n


if __name__ == "__main__":
    print(f"captioned {run()} clip(s)")
