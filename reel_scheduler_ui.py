#!/usr/bin/env python3
"""
Reel Scheduler UI  -  python reel_scheduler_ui.py
Opens http://localhost:8765 . No pip install needed (stdlib only).
Flow: API key -> channels (copy IDs) -> video + thumbnail frame + caption -> pick time -> schedule.
"""
import json, sys, threading, webbrowser, urllib.request, urllib.error
import re, shutil, subprocess, tempfile, urllib.parse
import time, os, hmac, hashlib
from datetime import datetime, timezone, timedelta
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

API = "https://api.buffer.com"
UA = "Mozilla/5.0 ReelSchedulerUI/1.0"
LIMIT = 1000  # Buffer posts per month
USAGE_FILE = Path(__file__).resolve().parent / "usage.json"
QUEUE_FILE = Path(__file__).resolve().parent / "queue.json"
ST = {"key": "", "orgs": {}, "log": ""}  # key + channel->org map (filled on connect)
LOCK = threading.Lock()
TICK = threading.Lock()

# ---- admin login (set ADMIN_PASSWORD in Render env vars; 9889 is only the fallback) ----
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "9889")
COVER_DUR = 0.05  # cover frame length in seconds, fixed
_SECRET = os.environ.get("SECRET_KEY", "reel-scheduler::" + ADMIN_PASSWORD).encode()
AUTH_TOKEN = hmac.new(_SECRET, b"admin", hashlib.sha256).hexdigest()
FAILED = {}  # ip -> (count, first_fail_time)

LOGIN = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Admin login</title>
<style>:root{color-scheme:light dark}*{box-sizing:border-box}
body{margin:0;min-height:100vh;display:grid;place-items:center;background:#f5f5f7;color:#1d1d1f;font:17px -apple-system,BlinkMacSystemFont,"Inter","Segoe UI",Roboto,sans-serif}
@media(prefers-color-scheme:dark){body{background:#000;color:#f5f5f7}form{background:#1c1c1e!important}input{background:#2c2c2e!important;color:#f5f5f7!important}}
form{background:#fff;padding:36px;border-radius:28px;width:min(92vw,360px);text-align:center}
h1{font-size:26px;margin:0 0 6px}p{color:#6e6e73;font-size:15px;margin:0 0 22px}
input{width:100%;font:inherit;padding:14px 16px;border:0;border-radius:14px;background:#f5f5f7;outline:0;text-align:center;letter-spacing:.3em}
button{margin-top:16px;width:100%;height:50px;border:0;border-radius:99px;background:#0071e3;color:#fff;font:inherit;font-weight:500;cursor:pointer}
.e{color:#ff3b30;font-size:14px;margin-top:14px;min-height:18px}</style></head><body>
<form method="post" action="/login"><h1>Reel Scheduler</h1><p>Admin access only</p>
<input name="password" type="password" inputmode="numeric" placeholder="Password" autofocus autocomplete="current-password">
<button type="submit">Unlock</button><div class="e">%ERR%</div></form></body></html>"""


class GqlError(Exception):
    pass


class MutErr(Exception):
    pass


def gql(key, query, variables=None):
    req = urllib.request.Request(
        API,
        data=json.dumps({"query": query, "variables": variables or {}}).encode(),
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json", "User-Agent": UA},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            data = json.load(r)
    except urllib.error.HTTPError as e:
        raise GqlError(f"Buffer HTTP {e.code}: {e.read().decode('utf-8', 'ignore')[:300]}")
    except urllib.error.URLError as e:
        raise GqlError(f"Network error: {e.reason}")
    if data.get("errors"):
        raise GqlError("; ".join(x.get("message", "?") for x in data["errors"]))
    return data["data"]


ORGS = "query { account { organizations { id name } } }"
CHANNELS = """query($organizationId: OrganizationId!) {
  channels(input: { organizationId: $organizationId }) { id name displayName service avatar } }"""
MUTATION = """mutation CreatePost($input: CreatePostInput!) {
  createPost(input: $input) {
    ... on PostActionSuccess { post { id dueAt } }
    ... on MutationError { message } } }"""


def _month():
    return datetime.now().strftime("%Y-%m")


def usage(d=None):
    """Local counter of posts sent through this tool this month (resets each month)."""
    try:
        u = json.loads(USAGE_FILE.read_text())
    except Exception:
        u = {}
    if d and "set" in d:
        u[_month()] = max(0, int(d["set"]))
        USAGE_FILE.write_text(json.dumps(u))
    return {"used": u.get(_month(), 0), "limit": LIMIT}


def bump():
    u = usage()["used"]
    usage({"set": u + 1})


def folder(d):
    """List .mp4/.mov files of a public GitHub folder as raw links."""
    m = re.match(r"https?://github\.com/([^/]+)/([^/#?]+?)(?:\.git)?(?:/tree/([^/]+)(?:/([^#?]*))?)?/?$", d["url"].strip())
    if not m:
        raise GqlError("Paste a GitHub folder link like github.com/user/repo/tree/main/reels")
    o, r, br, path = m.groups()
    api = f"https://api.github.com/repos/{o}/{r}/contents/{urllib.parse.quote((path or '').strip('/'))}"
    if br:
        api += f"?ref={br}"
    try:
        hdr = {"User-Agent": UA, "Accept": "application/vnd.github+json"}
        if os.environ.get("GITHUB_TOKEN"):  # lifts the 60/hour limit that shared Render IPs hit
            hdr["Authorization"] = "Bearer " + os.environ["GITHUB_TOKEN"].strip()
        req = urllib.request.Request(api, headers=hdr)
        with urllib.request.urlopen(req, timeout=30) as x:
            items = json.load(x)
    except urllib.error.HTTPError as e:
        raise GqlError(f"GitHub HTTP {e.code}: " + ("rate limit hit, add GITHUB_TOKEN in Render env" if e.code == 403 else "folder not found or repo is private" if e.code == 404 else "request failed"))
    except urllib.error.URLError as e:
        raise GqlError(f"Network error: {e.reason}")
    if not isinstance(items, list):
        raise GqlError("That is a file link, paste the folder link.")
    urls = sorted(i["download_url"] for i in items if i.get("type") == "file" and i.get("download_url")
                  and i["name"].lower().endswith((".mp4", ".mov")))
    return {"urls": urls}


def connect(d):
    key = d["key"].strip()
    out = []
    for org in gql(key, ORGS)["account"]["organizations"]:
        ch = gql(key, CHANNELS, {"organizationId": org["id"]})["channels"]
        out.append({"org": org, "channels": ch})
        for c in ch:
            ST["orgs"][c["id"]] = org["id"]
    ST["key"] = key
    return {"groups": out}


def schedule(d):
    key = d["key"].strip()
    base = {"text": d.get("caption", ""), "channelId": d["channelId"],
            "schedulingType": "automatic", "mode": d["mode"]}
    if d["mode"] == "customScheduled":
        base["dueAt"] = d["dueAt"]
    if d.get("service") == "instagram":
        base["metadata"] = {"instagram": {"type": "reel", "shouldShareToFeed": True}}
    video = {"url": d["videoUrl"]}
    if d.get("thumbOffset") is not None:  # Buffer rejects thumbnailUrl; it only takes a frame offset in ms
        video["metadata"] = {"thumbnailOffset": int(d["thumbOffset"])}

    def send(assets):
        res = gql(key, MUTATION, {"input": {**base, "assets": assets}})["createPost"]
        if "message" in res:
            raise MutErr(res["message"])
        return res["post"]

    try:
        post = send([{"video": video}])
    except GqlError as first:  # older schema shape fallback
        try:
            post = send({"videos": [video]})
        except GqlError:
            raise first
    bump()
    return {"post": post, **usage()}


# ---- cover editor: your cover image is placed at the very start of each video for a split second ----
HOME = Path.home()
OUT_DIR = (HOME / "storage" / "movies" / "EditedReels") if (HOME / "storage" / "movies").exists() \
    else Path(__file__).resolve().parent / "EditedReels"
WORK = Path(tempfile.gettempdir()) / "reel_ui_work"
WORK.mkdir(exist_ok=True)


def info(d=None):
    return {"out": str(OUT_DIR), "ffmpeg": bool(shutil.which("ffmpeg") and shutil.which("ffprobe")),
            "cover": any(WORK.glob("cover.*"))}


def _save(rfile, n, path):
    with open(path, "wb") as f:
        while n > 0:
            b = rfile.read(min(1 << 20, n))
            if not b:
                break
            f.write(b)
            n -= len(b)


def cover(rfile, n, name, dur):
    for old in WORK.glob("cover.*"):
        old.unlink()
    _save(rfile, n, WORK / ("cover" + (Path(name).suffix.lower() or ".jpg")))
    return {}


def _probe(p):
    r = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", str(p)],
                       capture_output=True, text=True)
    if r.returncode:
        raise GqlError("Cannot read video: " + r.stderr[-200:])
    ss = json.loads(r.stdout).get("streams", [])
    v = next((s for s in ss if s.get("codec_type") == "video"), None)
    if not v:
        raise GqlError("No video stream found")
    w, h = int(v["width"]), int(v["height"])
    rot = int(float(v.get("tags", {}).get("rotate", 0) or 0))
    for sd in v.get("side_data_list") or []:
        if "rotation" in sd:
            rot = int(float(sd["rotation"]))
    if abs(rot) % 180 == 90:
        w, h = h, w
    try:
        a, b = v.get("r_frame_rate", "30/1").split("/")
        fps = float(a) / float(b)
    except Exception:
        fps = 30.0
    fps = fps if 1 <= fps <= 120 else 30.0
    return w - w % 2, h - h % 2, round(fps, 3), any(s.get("codec_type") == "audio" for s in ss)


def edit(rfile, n, name, dur):
    if not info()["ffmpeg"]:
        raise GqlError("ffmpeg missing. In Termux run: pkg install -y ffmpeg")
    cov = next(WORK.glob("cover.*"), None)
    if not cov:
        raise GqlError("Choose a cover image first.")
    stem = re.sub(r"[^A-Za-z0-9._-]", "_", Path(name).stem)[:80] or "reel"
    src = WORK / ("in_" + stem + (Path(name).suffix or ".mp4"))
    _save(rfile, n, src)
    try:
        w, h, fps, has_a = _probe(src)
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        out = OUT_DIR / (stem + ".mp4")
        vf = f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},setsar=1,fps={fps},format=yuv420p"
        norm = f"scale={w}:{h},setsar=1,fps={fps},format=yuv420p"
        cmd = ["ffmpeg", "-nostdin", "-y", "-loglevel", "error", "-loop", "1", "-framerate", str(fps),
               "-t", str(dur), "-i", str(cov)]
        if has_a:
            cmd += ["-f", "lavfi", "-t", str(dur), "-i", "anullsrc=r=44100:cl=stereo", "-i", str(src)]
            fc = (f"[0:v]{vf}[c];[2:v]{norm}[v];[1:a]aformat=sample_rates=44100:channel_layouts=stereo[ca];"
                  "[2:a]aresample=44100,aformat=channel_layouts=stereo[a];[c][ca][v][a]concat=n=2:v=1:a=1[ov][oa]")
            maps = ["-map", "[ov]", "-map", "[oa]", "-c:a", "aac", "-b:a", "128k"]
        else:
            cmd += ["-i", str(src)]
            fc = f"[0:v]{vf}[c];[1:v]{norm}[v];[c][v]concat=n=2:v=1:a=0[ov]"
            maps = ["-map", "[ov]", "-an"]
        cmd += ["-filter_complex", fc, *maps, "-c:v", "libx264", "-preset", "veryfast", "-crf", "22",
                "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(out)]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=1200)
        if r.returncode:
            raise GqlError("ffmpeg failed: " + r.stderr[-250:])
    finally:
        src.unlink(missing_ok=True)
    if shutil.which("termux-media-scan"):
        subprocess.run(["termux-media-scan", str(out)], capture_output=True)
    return {"out": str(out), "name": out.name}


# ---- drip queue: Buffer allows only N scheduled posts per channel, so we feed it gradually ----
def qload():
    try:
        q = json.loads(QUEUE_FILE.read_text())
    except Exception:
        q = {}
    return {"items": q.get("items", []), "sent": q.get("sent", 0), "missed": q.get("missed", 0),
            "failed": q.get("failed", 0), "cap": q.get("cap", 10)}


def qadd(d):
    ST["key"] = d["key"].strip() or ST["key"]
    with LOCK:
        q = qload()
        have = {(i["channelId"], i["dueAt"], i["videoUrl"]) for i in q["items"]}
        q["items"] += [i for i in d["items"] if (i["channelId"], i["dueAt"], i["videoUrl"]) not in have]
        q["items"].sort(key=lambda i: i["dueAt"])
        q["cap"] = max(1, int(d.get("cap") or 10))
        QUEUE_FILE.write_text(json.dumps(q))
    threading.Thread(target=tick, daemon=True).start()
    return qstatus()


def qclear(d=None):
    with LOCK:
        q = qload()
        q["items"] = []
        QUEUE_FILE.write_text(json.dumps(q))
    return qstatus()


def qstatus(d=None):
    q = qload()
    return {"pending": len(q["items"]), "sent": q["sent"], "missed": q["missed"], "failed": q["failed"],
            "next": q["items"][0]["dueAt"] if q["items"] else None, "log": ST["log"], "cap": q["cap"]}


def qdone(it, kind):
    with LOCK:
        q = qload()
        q["items"] = [i for i in q["items"] if not (i["channelId"] == it["channelId"] and i["dueAt"] == it["dueAt"]
                                                    and i["videoUrl"] == it["videoUrl"])]
        q[kind] += 1
        QUEUE_FILE.write_text(json.dumps(q))


COUNT = """query($o: OrganizationId!, $c: [ChannelId!]) {
  posts(first: 100, input: { organizationId: $o, filter: { status: [scheduled], channelIds: $c } }) { edges { node { id } } } }"""
FAILS = {}


def tick():
    if not ST["key"] or not TICK.acquire(blocking=False):
        return
    try:
        q = qload()
        for ch in sorted({i["channelId"] for i in q["items"]}):
            org = ST["orgs"].get(ch)
            if not org:
                continue
            try:
                free = q["cap"] - len(gql(ST["key"], COUNT, {"o": org, "c": [ch]})["posts"]["edges"])
            except GqlError as e:
                ST["log"] = str(e)[:200]
                continue
            for it in [i for i in q["items"] if i["channelId"] == ch]:
                if free <= 0:
                    break
                due = datetime.fromisoformat(it["dueAt"].replace("Z", "+00:00"))
                if due < datetime.now(timezone.utc) + timedelta(minutes=2):
                    qdone(it, "missed")
                    continue
                k = (ch, it["dueAt"], it["videoUrl"])
                try:
                    schedule({"key": ST["key"], "channelId": ch, "service": it.get("service"), "mode": "customScheduled",
                              "dueAt": it["dueAt"], "videoUrl": it["videoUrl"], "thumbOffset": it.get("thumbOffset"), "caption": it.get("caption", "")})
                    qdone(it, "sent")
                    ST["log"] = ""
                    free -= 1
                except (GqlError, MutErr) as e:
                    ST["log"] = str(e)[:200]
                    FAILS[k] = FAILS.get(k, 0) + 1
                    if FAILS[k] >= 3 and "limit" not in str(e).lower():
                        qdone(it, "failed")  # bad link etc: stop blocking the account
                    else:
                        break
    finally:
        TICK.release()


def worker():
    while True:
        time.sleep(45)
        try:
            tick()
        except Exception as e:
            ST["log"] = str(e)[:200]


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        b = body if isinstance(body, bytes) else body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _ip(self):
        return (self.headers.get("X-Forwarded-For") or self.client_address[0]).split(",")[0].strip()

    def _authed(self):
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == "rs_auth" and hmac.compare_digest(v, AUTH_TOKEN):
                return True
        return False

    def _redirect(self, to, cookie=None):
        self.send_response(303)
        self.send_header("Location", to)
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _login(self):
        ip = self._ip()
        cnt, t0 = FAILED.get(ip, (0, 0))
        if cnt >= 5 and time.time() - t0 < 300:
            return self._send(429, LOGIN.replace("%ERR%", "Too many attempts. Try again in 5 minutes."), "text/html")
        n = int(self.headers.get("Content-Length", 0))
        pw = urllib.parse.parse_qs(self.rfile.read(min(n, 1024)).decode("utf-8", "ignore")).get("password", [""])[0]
        if hmac.compare_digest(pw.encode(), ADMIN_PASSWORD.encode()):
            FAILED.pop(ip, None)
            secure = "; Secure" if self.headers.get("X-Forwarded-Proto") == "https" else ""
            return self._redirect("/", f"rs_auth={AUTH_TOKEN}; HttpOnly; SameSite=Lax; Path=/; Max-Age=2592000{secure}")
        FAILED[ip] = (cnt + 1, t0 if cnt else time.time())
        self._send(401, LOGIN.replace("%ERR%", "Wrong password."), "text/html")

    def _file(self, name):
        p = OUT_DIR / Path(urllib.parse.unquote(name)).name
        if not p.is_file():
            return self._send(404, "{}")
        self.send_response(200)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Content-Length", str(p.stat().st_size))
        self.send_header("Content-Disposition", f'attachment; filename="{p.name}"')
        self.end_headers()
        with open(p, "rb") as f:
            shutil.copyfileobj(f, self.wfile, 1 << 20)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/logout":
            return self._redirect("/", "rs_auth=; HttpOnly; Path=/; Max-Age=0")
        if not self._authed():
            return self._send(200, LOGIN.replace("%ERR%", ""), "text/html")
        if path in ("/", "/index.html"):
            return self._send(200, PAGE, "text/html")
        if path.startswith("/out/"):
            return self._file(path[5:])
        self._send(404, "{}")

    def do_POST(self):
        if self.path == "/login":
            return self._login()
        if not self._authed():
            return self._send(401, json.dumps({"ok": False, "error": "Login required"}))
        try:
            if self.path in ("/api/cover", "/api/edit"):  # raw file uploads
                fn = cover if self.path == "/api/cover" else edit
                res = fn(self.rfile, int(self.headers.get("Content-Length", 0)),
                         urllib.parse.unquote(self.headers.get("X-Name", "x")), COVER_DUR)
                return self._send(200, json.dumps({"ok": True, **res}))
            d = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            fn = {"/api/connect": connect, "/api/schedule": schedule, "/api/usage": usage, "/api/folder": folder,
                  "/api/qadd": qadd, "/api/qstatus": qstatus, "/api/qclear": qclear, "/api/info": info}[self.path]
            self._send(200, json.dumps({"ok": True, **fn(d)}))
        except (GqlError, MutErr) as e:
            self._send(200, json.dumps({"ok": False, "error": str(e)}))
        except Exception as e:
            self._send(200, json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"}))


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Reel Scheduler</title>
<style>
:root{--bg:#f5f5f7;--card:#fff;--field:#f5f5f7;--ink:#1d1d1f;--mute:#6e6e73;--line:rgba(0,0,0,.08);--blue:#0071e3;--green:#34c759;--red:#ff3b30;--nav:rgba(245,245,247,.72)}
@media(prefers-color-scheme:dark){:root{--bg:#000;--card:#1c1c1e;--field:#2c2c2e;--ink:#f5f5f7;--mute:#98989d;--line:rgba(255,255,255,.12);--blue:#0a84ff;--nav:rgba(0,0,0,.72)}}
*{box-sizing:border-box;margin:0}
body{background:var(--bg);color:var(--ink);font:17px/1.47 -apple-system,BlinkMacSystemFont,"SF Pro Display","SF Pro Text","Inter","Segoe UI",Roboto,sans-serif;-webkit-font-smoothing:antialiased}
nav{position:sticky;top:0;z-index:20;height:52px;display:flex;align-items:center;justify-content:space-between;padding:0 max(22px,calc(50% - 540px));background:var(--nav);backdrop-filter:saturate(180%) blur(20px);-webkit-backdrop-filter:saturate(180%) blur(20px);border-bottom:1px solid var(--line)}
nav b{font-weight:600;letter-spacing:-.01em}
.pill{font-size:13px;padding:4px 12px;border-radius:99px;background:var(--field);color:var(--mute);display:flex;gap:7px;align-items:center;transition:.4s}
.pill i{width:8px;height:8px;border-radius:50%;background:#aaa;transition:.4s}.pill.on i{background:var(--green);box-shadow:0 0 0 4px rgba(52,199,89,.2)}.pill.on{color:var(--ink)}
.bar{width:44px;height:5px;border-radius:9px;background:var(--line);overflow:hidden}.bar u{display:block;height:100%;width:0;background:var(--green);border-radius:9px;transition:width .8s cubic-bezier(.2,.8,.2,1),background .4s}
.acs{display:flex;flex-wrap:wrap;gap:8px}.ac{border:1.5px solid var(--line);background:0;color:var(--ink);font:inherit;font-size:14px;padding:8px 16px;border-radius:99px;cursor:pointer;transition:.25s}.ac.on{background:var(--blue);border-color:var(--blue);color:#fff}
.slots{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:10px}.g4{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:12px}
.pb{height:6px;border-radius:9px;background:var(--field);overflow:hidden;margin-top:22px}.pb u{display:block;height:100%;width:0;background:var(--blue);transition:width .4s}
.ghost{background:var(--field);color:var(--ink);margin-top:10px;width:100%}:root{color-scheme:light dark}
input[type=file]{padding:10px 12px}input[type=file]::file-selector-button{margin-right:12px;border:0;border-radius:99px;background:var(--blue);color:#fff;padding:8px 16px;font:inherit;font-size:14px;cursor:pointer}
.hero{text-align:center;padding:84px 22px 56px;animation:rise 1s cubic-bezier(.2,.8,.2,1) both}
.hero h1{font-size:clamp(40px,7.4vw,84px);line-height:1.05;letter-spacing:-.035em;font-weight:700}
.hero p{font-size:clamp(19px,2.4vw,26px);color:var(--mute);margin-top:16px;letter-spacing:-.01em}
@keyframes rise{from{opacity:0;transform:translateY(28px)}to{opacity:1;transform:none}}
.wrap{max-width:1080px;margin:0 auto;padding:0 22px 120px;display:grid;grid-template-columns:minmax(0,1fr) 300px;gap:28px;align-items:start}main{min-width:0}
.card{background:var(--card);border-radius:28px;padding:34px;margin-bottom:20px;transition:opacity .6s,transform .6s cubic-bezier(.2,.8,.2,1)}
.card.lock{opacity:.35;pointer-events:none;transform:scale(.985)}
h2{font-size:28px;letter-spacing:-.02em;font-weight:600}.sub{color:var(--mute);font-size:15px;margin:4px 0 22px}
label{display:block;font-size:13px;color:var(--mute);margin:18px 0 7px;font-weight:500}
input,textarea{width:100%;font:inherit;font-size:16px;color:var(--ink);background:var(--field);border:1px solid transparent;border-radius:14px;padding:14px 16px;outline:0;transition:.2s}
textarea{resize:vertical;min-height:120px}input:focus,textarea:focus{border-color:var(--blue);box-shadow:0 0 0 4px rgba(0,113,227,.18)}
.row{display:flex;gap:12px}.row input{flex:1}
.btn{border:0;border-radius:99px;background:var(--blue);color:#fff;font:inherit;font-size:16px;font-weight:500;padding:0 26px;height:50px;cursor:pointer;transition:.2s;white-space:nowrap}
.btn:hover{filter:brightness(1.08)}.btn:active{transform:scale(.97)}.btn[disabled]{opacity:.6;cursor:wait}.btn.ok{background:var(--green)}
.btn.big{width:100%;height:56px;font-size:18px;margin-top:26px}
.chs{display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:12px}
.ch{border:1.5px solid var(--line);border-radius:20px;padding:16px;cursor:pointer;transition:.25s;display:flex;gap:12px;align-items:center}
.ch:hover{transform:translateY(-2px)}.ch.sel{border-color:var(--blue);box-shadow:0 0 0 4px rgba(0,113,227,.15)}
.av{width:46px;height:46px;border-radius:50%;background:linear-gradient(135deg,#f9ce34,#ee2a7b,#6228d7);color:#fff;display:grid;place-items:center;font-weight:600;overflow:hidden;flex:none}.av img{width:100%;height:100%;object-fit:cover}
.ch div{min-width:0}.ch b{display:block;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.ch small{color:var(--mute);font-size:13px;text-transform:capitalize}
.cid{margin-top:4px;font-size:12px;color:var(--blue);background:0;border:0;padding:0;cursor:pointer;font-family:ui-monospace,Menlo,monospace;max-width:100%;overflow:hidden;text-overflow:ellipsis;display:block}
.seg{display:flex;background:var(--field);border-radius:12px;padding:3px;margin-bottom:26px}
.seg button{flex:1;border:0;background:0;font:inherit;font-size:14px;font-weight:500;color:var(--mute);height:36px;border-radius:9px;cursor:pointer;transition:.25s}
.seg button.on{background:var(--card);color:var(--ink);box-shadow:0 1px 4px rgba(0,0,0,.18)}
#when.off{display:none}
.digi{text-align:center;margin-bottom:6px}.digi .t{font-size:clamp(54px,9vw,88px);font-weight:200;letter-spacing:-.04em;font-variant-numeric:tabular-nums;line-height:1}.digi .d{color:var(--mute);margin-top:6px}
.days{max-width:100%;display:flex;gap:10px;overflow-x:auto;padding:14px 2px;scrollbar-width:none}.days::-webkit-scrollbar{display:none}
.day{flex:none;width:62px;padding:10px 0;border-radius:18px;border:0;background:var(--field);color:var(--ink);font:inherit;cursor:pointer;transition:.25s}
.day small{display:block;font-size:12px;color:var(--mute)}.day b{font-size:22px;font-weight:600}
.day.on{background:var(--blue);color:#fff;transform:scale(1.05)}.day.on small{color:rgba(255,255,255,.8)}
.clock{position:relative;display:flex;justify-content:center;gap:6px;height:220px;margin:6px 0}
.clock:before{content:"";position:absolute;left:50%;transform:translateX(-50%);width:min(100%,300px);top:88px;height:44px;border-radius:12px;background:var(--field)}
.w{position:relative;width:84px;overflow-y:scroll;scroll-snap-type:y mandatory;padding:88px 0;scrollbar-width:none;-webkit-mask-image:linear-gradient(#0000,#000 32%,#000 68%,#0000);mask-image:linear-gradient(#0000,#000 32%,#000 68%,#0000)}
.w::-webkit-scrollbar{display:none}.w div{height:44px;line-height:44px;text-align:center;scroll-snap-align:center;font-size:24px;color:var(--mute);font-variant-numeric:tabular-nums;cursor:pointer;transition:.2s}.w div.on{color:var(--ink);font-weight:600}
.quick{display:flex;gap:8px;flex-wrap:wrap;justify-content:center;margin-top:10px}
.quick button{border:1px solid var(--line);background:0;color:var(--ink);font:inherit;font-size:14px;padding:6px 14px;border-radius:99px;cursor:pointer;transition:.2s}.quick button:hover{background:var(--field)}
.cnt{float:right;font-size:12px;color:var(--mute)}
aside{position:sticky;top:76px;margin-top:0}
.phone{width:290px;aspect-ratio:9/18.6;border-radius:44px;border:9px solid #1d1d1f;background:#111;position:relative;overflow:hidden;box-shadow:0 30px 60px rgba(0,0,0,.28)}
.phone .bg{position:absolute;inset:0;background:linear-gradient(160deg,#3a3a3c,#0b0b0c);display:grid;place-items:center;color:#8e8e93;font-size:14px}
.phone img,.phone video{position:absolute;inset:0;width:100%;height:100%;object-fit:cover}
.phone .ov{position:absolute;inset:auto 0 0;padding:70px 16px 22px;background:linear-gradient(#0000,rgba(0,0,0,.82));color:#fff;font-size:13px}
.phone .ov b{display:block;margin-bottom:4px}.phone .ov p{display:-webkit-box;-webkit-line-clamp:4;-webkit-box-orient:vertical;overflow:hidden;white-space:pre-wrap;opacity:.95}
.phone .notch{position:absolute;top:8px;left:50%;transform:translateX(-50%);width:84px;height:22px;border-radius:99px;background:#000;z-index:3}
.chip{width:290px;text-align:center;margin-top:16px;color:var(--mute);font-size:14px}
#toast{position:fixed;left:50%;bottom:30px;transform:translate(-50%,120px);background:rgba(30,30,32,.92);color:#fff;padding:13px 22px;border-radius:99px;font-size:15px;max-width:92vw;backdrop-filter:blur(20px);transition:transform .5s cubic-bezier(.2,.8,.2,1);z-index:50}
#toast.show{transform:translate(-50%,0)}#toast.err{background:rgba(255,59,48,.95)}
@media(max-width:900px){.wrap{grid-template-columns:minmax(0,1fr)}aside{display:none}.card{padding:26px}}
@media(prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}
</style></head><body>
<nav><b>Reels</b><span style="display:flex;gap:10px;align-items:center"><a href="/logout" style="color:var(--mute);font-size:13px;text-decoration:none">Logout</a><span class="pill" id="lim" title="Tap to correct the count" style="cursor:pointer"><span id="lt">0 / 1000</span><span class="bar"><u id="lb"></u></span></span><span class="pill" id="pill"><i></i><span id="pt">Not connected</span></span></span></nav>
<header class="hero"><h1>Schedule reels.<br>Beautifully simple.</h1><p>Connect Buffer, pick a time, done.</p></header>
<div class="wrap"><main>

<section class="card" id="s0"><h2>Prepare reels</h2><p class="sub">Puts your cover image at the very start of every video for a split second, so it becomes the thumbnail. Edited reels are saved to a folder on your device.</p>
<label>Cover image</label><input id="cvf" type="file" accept="image/*">
<label>Cover duration (seconds, fixed)</label><input id="cdur" type="number" step="0.01" value="0.05" readonly>
<label>Videos</label><input id="vf" type="file" accept="video/*" multiple>
<div class="pb"><u id="pp"></u></div><p class="sub" id="pnote" style="margin:12px 0 0"></p>
<button class="btn big" id="prep">Add cover to videos</button></section>

<section class="card" id="s1"><h2>Connect Buffer</h2><p class="sub">Paste your API key from Buffer, Settings, API. It stays on this computer.</p>
<div class="row"><input id="key" type="password" placeholder="Buffer API key" autocomplete="off"><button class="btn" id="cbtn">Connect</button></div></section>

<section class="card lock" id="s2"><h2>Choose a channel</h2><p class="sub">Tap a card to select it. Tap the ID to copy it.</p><div class="chs" id="chs"></div></section>

<section class="card lock" id="s5"><h2>Auto-plan all accounts</h2><p class="sub">Reels rotate per account with no repeats until the folder runs out. Buffer holds only a few scheduled posts per account, so extra reels wait here and are sent as space frees up. Keep this running.</p>
<label>Accounts</label><div class="acs" id="acs"></div>
<label>GitHub folder link</label><div class="row"><input id="fu" placeholder="https://github.com/user/repo/tree/main/reels"><button class="btn" id="fl">Load</button></div>
<label>Reel links <span class="cnt" id="uc">0 reels</span></label><textarea id="urls" placeholder="One public .mp4 link per line (auto-filled by Load). Optional custom frame: link | 2.5"></textarea>
<label>Caption for every reel</label><textarea id="acap" placeholder="Fixed caption..."></textarea>
<label>Daily slots</label><div class="slots" id="slots"></div>
<div class="g4"><div><label>Reels per slot</label><input id="per" type="number" min="1" value="6"></div><div><label>Gap (minutes)</label><input id="gap" type="number" min="1" value="2"></div><div><label>Start date</label><input id="sd" type="date"></div><div><label>Days</label><input id="dys" type="number" min="1" value="1"></div><div><label>Buffer limit per account</label><input id="cap" type="number" min="1" value="10"></div><div><label>Thumbnail frame (sec)</label><input id="tof" type="number" min="0" step="0.01" value="0.05" placeholder="default"></div></div>
<p class="sub" id="sum" style="margin:20px 0 0"></p><div class="pb"><u id="pg"></u></div><p class="sub" id="qs" style="margin:12px 0 0"></p>
<button class="btn big" id="run">Schedule all</button><button class="btn ghost" id="stop" style="display:none">Clear waiting queue</button></section>

<section class="card lock" id="s3"><h2>Your reel</h2><p class="sub">Buffer needs public direct links (Cloudinary, S3, GitHub raw).</p>
<label>Video URL (.mp4)</label><input id="video" placeholder="https://example.com/reel.mp4">
<label>Thumbnail frame (seconds into the video)</label><input id="thumb" type="number" min="0" step="0.01" value="0.05" placeholder="default">
<label>Caption <span class="cnt" id="cnt">0 / 2200</span></label><textarea id="cap" maxlength="2200" placeholder="Write a caption..."></textarea></section>

<section class="card lock" id="s4"><h2>Set the time</h2><p class="sub">Uses your computer's time zone.</p>
<div class="seg" id="seg"><button data-m="customScheduled" class="on">Schedule</button><button data-m="addToQueue">Add to queue</button><button data-m="shareNow">Publish now</button></div>
<div id="when"><div class="digi"><div class="t" id="dt">09:00 PM</div><div class="d" id="dd"></div></div>
<div class="days" id="days"></div>
<div class="clock"><div class="w" id="wh"></div><div class="w" id="wm"></div><div class="w" id="wa"></div></div>
<div class="quick" id="quick"><button data-t="9:0">9:00 AM</button><button data-t="18:0">6:00 PM</button><button data-t="21:0">9:00 PM</button><button data-t="+60">In 1 hour</button></div></div>
<button class="btn big" id="go">Schedule reel</button></section>

</main><aside><div class="phone"><div class="notch"></div><div class="bg" id="pbg">Preview</div><div class="ov"><b id="pn">@channel</b><p id="pc">Your caption appears here.</p></div></div><div class="chip" id="pw"></div></aside></div>
<div id="toast"></div>
<script>
const $=s=>document.querySelector(s),S={key:localStorage.getItem('bk')||'',ch:null,mode:'customScheduled',date:null,h:9,m:0,ap:'PM'};
const api=async(p,b)=>{const r=await fetch(p,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b)});if(r.status==401)return location.reload();const j=await r.json();if(!j.ok)throw new Error(j.error);return j};
let tt;const toast=(m,e)=>{const t=$('#toast');t.textContent=m;t.className='show'+(e?' err':'');clearTimeout(tt);tt=setTimeout(()=>t.className='',e?5500:3200)};
const pad=n=>String(n).padStart(2,'0'),unlock=id=>$(id).classList.remove('lock');

// connect + channels
async function connect(){const b=$('#cbtn');S.key=$('#key').value.trim();if(!S.key)return toast('Paste your API key first.',1);b.disabled=1;b.textContent='Connecting...';
 try{const j=await api('/api/connect',{key:S.key});localStorage.setItem('bk',S.key);const all=j.groups.flatMap(g=>g.channels);S.all=all;
  $('#chs').innerHTML=all.map((c,i)=>`<div class="ch" data-i="${i}"><div class="av">${c.avatar?`<img src="${c.avatar}" onerror="this.remove()">`:''}${(c.displayName||c.name||'?')[0].toUpperCase()}</div><div><b>${c.displayName||c.name}</b><small>${c.service}</small><button class="cid" title="Copy ID">${c.id}</button></div></div>`).join('')||'<p class="sub">No channels found.</p>';
  document.querySelectorAll('.ch').forEach(el=>{const c=all[el.dataset.i];el.onclick=()=>{S.ch=c;document.querySelectorAll('.ch').forEach(x=>x.classList.toggle('sel',x===el));$('#pn').textContent='@'+(c.displayName||c.name);['#s3','#s4'].forEach(unlock);upd()};
   el.querySelector('.cid').onclick=e=>{e.stopPropagation();navigator.clipboard.writeText(c.id).then(()=>toast('Channel ID copied'))}});
  $('#pill').classList.add('on');$('#pt').textContent='Connected';unlock('#s2');unlock('#s5');$('#acs').innerHTML=all.map((c,i)=>`<button class="ac${c.service=='instagram'?' on':''}" data-i="${i}">${c.displayName||c.name}</button>`).join('');sum();
  if(all.length==1)document.querySelector('.ch').click();$('#s2').scrollIntoView({behavior:'smooth',block:'center'});
 }catch(e){toast(e.message,1)}b.disabled=0;b.textContent='Connect'}
$('#cbtn').onclick=connect;$('#key').value=S.key;$('#key').onkeydown=e=>e.key=='Enter'&&connect();if(S.key)connect();

// wheels
function wheel(el,items,get,set){el.innerHTML=items.map(t=>`<div>${t}</div>`).join('');let cur=-1;
 const mark=i=>{if(i==cur||i<0||i>=items.length)return;cur=i;[...el.children].forEach((c,k)=>c.classList.toggle('on',k==i));set(i)};
 el.onscroll=()=>mark(Math.round(el.scrollTop/44));el.onclick=e=>{const i=[...el.children].indexOf(e.target);if(i>-1)el.scrollTo({top:i*44,behavior:'smooth'})};
 return i=>{el.scrollTo({top:i*44,behavior:'smooth'});mark(i)}}
const W={};
W.h=wheel($('#wh'),[...Array(12)].map((_,i)=>pad(i+1)),0,i=>{S.h=i+1;upd()});
W.m=wheel($('#wm'),[...Array(60)].map((_,i)=>pad(i)),0,i=>{S.m=i;upd()});
W.a=wheel($('#wa'),['AM','PM'],0,i=>{S.ap=i?'PM':'AM';upd()});

// days
const D0=new Date();D0.setHours(0,0,0,0);
$('#days').innerHTML=[...Array(21)].map((_,i)=>{const d=new Date(D0);d.setDate(d.getDate()+i);return `<button class="day" data-i="${i}"><small>${i?d.toLocaleDateString('en',{weekday:'short'}):'Today'}</small><b>${d.getDate()}</b></button>`}).join('');
document.querySelectorAll('.day').forEach(b=>b.onclick=()=>{setDay(+b.dataset.i);upd()});
function setDay(i){S.date=new Date(D0);S.date.setDate(D0.getDate()+i);document.querySelectorAll('.day').forEach((x,k)=>x.classList.toggle('on',k==i))}
function setTime(d){const h=d.getHours();S.ap=h>=12?'PM':'AM';S.h=h%12||12;S.m=d.getMinutes();W.h(S.h-1);W.m(S.m);W.a(S.ap=='PM'?1:0);setDay(Math.round((new Date(d).setHours(0,0,0,0)-D0)/864e5))}
const when=()=>{let h=S.h%12+(S.ap=='PM'?12:0);const d=new Date(S.date);d.setHours(h,S.m,0,0);return d};
const fmtD=d=>d.toLocaleDateString('en',{weekday:'long',day:'numeric',month:'long'});
function upd(){const d=when(),now=S.mode!='customScheduled';$('#dt').textContent=`${pad(S.h)}:${pad(S.m)} ${S.ap}`;$('#dd').textContent=fmtD(d);
 $('#pw').textContent=S.mode=='shareNow'?'Publishes immediately':S.mode=='addToQueue'?'Goes to your Buffer queue':fmtD(d)+', '+$('#dt').textContent;
 $('#go').textContent={customScheduled:'Schedule reel',addToQueue:'Add to queue',shareNow:'Publish now'}[S.mode]}
$('#seg').onclick=e=>{const m=e.target.dataset.m;if(!m)return;S.mode=m;document.querySelectorAll('#seg button').forEach(b=>b.classList.toggle('on',b===e.target));$('#when').classList.toggle('off',m!='customScheduled');upd()};
$('#quick').onclick=e=>{const t=e.target.dataset.t;if(!t)return;let d=new Date();if(t[0]=='+')d=new Date(Date.now()+t.slice(1)*6e4);else{const[h,m]=t.split(':');d.setHours(+h,+m,0,0);if(d<Date.now())d.setDate(d.getDate()+1)}setTime(d)};
{const d=new Date(Date.now()+15*6e4);d.setMinutes(Math.ceil(d.getMinutes()/5)*5);setTime(d);setTimeout(upd,50)}

// preview
function prev(){const v=$('#video').value.trim(),t=$('#thumb').value.trim(),bg=$('#pbg');bg.innerHTML=v?`<video src="${v}#t=${t||0.05}" muted preload="metadata" onerror="this.remove()"></video>`:'Preview';
 $('#pc').textContent=$('#cap').value||'Your caption appears here.';$('#cnt').textContent=$('#cap').value.length+' / 2200'}
['#video','#thumb','#cap'].forEach(s=>$(s).oninput=prev);

// submit
$('#go').onclick=async()=>{const video=$('#video').value.trim(),d=when();
 if(!S.ch)return toast('Select a channel first.',1);if(!video)return toast('Add a video URL.',1);
 if(S.mode=='customScheduled'&&d<Date.now()+60000)return toast('Pick a time at least a minute from now.',1);
 if(S.u&&S.u.used>=S.u.limit)return toast('Monthly limit reached ('+S.u.limit+'). Tap the counter if it is wrong.',1);
 const b=$('#go'),old=b.textContent;b.disabled=1;b.textContent='Sending...';
 try{const r=await api('/api/schedule',{key:S.key,channelId:S.ch.id,service:S.ch.service,mode:S.mode,dueAt:d.toISOString(),videoUrl:video,thumbOffset:$('#thumb').value===''?null:Math.round($('#thumb').value*1000),caption:$('#cap').value});
  setU(r);b.classList.add('ok');b.textContent='Done';toast(S.mode=='customScheduled'?'Scheduled for '+fmtD(d)+', '+$('#dt').textContent:S.mode=='shareNow'?'Sent to Buffer':'Added to queue');
  ['#video','#cap'].forEach(s=>$(s).value='');prev();
  setTimeout(()=>{b.classList.remove('ok');b.textContent=old;b.disabled=0},1800);
 }catch(e){toast(e.message,1);b.textContent=old;b.disabled=0}};
const setU=j=>{const p=j.used/j.limit;$('#lt').textContent=j.used+' / '+j.limit;$('#lb').style.width=Math.min(100,p*100)+'%';$('#lb').style.background=p>=.95?'var(--red)':p>=.8?'#ff9f0a':'var(--green)';$('#lim').title=(j.limit-j.used)+' posts left this month. Tap to correct the count.';S.u=j};
api('/api/usage',{}).then(setU);
$('#lim').onclick=()=>{const v=prompt('Posts already used this month (match Buffer):',S.u.used);if(v!==null&&v.trim()!==''&&!isNaN(v))api('/api/usage',{set:+v}).then(setU)};

// ---- auto plan
let running=false,stopF=false;
$('#slots').innerHTML=['06:00','11:00','15:00','19:00','22:00'].map(t=>`<input class="sl" type="time" value="${t}">`).join('');
$('#sd').value=new Date().toLocaleDateString('en-CA');
const urls=()=>$('#urls').value.split('\n').map(l=>l.trim().match(/^(https?:\/\/\S+?)(?:\s*\|\s*([\d.]+))?$/)).filter(Boolean).map(m=>({u:m[1],o:m[2]==null?null:Math.round(m[2]*1000)}));
const fmt=d=>d.toLocaleString('en',{day:'numeric',month:'short',hour:'numeric',minute:'2-digit'});
function plan(){const accs=[...document.querySelectorAll('.ac.on')].map(b=>S.all[b.dataset.i]),R=urls(),n=R.length,
 slots=[...document.querySelectorAll('.sl')].map(i=>i.value).filter(Boolean).sort(),per=+$('#per').value||6,gap=+$('#gap').value||2,days=+$('#dys').value||1,g=$('#tof').value===''?null:Math.round($('#tof').value*1000),
 start=new Date($('#sd').value+'T00:00'),out=[];if(!n||!accs.length)return out;
 const cur=accs.map((_,a)=>Math.floor(n/accs.length)*a);
 for(let d=0;d<days;d++)for(const t of slots){const[h,m]=t.split(':'),base=new Date(start);base.setDate(start.getDate()+d);base.setHours(+h,+m,0,0);
  if(base<Date.now()+120000)continue;
  accs.forEach((ac,a)=>{for(let j=0;j<per;j++){const r=R[cur[a]%n];out.push({ac,url:r.u,off:r.o!=null?r.o:g,when:new Date(base.getTime()+j*gap*6e4)});cur[a]++}})}
 return out}
function sum(){const p=plan(),n=urls().length,a=document.querySelectorAll('.ac.on').length;$('#uc').textContent=n+' reels';
 $('#sum').textContent=!a?'Select at least one account.':!n?'Add reel links or load a folder.':!p.length?'No future slots left in this range.':`${p.length} reels, ${a} accounts, ${p.length/a} per account (each reel up to ${Math.ceil(p.length/a/n)}x). ${fmt(p[0].when)} to ${fmt(p[p.length-1].when)}.`;
 if(!running){$('#run').disabled=!p.length;$('#run').textContent=p.length?'Schedule '+p.length+' reels':'Schedule all'}}
$('#s5').addEventListener('input',sum);$('#acs').onclick=e=>{if(e.target.dataset.i!=null){e.target.classList.toggle('on');sum()}};
$('#fl').onclick=async()=>{const b=$('#fl');b.disabled=1;b.textContent='Loading...';try{const j=await api('/api/folder',{url:$('#fu').value});$('#urls').value=j.urls.join('\n');sum();toast(j.urls.length+' reels found')}catch(e){toast(e.message,1)}b.disabled=0;b.textContent='Load'};
$('#run').onclick=async()=>{const p=plan();if(!p.length)return toast('Nothing to queue.',1);
 if(S.u&&S.u.limit-S.u.used<p.length&&!confirm('Only '+(S.u.limit-S.u.used)+' posts left this month but the plan has '+p.length+'. Continue?'))return;
 const b=$('#run');b.disabled=1;b.textContent='Queuing...';
 try{await api('/api/qadd',{key:S.key,cap:+$('#cap').value||10,items:p.map(x=>({channelId:x.ac.id,service:x.ac.service,dueAt:x.when.toISOString(),videoUrl:x.url,thumbOffset:x.off,caption:$('#acap').value}))});
  b.textContent='Queued '+p.length;toast(p.length+' reels queued. Keep Termux running.');poll()}catch(e){toast(e.message,1);b.disabled=0;sum()}};
$('#stop').onclick=async()=>{if(confirm('Remove all waiting reels from the queue? Reels already on Buffer stay.')){await api('/api/qclear',{});poll()}};
async function poll(){try{const j=await api('/api/qstatus',{}),t=j.sent+j.pending+j.missed+j.failed;
 $('#pg').style.width=(t?j.sent/t*100:0)+'%';$('#stop').style.display=j.pending?'':'none';
 $('#qs').textContent=t?`Queue: ${j.pending} waiting, ${j.sent} handed to Buffer${j.missed?', '+j.missed+' missed (time passed)':''}${j.failed?', '+j.failed+' failed':''}.${j.next?' Next: '+fmt(new Date(j.next))+'.':''}${j.log?' Last error: '+j.log:''}`:''}catch(e){}}
setInterval(poll,8000);poll();

// ---- prepare reels (cover at start)
let outDir='',cvOk=false;
const raw=async(p,f,h)=>{const r=await fetch(p,{method:'POST',headers:{'X-Name':encodeURIComponent(f.name),...h},body:f});if(r.status==401)return location.reload();const j=await r.json();if(!j.ok)throw new Error(j.error);return j};
api('/api/info',{}).then(j=>{outDir=j.out;cvOk=j.cover;$('#pnote').textContent=(j.ffmpeg?'':'ffmpeg not found. In Termux run: pkg install -y ffmpeg. ')+'Saved to: '+j.out});
$('#cvf').onchange=async e=>{const f=e.target.files[0];if(!f)return;try{await raw('/api/cover',f);cvOk=true;toast('Cover saved')}catch(x){toast(x.message,1)}};
$('#vf').onchange=e=>{const n=e.target.files.length;$('#prep').textContent=n?'Add cover to '+n+' videos':'Add cover to videos'};
$('#prep').onclick=async()=>{const fs=[...$('#vf').files];if(!cvOk)return toast('Choose a cover image first.',1);if(!fs.length)return toast('Select videos first.',1);
 const b=$('#prep');b.disabled=1;let ok=0;const er=[],nm=[];
 for(const[i,f]of fs.entries()){b.textContent='Editing '+(i+1)+' / '+fs.length;$('#pp').style.width=(i/fs.length*100)+'%';
  try{const rj=await raw('/api/edit',f,{'X-Dur':'0.05'});ok++;nm.push(rj.name)}catch(e){er.push(f.name+': '+e.message)}}
 $('#pp').style.width='100%';b.disabled=0;b.textContent=ok+' of '+fs.length+' ready';
 if(er.length)$('#pnote').textContent=er.slice(0,2).join(' | ');else $('#pnote').innerHTML='Done. Download: '+nm.map(n=>`<a href="/out/${encodeURIComponent(n)}" download>${n}</a>`).join(' &middot; ');toast(er.length?ok+' ready, '+er.length+' failed':'All '+ok+' videos ready',er.length>0)};
upd();
</script></body></html>
"""


def main():
    cloud = bool(os.environ.get("PORT"))  # Render sets PORT
    port = int(os.environ.get("PORT") or (sys.argv[1] if len(sys.argv) > 1 else 8765))
    host = "0.0.0.0" if cloud else "127.0.0.1"
    srv = None
    for p in range(port, port + (1 if cloud else 10)):
        try:
            srv = ThreadingHTTPServer((host, p), H)
            port = p
            break
        except OSError:
            continue
    if not srv:
        sys.exit("No free port found.")
    print(f"Reel Scheduler running on {host}:{port}  (Ctrl+C to stop)")
    if not cloud:
        threading.Timer(0.6, lambda: webbrowser.open(f"http://127.0.0.1:{port}")).start()
    threading.Thread(target=worker, daemon=True).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nBye")


if __name__ == "__main__":
    main()
