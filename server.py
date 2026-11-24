#!/usr/bin/env python3
"""BAUHAUS AI — local bridge.

Serves index.html on 127.0.0.1 and relays the few calls the page needs:
GPT (OpenAI Responses API, streamed), Xiaoyunque / Seedance (upload, submit, poll),
Openverse music search, a media relay (so the browser can edit clips on a canvas),
and saving the finished film into ./作品/.
Keys live in keys.json next to this file and never reach the page; the page can
set them (POST /api/config) and see only whether each one is set plus its last 4 chars.
Standard library only.
"""
import subprocess
import json, os, re, ssl, sys, time, uuid, threading, webbrowser, urllib.parse, urllib.request, urllib.error
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

ROOT = os.path.dirname(os.path.abspath(__file__))
PORT = int(os.environ.get("BAUHAUS_PORT", "7788"))
XYQ = "https://xyq.jianying.com"
OPENAI = "https://api.openai.com/v1/responses"
UA = "BauhausAI/1.0"

KEYS = os.path.join(ROOT, "keys.json")
DEFAULTS = {"openai_api_key": "", "xyq_access_key": "", "gpt_model": "gpt-6.2-sol", "video_model": "Seedance_2.0_mini_lite"}

def load_cfg():
    cfg = dict(DEFAULTS)
    try:
        with open(KEYS, encoding="utf-8") as f:
            cfg.update({k: v for k, v in json.load(f).items() if isinstance(v, str)})
    except (OSError, ValueError):
        pass
    return cfg

CFG = load_cfg()
NAMES = {"openai_api_key": "OpenAI API Key", "xyq_access_key": "小云雀 Access Key"}

def key(name):
    v = (CFG.get(name) or "").strip()
    if not v:
        raise RuntimeError("还没有配置%s，请点主页右上角「API 设置」填写" % NAMES[name])
    return v

def save_cfg(new):
    data = dict(CFG)
    data.update(new)
    tmp = KEYS + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, KEYS)
    CFG.clear()
    CFG.update(data)

def cfg_status():
    def one(name):
        v = (CFG.get(name) or "").strip()
        return {"set": bool(v), "tail": v[-4:] if len(v) >= 12 else ""}
    return {"openai": one("openai_api_key"), "xyq": one("xyq_access_key"),
            "gpt_model": CFG.get("gpt_model"), "video_model": CFG.get("video_model")}

def _ctx():
    try:
        import certifi  # type: ignore
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()
CTX = _ctx()

def http(url, data=None, headers=None, method=None, timeout=120):
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    req.add_header("User-Agent", UA)
    try:
        return urllib.request.urlopen(req, context=CTX, timeout=timeout)
    except ssl.SSLError:
        # Some bundled Pythons ship without a CA store; fall back rather than die.
        return urllib.request.urlopen(req, context=ssl._create_unverified_context(), timeout=timeout)

def xyq_post(path, body):
    r = http(XYQ + path, json.dumps(body).encode(), {
        "Authorization": "Bearer " + key("xyq_access_key"),
        "Content-Type": "application/json", "Accept": "application/json"}, "POST", 60)
    return json.loads(r.read().decode("utf-8"))

BABI = urllib.parse.quote(json.dumps({"scene_lv1": "ai_agent", "scene_lv2": "front_tool", "tool_id": "instant_video",
                                      "tab_name": "other", "edit_type": "instant_video", "enter_from": "skill"}))

def _maybe_json(obj):
    if isinstance(obj, str) and obj[:1] in "{[":
        try:
            return json.loads(obj)
        except Exception:
            return obj
    return obj

def walk_videos(obj, out):
    obj = _maybe_json(obj)
    if isinstance(obj, dict):
        v = obj.get("video")
        if isinstance(v, dict) and (v.get("download_url") or v.get("url")):
            u = v.get("download_url") or v.get("url")
            if u not in out:
                out.append(u)
        for k, val in obj.items():
            if k != "video":
                walk_videos(val, out)
    elif isinstance(obj, list):
        for it in obj:
            walk_videos(it, out)

def walk_errors(obj, out):
    obj = _maybe_json(obj)
    if isinstance(obj, dict):
        for k in ("error_message", "error_msg", "errmsg", "message"):
            if isinstance(obj.get(k), str) and obj[k].strip() and k != "message":
                out.append(obj[k])
        fr = obj.get("fail_reason")
        if isinstance(fr, dict) and fr.get("message"):
            out.append(fr["message"])
        for val in obj.values():
            walk_errors(val, out)
    elif isinstance(obj, list):
        for it in obj:
            walk_errors(it, out)

class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def log_message(self, fmt, *a):
        sys.stderr.write("  · " + (fmt % a) + "\n")

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def _json(self, obj, code=200):
        b = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(b)

    def _local_origin(self):
        # Only this page (or the file:// copy of it) may drive the bridge; any other website is refused,
        # so a page you happen to visit cannot spend your credits or rewrite your keys.
        o = self.headers.get("Origin")
        return o is None or o == "null" or o in ("http://127.0.0.1:%d" % PORT, "http://localhost:%d" % PORT)

    def end_headers(self):
        o = self.headers.get("Origin")
        if o and self._local_origin():
            self.send_header("Access-Control-Allow-Origin", o)
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
        BaseHTTPRequestHandler.end_headers(self)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _file(self, path, ctype, head=False):
        size = os.path.getsize(path)
        start, end = 0, size - 1
        rng = self.headers.get("Range") or ""
        m = re.match(r"bytes=(\d*)-(\d*)$", rng.strip())
        if m and size:
            if m.group(1):
                start = int(m.group(1))
                end = min(int(m.group(2)), size - 1) if m.group(2) else size - 1
            elif m.group(2):
                start = max(0, size - int(m.group(2)))
        partial = bool(m) and size and start <= end
        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        if partial:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
        self.send_header("Content-Length", str(end - start + 1 if partial else size))
        self.send_header("Cache-Control", "no-store" if ctype.startswith("text/html") else "max-age=3600")
        self.end_headers()
        if head:
            return
        with open(path, "rb") as f:
            f.seek(start)
            left = (end - start + 1) if partial else size
            while left > 0:
                chunk = f.read(min(1 << 20, left))
                if not chunk:
                    break
                self.wfile.write(chunk)
                left -= len(chunk)

    def do_HEAD(self):
        self.do_GET(head=True)

    def do_GET(self, head=False):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        try:
            if u.path in ("/", "/index.html"):
                return self._file(os.path.join(ROOT, "index.html"), "text/html; charset=utf-8")
            if u.path.startswith(("/demo/", "/home/")):
                name = os.path.basename(urllib.parse.unquote(u.path))
                fp = os.path.join(ROOT, u.path.split("/")[1], name)
                types = {".jpg": "image/jpeg", ".mp4": "video/mp4", ".mp3": "audio/mpeg", ".json": "application/json; charset=utf-8"}
                ext = os.path.splitext(name)[1].lower()
                if u.path.startswith("/home/") and ext not in (".mp4", ".jpg"):
                    return self.send_error(404)
                if name and ext in types and os.path.isfile(fp):
                    return self._file(fp, types[ext], head)
                return self.send_error(404)
            if u.path.startswith("/api/") and not self._local_origin():
                return self._json({"error": "forbidden origin"}, 403)
            if u.path == "/api/health":
                st = cfg_status()
                return self._json({"ok": True, "configured": st["openai"]["set"] and st["xyq"]["set"],
                                   "gpt_model": CFG.get("gpt_model"), "video_model": CFG.get("video_model")})
            if u.path == "/api/config":
                return self._json(cfg_status())
            if u.path == "/api/music":
                return self.music(q.get("q", ["ambient piano"])[0])
            if u.path == "/api/gpt_bg":
                return self.gpt_bg_poll(q.get("id", [""])[0])
            if u.path == "/api/media":
                return self.media(q.get("u", [""])[0])
            self.send_error(404)
        except Exception as e:
            self._json({"error": str(e)}, 502)

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        try:
            if not self._local_origin():
                return self._json({"error": "forbidden origin"}, 403)
            if u.path == "/api/config":
                return self.config_set()
            if u.path == "/api/config/test":
                return self.config_test()
            if u.path == "/api/gpt":
                return self.gpt()
            if u.path == "/api/gpt_bg":
                return self.gpt_bg_submit()
            if u.path == "/api/xyq/upload":
                return self.upload()
            if u.path == "/api/xyq/submit":
                return self.submit()
            if u.path == "/api/xyq/poll":
                return self.poll()
            if u.path == "/api/save":
                return self.save(urllib.parse.parse_qs(u.query).get("name", ["film.webm"])[0])
            self.send_error(404)
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:800]
            self._json({"error": "HTTP %s: %s" % (e.code, detail)}, 502)
        except Exception as e:
            self._json({"error": str(e)}, 502)

    # ---------- GPT (streamed SSE passthrough) ----------
    def gpt(self):
        body = json.loads(self._body() or b"{}")
        body.setdefault("model", CFG.get("gpt_model", "gpt-6.2-sol"))
        body["stream"] = True
        try:
            r = http(OPENAI, json.dumps(body).encode(), {
                "Authorization": "Bearer " + key("openai_api_key"), "Content-Type": "application/json"}, "POST", 600)
        except urllib.error.HTTPError as e:
            return self._json({"error": "OpenAI %s: %s" % (e.code, e.read().decode("utf-8", "replace")[:800])}, 502)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        try:
            while True:
                line = r.readline()
                if not line:
                    break
                self.wfile.write(b"%x\r\n%s\r\n" % (len(line), line))
                if line in (b"\n", b"\r\n"):
                    self.wfile.flush()
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass

    # Long image generations get cut on a held-open stream; background mode submits and is polled instead.
    def gpt_bg_submit(self):
        body = json.loads(self._body() or b"{}")
        body.setdefault("model", CFG.get("gpt_model", "gpt-6.2-sol"))
        body["background"] = True
        body.pop("stream", None)
        try:
            r = http(OPENAI, json.dumps(body).encode(), {
                "Authorization": "Bearer " + key("openai_api_key"), "Content-Type": "application/json"}, "POST", 60)
        except urllib.error.HTTPError as e:
            return self._json({"error": "OpenAI %s: %s" % (e.code, e.read().decode("utf-8", "replace")[:800])}, 502)
        d = json.loads(r.read().decode("utf-8"))
        return self._json({"id": d.get("id"), "status": d.get("status")})

    def gpt_bg_poll(self, rid):
        if not re.match(r"^resp_[A-Za-z0-9]+$", rid or ""):
            return self._json({"error": "bad id"}, 400)
        try:
            r = http(OPENAI + "/" + rid, None, {"Authorization": "Bearer " + key("openai_api_key")}, "GET", 60)
        except urllib.error.HTTPError as e:
            return self._json({"error": "OpenAI %s: %s" % (e.code, e.read().decode("utf-8", "replace")[:800])}, 502)
        d = json.loads(r.read().decode("utf-8"))
        img = next((o.get("result") for o in d.get("output") or [] if o.get("type") == "image_generation_call" and o.get("result")), None)
        err = d.get("error") or d.get("incomplete_details")
        return self._json({"status": d.get("status"), "image": img, "error": (err.get("message") if isinstance(err, dict) else err)})

    # ---------- Xiaoyunque ----------
    def upload(self):
        data = self._body()
        ctype = self.headers.get("Content-Type", "image/jpeg")
        name = ("model" if self.headers.get("X-Name") == "model" else "garment") + "." + ("png" if "png" in ctype else "webp" if "webp" in ctype else "jpg")
        boundary = "----bauhaus" + uuid.uuid4().hex
        payload = (("--%s\r\nContent-Disposition: form-data; name=\"file\"; filename=\"%s\"\r\nContent-Type: %s\r\n\r\n"
                    % (boundary, name, ctype)).encode() + data + ("\r\n--%s--\r\n" % boundary).encode())
        r = http(XYQ + "/api/biz/v1/skill/upload_file", payload, {
            "Authorization": "Bearer " + key("xyq_access_key"),
            "Content-Type": "multipart/form-data; boundary=" + boundary, "Accept": "application/json"}, "POST", 120)
        d = json.loads(r.read().decode("utf-8"))
        if d.get("ret") != "0":
            return self._json({"error": d.get("errmsg") or "upload failed", "log_id": d.get("log_id")}, 502)
        dd = d.get("data") or {}
        return self._json({"asset_id": dd.get("pippit_asset_id") or dd.get("asset_id"),
                           "width": dd.get("width"), "height": dd.get("height"), "log_id": d.get("log_id")})

    def submit(self):
        b = json.loads(self._body() or b"{}")
        prompt = (b.get("prompt") or "").strip()
        param = {"prompt": prompt, "duration_sec": int(b.get("duration", 5)), "ratio": b.get("ratio", "9:16"),
                 "model": b.get("model") or CFG.get("video_model", "Seedance_2.0_mini_lite")}
        ids = [i for i in (b.get("asset_ids") or []) if i]
        if ids:
            param["images"] = [{"pippit_asset_id": i} for i in ids]
        if b.get("resolution"):
            param["resolution"] = b["resolution"]
        if b.get("first_frame"):
            param["generate_type"] = 1  # first-frame mode: the single image is frame 0
        body = {"agent_name": "pippit_video_part_agent", "call_source": "pippit_cli", "platform": "bauhaus_ai",
                "message": prompt, "video_part_tool_param": param}
        r = http(XYQ + "/api/biz/v1/skill/submit_run?babi_param=" + BABI, json.dumps(body).encode(), {
            "Authorization": "Bearer " + key("xyq_access_key"), "Content-Type": "application/json",
            "Accept": "application/json"}, "POST", 60)
        d = json.loads(r.read().decode("utf-8"))
        if d.get("ret") != "0":
            return self._json({"error": d.get("errmsg") or "submit failed", "log_id": d.get("log_id")}, 502)
        run = (d.get("data") or {}).get("run") or {}
        return self._json({"thread_id": run.get("thread_id"), "run_id": run.get("run_id"),
                           "link": (d.get("data") or {}).get("web_thread_link")})

    def poll(self):
        b = json.loads(self._body() or b"{}")
        d = xyq_post("/api/biz/v1/skill/get_thread", {"thread_id": b["thread_id"], "run_id": b["run_id"]})
        thread = ((d.get("data") or {}).get("thread")) or {}
        run = next((x for x in thread.get("run_list") or [] if x.get("run_id") == b["run_id"]), None)
        if not run:
            return self._json({"state": 1, "status": "running", "videos": []})
        st = int(run.get("state") or 0)
        vids, errs = [], []
        walk_videos(run.get("entry_list") or [], vids)
        if st in (4, 5, 9):
            walk_errors(run, errs)
        status = {3: "succeeded", 4: "failed", 5: "canceled", 9: "interrupted", 6: "input_required"}.get(st, "running")
        return self._json({"state": st, "status": status, "videos": vids, "error": (errs[0] if errs else "")})

    # ---------- Music (Openverse, CC-licensed) ----------
    def music(self, query):
        out = []
        for params in ({"q": query, "source": "jamendo", "license_type": "commercial", "page_size": "12"},
                       {"q": query, "source": "jamendo", "page_size": "12"},
                       {"q": "ambient piano", "source": "jamendo", "license_type": "commercial", "page_size": "12"}):
            try:
                r = http("https://api.openverse.org/v1/audio/?" + urllib.parse.urlencode(params), None,
                         {"Accept": "application/json"}, "GET", 30)
                d = json.loads(r.read().decode("utf-8"))
            except Exception:
                continue
            for it in d.get("results") or []:
                if not it.get("url") or it.get("mature"):
                    continue
                dur = (it.get("duration") or 0) / 1000.0
                if dur and dur < 40:
                    continue
                out.append({"title": it.get("title"), "creator": it.get("creator"), "url": it.get("url"),
                            "landing": it.get("foreign_landing_url"), "license": it.get("license"),
                            "license_version": it.get("license_version"), "duration": round(dur),
                            "genres": it.get("genres") or [], "tags": [t.get("name") for t in it.get("tags") or []][:8],
                            "attribution": it.get("attribution")})
            if len(out) >= 4:
                break
        return self._json({"query": query, "results": out[:10]})

    # ---------- Media relay (same-origin so the canvas stays exportable) ----------
    def media(self, url):
        p = urllib.parse.urlparse(url)
        if p.scheme != "https" or not p.hostname:
            return self._json({"error": "https only"}, 400)
        try:
            r = http(url, None, {}, "GET", 300)
        except (urllib.error.URLError, ssl.SSLError, OSError) as e:
            if isinstance(e, urllib.error.HTTPError):
                raise
            # some CDN edges refuse this Python's TLS handshake; curl (system TLS) gets through
            return self._curl(url)
        self.send_response(200)
        self.send_header("Content-Type", r.headers.get("Content-Type", "application/octet-stream"))
        ln = r.headers.get("Content-Length")
        if ln:
            self.send_header("Content-Length", ln)
        else:
            self.send_header("Transfer-Encoding", "chunked")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            while True:
                chunk = r.read(65536)
                if not chunk:
                    break
                self.wfile.write(chunk if ln else b"%x\r\n%s\r\n" % (len(chunk), chunk))
            if not ln:
                self.wfile.write(b"0\r\n\r\n")
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _curl(self, url):
        for attempt in range(4):
            proc = subprocess.Popen(["curl", "-sSfL", "--max-time", "300", "--", url], stdout=subprocess.PIPE)
            first = proc.stdout.read(65536)
            if first:
                break
            proc.wait()
            time.sleep(1.5 * (attempt + 1))
        else:
            return self._json({"error": "download failed"}, 502)
        self.send_response(200)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            chunk = first
            while chunk:
                self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
                chunk = proc.stdout.read(65536)
            self.wfile.write(b"0\r\n\r\n")
        except (BrokenPipeError, ConnectionResetError):
            proc.kill()
        proc.wait()

    # ---------- API settings ----------
    def config_set(self):
        b = json.loads(self._body() or b"{}")
        new = {}
        for k in ("openai_api_key", "xyq_access_key", "gpt_model", "video_model"):
            v = b.get(k)
            if isinstance(v, str) and v.strip():
                v = v.strip()
                if len(v) > 400 or any(c.isspace() for c in v):
                    return self._json({"error": "格式不对：%s" % k}, 400)
                new[k] = v
        for k in b.get("clear") or []:
            if k in NAMES:
                new[k] = ""
        if new:
            save_cfg(new)
        return self._json(cfg_status())

    def config_test(self):
        out = {}
        try:
            r = http("https://api.openai.com/v1/models", None, {"Authorization": "Bearer " + key("openai_api_key")}, "GET", 20)
            out["openai"] = {"ok": r.status == 200, "msg": "可用"}
        except urllib.error.HTTPError as e:
            out["openai"] = {"ok": False, "msg": "密钥无效" if e.code == 401 else "HTTP %d" % e.code}
        except Exception as e:
            out["openai"] = {"ok": False, "msg": str(e)[:60]}
        try:
            d = xyq_post("/api/biz/v1/skill/get_thread", {"thread_id": "0", "run_id": "0"})
            bad = str(d.get("ret")) == "1015" or "access key" in str(d.get("errmsg", "")).lower()
            out["xyq"] = {"ok": not bad, "msg": "密钥无效" if bad else "可用"}
        except urllib.error.HTTPError as e:
            out["xyq"] = {"ok": False, "msg": "密钥无效" if e.code in (401, 403) else "HTTP %d" % e.code}
        except Exception as e:
            out["xyq"] = {"ok": False, "msg": str(e)[:60]}
        return self._json(out)

    def save(self, name):
        safe = "".join(c for c in os.path.basename(name) if c.isalnum() or c in "._-") or "film.webm"
        path = os.path.join(ROOT, "作品", safe)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(self._body())
        return self._json({"saved": path})

def main():
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), H)
    url = "http://127.0.0.1:%d/" % PORT
    print("\n  BAUHAUS AI  ·  Fashion Film Director\n  %s\n  (close this window to stop)\n" % url)
    if "--no-browser" not in sys.argv:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass

if __name__ == "__main__":
    main()
