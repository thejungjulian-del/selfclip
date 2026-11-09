#!/usr/bin/env python3
"""
Clip production line + posting queue + earnings tracker.

Three niches, one themed account each: sports card breaking, trading streamers, and
artists/music. Each carries its own layout, hashtags and edit style. The point is that the bottleneck in clipping income is not uploading — it's
finding and cutting good moments, then keeping track of what earned. So this automates
production and record-keeping, and leaves posting to you (bulk automated posting is what
gets accounts restricted, and TikTok's official API only allows private drafts for
unaudited apps).

    downloaded campaign footage  →  9:16 + captions  →  queue  →  you post  →  track views/$

Workflow:
    python3 batch.py niches                           # the 3 niches + which account for what
    python3 batch.py campaigns                        # every campaign, grouped by niche
    python3 batch.py prep backyard ~/Downloads/breaks # process a folder of raw clips
    python3 batch.py queue                            # what's ready to post
    python3 batch.py posted <id> <url>                # mark posted
    python3 batch.py views <id> <n>                   # record a view count
    python3 batch.py earnings                         # $ by campaign, and per clip
"""
import os
import re
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone

import captions
import layout

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "production.db")
OUT = os.path.join(HERE, "ready")

# ---------------------------------------------------------------- niches
# One themed account per niche. The constraint isn't platform rules — only 2 of 33 campaigns
# require a dedicated page — it's that a mixed-topic account gets poor algorithmic
# distribution, and views are the pay. So group campaigns that share an audience.
#
# CPM/accept are what the listing published. VERIFY on the campaign page: titles and
# structured fields have disagreed before (Matt Hazen $1.50 in title vs $3.50 in field;
# PB Blake $1.00 field vs "$100 per 1M views" = $0.10 in the brief).
#
# Layout defaults are a starting guess per content shape. Always confirm with:
#     python3 layout.py preview <a_real_source_file> --at 10
NICHES = {
    "cards": {
        "name": "Sports card breaking",
        "handle_idea": "@breakclipsdaily",
        # MEASURED off real Backyard Breaks footage, not guessed: the source is a TWO-PANEL
        # frame — reactions on the left ~68%, a dedicated card-cam on the right ~32%. A
        # single 9:16 crop throws one of them away. So stack them: reactions on top (the
        # hook — you see the face drop), card reveal below in the taller panel (the payoff).
        # Rects are FRACTIONS so the preset survives a change of source resolution.
        "layout": {"mode": "stack", "cam": (0.0, 0.0, 0.68, 1.0),
                   "main": (0.68, 0.0, 1.0, 1.0), "split": 760},
        "hashtags": "#sportscards #cardbreaking #cardtok #whatnot #grailpull #pull",
        "edit_style": "fast cuts on the reveal; hold on the reaction. Big pull = the hook.",
        "campaigns": {
            "backyard":  {"title": "Backyard Breaks", "route": "whop.com/cliphouse-5c",
                          "cpm": 2.00, "accept": 85, "left": 12630, "dedicated_page": True,
                          "source": "official Clip Context Tracker sheet",
                          "brief": "Most viral moments — big pulls, insane reactions. Upbeat."},
            "fanatics":  {"title": "Fanatics Live", "route": "whop.com/creatorxchange-clipping",
                          "cpm": 2.00, "accept": 38, "left": 4997, "dedicated_page": False,
                          "source": "live sports card breaks",
                          "brief": "People opening cards and reacting."},
            "letsgo":    {"title": "LETSGO Grail Pull", "route": "(see campaigns.py)",
                          "cpm": 2.00, "accept": 26, "left": 1935, "dedicated_page": False,
                          "source": "IRL reaction footage", "brief": "Grail pulls, IRL reactions."},
        },
    },
    "trading": {
        "name": "Trading streamers",
        "handle_idea": "@tradefloorclips",
        # charts are wide and detail-critical — a tight 9:16 crop destroys them. band keeps
        # the full width readable and leaves room under it for captions.
        "layout": {"mode": "band", "x0": 0.06, "x1": 0.94, "top": 0.20},
        "hashtags": "#trading #daytrading #stockmarket #tradingclips #futures #propfirm",
        "edit_style": "Lead with the P&L number or the reaction. Keep the chart legible.",
        "campaigns": {
            "tjr":       {"title": "TJR (TJRTrades)", "route": "(see campaigns.py)",
                          "cpm": 1.00, "accept": 100, "left": 636, "dedicated_page": False,
                          "source": "live day-trading streams",
                          "brief": "Daily campaign ~11:30 EST; approvals same day."},
            "tradeaxis": {"title": "TradeAxis System", "route": "(see campaigns.py)",
                          "cpm": 2.00, "accept": 86, "left": 601, "dedicated_page": False,
                          "source": "supplied assets", "brief": "System/strategy content."},
            "kannada":   {"title": "Trading Kannada Clips", "route": "(see campaigns.py)",
                          "cpm": 3.00, "accept": 63, "left": 681, "dedicated_page": False,
                          "source": "supplied assets", "brief": "Kannada-language trading clips."},
        },
        "warning": "FINANCE category — TikTok restricts financial-services promo, and this "
                   "content is often course-selling. Highest account risk of the three.",
    },
    "artist": {
        "name": "Artists / music",
        "handle_idea": "@newsoundcuts",
        # performance and music-video footage is usually subject-centred; tight crop reads
        # best on a phone. Wide stage shots may need band instead — check with preview.
        "layout": {"mode": "full", "focus": 0.5},
        "hashtags": "#newmusic #edit #musicedit #fyp #song #onrepeat",
        "edit_style": "Beat-synced cuts. The drop is the hook — build to it in the first 2s.",
        "campaigns": {
            "makeitright": {"title": "Make It Right (Alan Fitzpatrick)", "route": "whop.com/the-cross-records-9d",
                            "cpm": 2.50, "accept": 100, "left": 2102, "dedicated_page": False,
                            "source": "official song", "brief": "Capped, ultra-selective."},
            "yungblud":    {"title": "Yungblud Tour Content", "route": "(see campaigns.py)",
                            "cpm": 1.00, "accept": 75, "left": 2219, "dedicated_page": False,
                            "source": "approved assets folder", "brief": "Drive tour hype."},
            "twinfin":     {"title": "Twin Fin — California Sober", "route": "(see campaigns.py)",
                            "cpm": 1.50, "accept": 57, "left": 1981, "dedicated_page": False,
                            "source": "sound campaign", "brief": "EDITS wanted."},
            "sarahlola":   {"title": "Sarah Lola — You're All I Need", "route": "(see campaigns.py)",
                            "cpm": 1.00, "accept": 55, "left": 1206, "dedicated_page": False,
                            "source": "sound campaign", "brief": "EDITS wanted. $100 bonus."},
            "notomorrow":  {"title": "No Tomorrow Audio", "route": "(see campaigns.py)",
                            "cpm": 1.00, "accept": 86, "left": 809, "dedicated_page": False,
                            "source": "audio campaign",
                            "brief": "EDITS — soccer/friendship/nostalgia angles."},
        },
        "note": "Biggest niche by far: 53 live campaigns, ~$95k available. Most 'edit' "
                "campaigns live here, which is where EditForge applies.",
    },
}


def all_campaigns():
    """Flat {key: campaign} across every niche, with the niche stamped on each."""
    out = {}
    for nk, n in NICHES.items():
        for ck, cv in n["campaigns"].items():
            out[ck] = {**cv, "niche": nk, "layout": n["layout"],
                       "hashtags": n["hashtags"], "edit_style": n["edit_style"]}
    return out


CAMPAIGNS = all_campaigns()


def conn():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    c.executescript("""
        CREATE TABLE IF NOT EXISTS clips (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            campaign TEXT NOT NULL,
            src TEXT NOT NULL,
            out TEXT NOT NULL,
            duration REAL,
            caption_count INTEGER,
            made_at TEXT,
            posted_at TEXT,
            post_url TEXT,
            views INTEGER DEFAULT 0,
            views_at TEXT,
            approved INTEGER,
            UNIQUE(campaign, src)
        );
    """)
    return c


def show_niches():
    print("THREE NICHES — one themed account each. Mixed-topic accounts get poor")
    print("distribution, and views are the pay.\n")
    for nk, n in NICHES.items():
        tot = sum(c["left"] for c in n["campaigns"].values())
        best = max(n["campaigns"].values(), key=lambda c: c["cpm"] * c["accept"] / 100)
        print(f"── {n['name'].upper()}   suggested handle {n['handle_idea']}")
        print(f"   {len(n['campaigns'])} campaigns tracked · ${tot:,} available · "
              f"layout '{n['layout']['mode']}'")
        print(f"   best EV: {best['title']} — ${best['cpm']:.2f} CPM x {best['accept']}% "
              f"= ${best['cpm']*best['accept']/100:.2f}/1k")
        print(f"   edit style: {n['edit_style']}")
        print(f"   hashtags:   {n['hashtags']}")
        if n.get("warning"):
            print(f"   ⚠ {n['warning']}")
        if n.get("note"):
            print(f"   note: {n['note']}")
        print()
    print("  per-campaign detail:  python3 batch.py campaigns")


def show_campaigns():
    for nk, n in NICHES.items():
        print(f"\n── {n['name'].upper()}")
        print(f"   {'KEY':<12}{'CAMPAIGN':<32}{'CPM':>7}{'ACC':>6}{'EV/1k':>8}{'LEFT':>9}  PAGE")
        print("   " + "-" * 84)
        for k, v in sorted(n["campaigns"].items(),
                           key=lambda kv: -kv[1]["cpm"] * kv[1]["accept"] / 100):
            ev = v["cpm"] * v["accept"] / 100
            print(f"   {k:<12}{v['title'][:31]:<32}${v['cpm']:>6.2f}{v['accept']:>5}%"
                  f"${ev:>7.2f}${v['left']:>8,}  "
                  f"{'DEDICATED' if v['dedicated_page'] else 'any'}")
        for k, v in n["campaigns"].items():
            print(f"     {k}: {v['route']}")
            print(f"        source: {v['source']}  |  {v['brief']}")


def dur(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                        "-of", "default=nk=1:nw=1", path], capture_output=True, text=True)
    try:
        return float(r.stdout.strip())
    except ValueError:
        return None


def prep(campaign, folder, limit=None):
    if campaign not in CAMPAIGNS:
        sys.exit(f"unknown campaign '{campaign}'. one of: {', '.join(CAMPAIGNS)}")
    if not os.path.isdir(folder):
        sys.exit(f"not a folder: {folder}")
    vids = sorted(f for f in os.listdir(folder)
                  if f.lower().endswith((".mp4", ".mov", ".mkv", ".ts", ".webm")))
    if not vids:
        sys.exit(f"no video files in {folder}")
    if limit:
        vids = vids[:int(limit)]
    os.makedirs(OUT, exist_ok=True)
    c = conn()
    print(f"{CAMPAIGNS[campaign]['title']}: processing {len(vids)} file(s)\n")
    made = skipped = failed = 0
    for i, v in enumerate(vids, 1):
        src = os.path.join(folder, v)
        if c.execute("SELECT 1 FROM clips WHERE campaign=? AND src=?", (campaign, src)).fetchone():
            print(f"  [{i}/{len(vids)}] {v[:44]:<46} already done")
            skipped += 1
            continue
        stem = re.sub(r"[^A-Za-z0-9_-]+", "_", os.path.splitext(v)[0])[:40]
        vert = os.path.join(OUT, f"{campaign}_{stem}_v.mp4")
        final = os.path.join(OUT, f"{campaign}_{stem}.mp4")
        print(f"  [{i}/{len(vids)}] {v[:44]:<46} ", end="", flush=True)
        ok, err = layout.render(src, vert, params=CAMPAIGNS[campaign]['layout'])
        if not ok:
            print(f"layout failed: {err[:60]}")
            failed += 1
            continue
        n_caps = 0
        try:
            segs = captions.transcribe(vert)
            n_caps = len(segs)
            done, cerr = captions.burn(vert, final, segs)
            if done:
                os.remove(vert)
            else:
                os.replace(vert, final)      # keep the uncaptioned clip rather than lose it
                print("(captions skipped) ", end="")
        except Exception:
            os.replace(vert, final)
            print("(captions skipped) ", end="")
        c.execute("INSERT INTO clips (campaign,src,out,duration,caption_count,made_at) "
                  "VALUES (?,?,?,?,?,?)",
                  (campaign, src, final, dur(final), n_caps,
                   datetime.now(timezone.utc).isoformat()))
        c.commit()
        made += 1
        print(f"ready  ({os.path.getsize(final)/1e6:.1f} MB, {n_caps} captions)")
    print(f"\n  {made} ready · {skipped} already done · {failed} failed")
    print(f"  output: {OUT}/     next: python3 batch.py queue")


def queue():
    c = conn()
    rows = c.execute("SELECT * FROM clips WHERE posted_at IS NULL ORDER BY campaign, id").fetchall()
    if not rows:
        print("nothing waiting to post. Run 'prep' on a folder of campaign footage.")
        return
    print(f"{len(rows)} clip(s) ready to post\n")
    cur = None
    for r in rows:
        if r["campaign"] != cur:
            cur = r["campaign"]
            cf = CAMPAIGNS.get(cur, {})
            print(f"── {cf.get('title', cur)}  (${cf.get('cpm', 0):.2f} CPM, "
                  f"{cf.get('accept', 0)}% accepted)")
            print(f"   hashtags: {cf.get('hashtags', '')}")
            print(f"   edit: {cf.get('edit_style', '')}")
        print(f"   #{r['id']:<4} {os.path.basename(r['out'])[:52]:<54} "
              f"{r['duration'] or 0:>5.1f}s  {r['caption_count'] or 0} caps")
    print("\n  after posting:  python3 batch.py posted <id> <url>")


def posted(cid, url):
    c = conn()
    if not c.execute("SELECT 1 FROM clips WHERE id=?", (cid,)).fetchone():
        sys.exit(f"no clip #{cid}")
    c.execute("UPDATE clips SET posted_at=?, post_url=? WHERE id=?",
              (datetime.now(timezone.utc).isoformat(), url, cid))
    c.commit()
    print(f"clip #{cid} marked posted -> {url}")


def views(cid, n):
    c = conn()
    r = c.execute("SELECT * FROM clips WHERE id=?", (cid,)).fetchone()
    if not r:
        sys.exit(f"no clip #{cid}")
    c.execute("UPDATE clips SET views=?, views_at=? WHERE id=?",
              (int(n), datetime.now(timezone.utc).isoformat(), cid))
    c.commit()
    cpm = CAMPAIGNS.get(r["campaign"], {}).get("cpm", 0)
    print(f"clip #{cid}: {int(n):,} views  ->  ${int(n)/1000*cpm:.2f} at ${cpm:.2f} CPM")


def earnings():
    c = conn()
    rows = c.execute("SELECT * FROM clips").fetchall()
    if not rows:
        print("no clips yet")
        return
    print(f"{'CAMPAIGN':<18}{'MADE':>6}{'POSTED':>8}{'VIEWS':>12}{'GROSS $':>10}{'CPM':>7}")
    print("-" * 62)
    tv = tg = 0
    for k in CAMPAIGNS:
        g = [r for r in rows if r["campaign"] == k]
        if not g:
            continue
        cpm = CAMPAIGNS[k]["cpm"]
        v = sum(r["views"] or 0 for r in g)
        gross = v / 1000 * cpm
        tv += v
        tg += gross
        print(f"{CAMPAIGNS[k]['title']:<18}{len(g):>6}"
              f"{sum(1 for r in g if r['posted_at']):>8}{v:>12,}${gross:>9.2f}${cpm:>6.2f}")
    print("-" * 62)
    print(f"{'TOTAL':<18}{len(rows):>6}{sum(1 for r in rows if r['posted_at']):>8}"
          f"{tv:>12,}${tg:>9.2f}")
    posted_n = [r for r in rows if r["posted_at"]]
    if posted_n:
        print(f"\n  average views per posted clip: {tv/len(posted_n):,.0f}")
        print(f"  average $ per posted clip:     ${tg/len(posted_n):.2f}")
        best = max(posted_n, key=lambda r: r["views"] or 0)
        if best["views"]:
            print(f"  best clip: #{best['id']} {best['views']:,} views "
                  f"({os.path.basename(best['out'])[:40]})")
    print("\n  gross = views x CPM. Actual payout depends on approval "
          "(Backyard Breaks accepts ~85%).")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "help"
    if cmd == "niches":
        show_niches()
    elif cmd == "campaigns":
        show_campaigns()
    elif cmd == "prep" and len(sys.argv) > 3:
        prep(sys.argv[2], sys.argv[3], sys.argv[4] if len(sys.argv) > 4 else None)
    elif cmd == "queue":
        queue()
    elif cmd == "posted" and len(sys.argv) > 3:
        posted(int(sys.argv[2]), sys.argv[3])
    elif cmd == "views" and len(sys.argv) > 3:
        views(int(sys.argv[2]), sys.argv[3])
    elif cmd == "earnings":
        earnings()
    else:
        print(__doc__)
