"""Clip Desk - http://localhost:8753. Everything AI-made, staged, one-tap to ship.

Columns: INBOX (source you dropped) -> QUEUE (auto-made, awaiting your ok) ->
APPROVED (you tapped go) -> POSTED. Approving is one click; it copies the clip +
its caption to APPROVED and pings you to post (the manual publish that keeps the
account unbanned). Nothing auto-publishes to any platform.
"""
import json, pathlib, shutil, time
from flask import Flask, jsonify, request, Response

HERE = pathlib.Path(__file__).parent
Q, RD, AP, PO = HERE/"queue", HERE/"ready", HERE/"approved", HERE/"posted"
IN = HERE/"inbox"
for d in (Q, RD, AP, PO, IN): d.mkdir(exist_ok=True)
app = Flask(__name__)


def items():
    q=[]
    for f in Q.glob("*.json"):
        m=json.loads(f.read_text()); m["_f"]=f.name; q.append(m)
    ap=[json.loads(f.read_text())|{"_f":f.name} for f in AP.glob("*.json")]
    po=[json.loads(f.read_text())|{"_f":f.name} for f in PO.glob("*.json")]
    inbox=[p.name for p in IN.glob("*") if p.suffix.lower() in (".mp4",".mkv",".mov",".webm")]
    return q, ap, po, inbox


@app.route("/api/state")
def state():
    q,ap,po,inbox=items()
    return jsonify({"queue":q,"approved":ap,"posted":po,"inbox":inbox})


@app.route("/api/approve", methods=["POST"])
def approve():
    fn=request.json["f"]
    src=Q/fn
    if src.exists():
        m=json.loads(src.read_text()); m["status"]="approved"; m["approved"]=time.strftime("%m-%d %H:%M")
        (AP/fn).write_text(json.dumps(m,indent=1)); src.unlink()
    return jsonify({"ok":True})


@app.route("/api/approve_all", methods=["POST"])
def approve_all():
    n=0
    for src in list(Q.glob("*.json")):
        m=json.loads(src.read_text()); m["status"]="approved"; m["approved"]=time.strftime("%m-%d %H:%M")
        (AP/src.name).write_text(json.dumps(m,indent=1)); src.unlink(); n+=1
    return jsonify({"ok":True,"n":n})


@app.route("/api/posted", methods=["POST"])
def posted():
    fn=request.json["f"]; src=AP/fn
    if src.exists():
        m=json.loads(src.read_text()); m["status"]="posted"; m["posted"]=time.strftime("%m-%d %H:%M")
        (PO/fn).write_text(json.dumps(m,indent=1)); src.unlink()
    return jsonify({"ok":True})




@app.route("/api/launch", methods=["POST"])
def launch():
    import subprocess
    fn = request.json["f"]
    plat = request.json.get("platform", "tiktok")
    src = AP / fn
    if not src.exists():
        src = Q / fn
    if not src.exists():
        return jsonify({"ok": False})
    m = json.loads(src.read_text())
    cap = m.get("caption", "")
    clip = RD / m.get("clip", "")
    # 1) caption -> clipboard (paste-ready)
    subprocess.run("pbcopy", input=cap.encode(), capture_output=True)
    # 2) reveal the clip file in Finder (drag-ready)
    if clip.exists():
        subprocess.run(["open", "-R", str(clip)], capture_output=True)
    # 3) open the platform's upload page
    urls = {"tiktok": "https://www.tiktok.com/upload",
            "instagram": "https://www.instagram.com",
            "youtube": "https://www.youtube.com/upload"}
    subprocess.run(["open", urls.get(plat, urls["tiktok"])], capture_output=True)
    return jsonify({"ok": True})


@app.route("/clip/<name>")
def clip(name):
    p=RD/name
    return Response(p.read_bytes(), mimetype="video/mp4") if p.exists() else ("",404)


HTML="""<!doctype html><meta charset=utf-8><title>Clip Desk</title>
<style>
:root{--bg:#0d0f12;--card:#16191e;--line:#242830;--ink:#e8eaed;--dim:#8a919c;--gold:#e8c268;--green:#6fc28f;--blue:#6fa8dc}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px -apple-system,sans-serif}
header{display:flex;align-items:baseline;gap:16px;padding:20px 26px;border-bottom:1px solid var(--line)}
h1{font-size:20px;margin:0}h1 span{color:var(--gold)}#s{color:var(--dim);font-size:13px}
.big{background:var(--green);color:#08120b;border:none;border-radius:8px;padding:9px 18px;font-weight:700;cursor:pointer}
.board{display:grid;grid-template-columns:1fr 1.4fr 1fr 1fr;gap:14px;padding:20px 26px}
.col h2{font-size:11px;letter-spacing:.16em;color:var(--dim);text-transform:uppercase;margin:0 0 10px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px;margin-bottom:12px}
video{width:100%;border-radius:6px;background:#000;max-height:340px}
.cap{font-size:13px;margin:8px 0}.camp{color:var(--gold);font-size:12px}.src{color:var(--dim);font-size:11px}
button{background:var(--card);color:var(--ink);border:1px solid var(--line);border-radius:6px;padding:5px 12px;cursor:pointer;font-size:12px;margin-top:6px}
button.go{background:var(--green);color:#08120b;border:none;font-weight:700}
.empty{color:var(--dim);font-style:italic;font-size:13px}
.hint{font-size:12px;color:var(--dim);border-top:1px solid var(--line);margin-top:8px;padding-top:8px}
</style>
<header><h1>Clip <span>Desk</span></h1><div id=s>loading…</div><div style=flex:1></div>
<button class=big onclick=approveAll()>✓ Approve all</button></header>
<div class=board id=b></div>
<script>
async function load(){
 const d=await(await fetch('/api/state')).json();
 document.getElementById('s').textContent=`${d.inbox.length} in inbox · ${d.queue.length} awaiting approval · ${d.approved.length} ready to post · ${d.posted.length} posted`;
 const b=document.getElementById('b');b.innerHTML='';
 col(b,'Inbox (drop source here)',d.inbox.map(n=>`<div class=card><div class=src>${n}</div><div class=hint>auto-processing every 5 min → becomes a clip</div></div>`),d.inbox.length);
 col(b,'Awaiting your OK',d.queue.map(m=>card(m,'queue')),d.queue.length);
 col(b,'Ready to post',d.approved.map(m=>card(m,'approved')),d.approved.length);
 col(b,'Posted 🎉',d.posted.map(m=>`<div class=card><div class=cap>${m.clip}</div><div class=src>posted ${m.posted}</div></div>`),d.posted.length);
}
function col(b,t,cards,n){const c=document.createElement('div');c.className='col';c.innerHTML=`<h2>${t} · ${n}</h2>`+(cards.length?cards.join(''):'<div class=empty>—</div>');b.appendChild(c)}
function card(m,stage){
 return `<div class=card><video src="/clip/${m.clip}" controls preload=metadata></video>
 <div class=cap>${m.caption||'<i>caption: '+(m.clip)+'</i>'}</div>
 <div class=camp>→ ${m.campaign||''}</div><div class=src>from ${m.source||''}</div>
 ${stage=='queue'?`<button class=go onclick="approve('${m._f}')">✓ approve</button>`:''}
 ${stage=='approved'?`<div class=hint><b>1-tap:</b> copies caption, opens the clip + upload page. Drag the clip in, paste (⌘V), post.</div>
 <button class=go onclick="launch('${m._f}','tiktok')">▶ Post to TikTok</button>
 <button onclick="launch('${m._f}','instagram')">Instagram</button>
 <button onclick="launch('${m._f}','youtube')">YouTube</button>
 <button onclick="donePost('${m._f}')">✓ done</button>`:''}
 </div>`}
async function approve(f){await fetch('/api/approve',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({f})});load()}
async function approveAll(){await fetch('/api/approve_all',{method:'POST'});load()}
async function launch(f,platform){await fetch('/api/launch',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({f,platform})})}
async function donePost(f){await fetch('/api/posted',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({f})});load()}
load();setInterval(load,15000);
</script>"""
@app.route("/")
def i(): return Response(HTML,mimetype="text/html")
if __name__=="__main__": app.run(port=8753)
