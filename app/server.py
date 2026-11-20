#!/usr/bin/env python3
"""Media Downloader: a small web UI around yt-dlp + ffmpeg.

Stdlib only (Python 3.8+). yt-dlp is called as a subprocess so it can be
updated on its own. Each download is a job with its own folder under
DATA_DIR/jobs; finished files are kept for KEEP_HOURS, then removed.

Public mode: requests that arrive with the header "X-Ytdl-Public: 1" (set by the
reverse proxy of the public domain, never by the LAN) are treated as anonymous
visitors: each visitor only sees and manages its own jobs (random owner cookie),
the owner's cookies.txt is not used, the generic extractor and private/LAN
addresses are refused, and size/duration/playlist/concurrency limits apply.
"""
import ipaddress
import json
import os
import re
import shlex
import secrets
import shutil
import signal
import socket
import subprocess
import threading
import time
import uuid
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote, urlparse

HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "18096"))
DATA = os.environ.get("DATA_DIR", "/data")
YTDLP = shlex.split(os.environ.get("YTDLP", "yt-dlp"))
KEEP_HOURS = float(os.environ.get("KEEP_HOURS", "24"))
MAX_PARALLEL = int(os.environ.get("MAX_PARALLEL", "2"))
UPDATE_CMD = os.environ.get("YTDLP_UPDATE_CMD", "")
EXTRA_ARGS = os.environ.get("YTDLP_EXTRA_ARGS", "").split()
STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
JOBS_DIR = os.path.join(DATA, "jobs")
COOKIES = os.path.join(DATA, "cookies.txt")
PUBLIC_KEEP_HOURS = float(os.environ.get("PUBLIC_KEEP_HOURS", "3"))
PUBLIC_MAX_ACTIVE = int(os.environ.get("PUBLIC_MAX_ACTIVE", "2"))       # queued+running jobs per visitor
PUBLIC_MAX_QUEUE = int(os.environ.get("PUBLIC_MAX_QUEUE", "12"))        # queued+running public jobs in total
PUBLIC_MAX_SECONDS = int(os.environ.get("PUBLIC_MAX_SECONDS", "10800"))  # longest video, 3 h
PUBLIC_MAX_FILESIZE = os.environ.get("PUBLIC_MAX_FILESIZE", "2G")
PUBLIC_MAX_ITEMS = int(os.environ.get("PUBLIC_MAX_ITEMS", "20"))         # playlist items per job
OWNER_COOKIE = "ytdl_owner"
OWNER_RE = re.compile(r"^[0-9a-f]{32}$")
os.makedirs(JOBS_DIR, exist_ok=True)

AUDIO_FORMATS = {"mp3", "m4a", "aac", "opus", "vorbis", "flac", "wav", "alac", "best"}
NO_COVER = {"wav", "aac"}  # containers yt-dlp can't embed cover art into
VIDEO_CONTAINERS = {"mp4", "mkv", "webm", "mov", "avi", "flv"}
REMUXABLE = {"mp4", "mkv", "webm", "mov"}
VCODECS = {"h264": "vcodec:h264", "vp9": "vcodec:vp9", "av1": "vcodec:av01", "h265": "vcodec:h265"}
SPONSOR_CATS = {"sponsor", "intro", "outro", "selfpromo", "preview", "filler",
                "interaction", "music_offtopic"}
SUB_LANG_RE = re.compile(r"^[A-Za-z0-9.*,\-_]{1,80}$")
TIME_RE = re.compile(r"^\d{1,3}(:\d{1,2}){0,2}(\.\d+)?$")
ITEMS_RE = re.compile(r"^[0-9:,\-]{1,100}$")
FMT_ID_RE = re.compile(r"^[A-Za-z0-9_\-]{1,40}$")
PROG_RE = re.compile(r"^\[dl\](.*)$")
ITEM_RE = re.compile(r"\[download\] Downloading item (\d+) of (\d+)")
STAGES = {
    "[Merger]": "Menggabungkan video + audio",
    "[ExtractAudio]": "Konversi audio",
    "[VideoConvertor]": "Konversi video (re-encode)",
    "[VideoRemuxer]": "Remux container",
    "[EmbedThumbnail]": "Menempel cover",
    "[Metadata]": "Menulis metadata",
    "[EmbedSubtitle]": "Menempel subtitle",
    "[SponsorBlock]": "Cek SponsorBlock",
    "[ModifyChapters]": "Memotong segmen",
    "[FixupM3u8]": "Merapikan file",
    "[ThumbnailsConvertor]": "Konversi cover",
}

jobs = {}
jobs_lock = threading.Lock()
slots = threading.Semaphore(MAX_PARALLEL)


def now():
    return time.time()


def base_args(public=False):
    a = ["--no-colors", "--ignore-config", "--no-warnings"]
    if public:
        # no owner cookies, no generic "any URL" extractor (keeps visitors off LAN/internal URLs)
        a += ["--use-extractors", "default,-generic"]
    elif os.path.isfile(COOKIES):
        a += ["--cookies", COOKIES]
    return a + EXTRA_ARGS


def public_url_ok(u):
    """Refuse URLs whose host is (or resolves to) a private, loopback or otherwise non-public address."""
    host = urlparse(u.strip()).hostname or ""
    if not host or host == "localhost" or host.endswith((".local", ".lan", ".internal", ".home.arpa")):
        return False
    try:
        addrs = {i[4][0] for i in socket.getaddrinfo(host, None)}
    except OSError:
        return False
    for a in addrs:
        ip = ipaddress.ip_address(a.split("%")[0])
        if not ip.is_global or ip.is_multicast:
            return False
    return bool(addrs)


def valid_url(u):
    if not isinstance(u, str) or len(u) > 2000:
        return False
    p = urlparse(u.strip())
    return p.scheme in ("http", "https") and bool(p.netloc)


# ---------------------------------------------------------------- info

def fmt_entry(f):
    vc = f.get("vcodec") or "none"
    ac = f.get("acodec") or "none"
    if vc == "none" and ac == "none":
        if f.get("ext") in ("mhtml",) or not f.get("protocol"):
            return None
    if f.get("ext") == "mhtml":
        return None
    kind = "av" if vc != "none" and ac != "none" else ("video" if vc != "none" else "audio")
    return {
        "id": f.get("format_id"),
        "kind": kind,
        "ext": f.get("ext"),
        "res": f.get("resolution") if kind != "audio" else "audio only",
        "height": f.get("height"),
        "fps": f.get("fps"),
        "vcodec": vc if vc != "none" else None,
        "acodec": ac if ac != "none" else None,
        "abr": f.get("abr"),
        "vbr": f.get("vbr"),
        "tbr": f.get("tbr"),
        "asr": f.get("asr"),
        "channels": f.get("audio_channels"),
        "hdr": f.get("dynamic_range") if f.get("dynamic_range") not in (None, "SDR") else None,
        "size": f.get("filesize") or f.get("filesize_approx"),
        "approx": not f.get("filesize") and bool(f.get("filesize_approx")),
        "note": f.get("format_note"),
        "lang": f.get("language"),
        "proto": f.get("protocol"),
    }


def get_info(url, playlist, public=False):
    cmd = [*YTDLP, *base_args(public), "-J", "--flat-playlist",
           "--yes-playlist" if playlist else "--no-playlist", "--", url]
    try:
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=90)
    except subprocess.TimeoutExpired:
        raise RuntimeError("Timeout waktu ambil info (90 detik).")
    except OSError as e:
        raise RuntimeError("yt-dlp tidak bisa dijalankan: " + str(e))
    if p.returncode != 0:
        err = p.stderr.decode("utf-8", "replace").strip().splitlines()
        msg = next((l for l in reversed(err) if l.startswith("ERROR")), err[-1] if err else "yt-dlp gagal")
        raise RuntimeError(msg.replace("ERROR: ", ""))
    d = json.loads(p.stdout.decode("utf-8", "replace"))
    out = {
        "title": d.get("title"),
        "uploader": d.get("uploader") or d.get("channel") or d.get("uploader_id"),
        "duration": d.get("duration"),
        "thumbnail": d.get("thumbnail"),
        "url": d.get("webpage_url") or url,
        "extractor": d.get("extractor_key"),
        "is_live": d.get("is_live"),
        "upload_date": d.get("upload_date"),
        "view_count": d.get("view_count"),
        "is_playlist": d.get("_type") == "playlist",
    }
    if out["is_playlist"]:
        ents = [e for e in (d.get("entries") or []) if e]
        out["count"] = d.get("playlist_count") or len(ents)
        out["entries"] = [{"title": e.get("title"), "duration": e.get("duration"),
                           "id": e.get("id")} for e in ents[:500]]
        if not out["thumbnail"]:
            th = d.get("thumbnails") or (ents[0].get("thumbnails") if ents else None) or []
            out["thumbnail"] = th[-1]["url"] if th else None
        out["formats"] = []
    else:
        fs = [x for x in (fmt_entry(f) for f in d.get("formats") or []) if x]
        out["formats"] = fs
        out["heights"] = sorted({f["height"] for f in fs if f["height"] and f["kind"] != "audio"},
                                reverse=True)
        out["has_subs"] = sorted((d.get("subtitles") or {}).keys())
        out["has_auto_subs"] = bool(d.get("automatic_captions"))
        out["chapters"] = len(d.get("chapters") or [])
    return out


# ---------------------------------------------------------------- build command

def build_cmd(o, jobdir, public=False):
    url = o["url"].strip()
    mode = o.get("mode", "video")
    playlist = bool(o.get("playlist"))
    name = "%(playlist_index)03d - %(title).150B [%(id)s].%(ext)s" if playlist \
        else "%(title).170B [%(id)s].%(ext)s"
    a = [*YTDLP, *base_args(public), "--newline", "--windows-filenames", "--no-mtime",
         "--continue",
         "--progress-template",
         "download:[dl]%(progress._percent_str)s|%(progress._speed_str)s|%(progress._eta_str)s|"
         "%(progress.downloaded_bytes)s|%(progress.total_bytes)s|%(progress.total_bytes_estimate)s",
         "-P", jobdir, "-P", "temp:" + os.path.join(jobdir, ".tmp"), "-o", name,
         "--print-to-file", "after_move:filepath", os.path.join(jobdir, ".files.txt"),
         "--yes-playlist" if playlist else "--no-playlist"]
    if playlist:
        items = (o.get("items") or "").strip()
        if items:
            if not ITEMS_RE.match(items):
                raise ValueError("Format item playlist salah (contoh: 1-5,8,10)")
            a += ["-I", items]
        a += ["--ignore-errors"]
    if public:
        a += ["--max-filesize", PUBLIC_MAX_FILESIZE,
              "--match-filters", f"!is_live & duration<=?{PUBLIC_MAX_SECONDS}"]
        if playlist:
            a += ["--max-downloads", str(PUBLIC_MAX_ITEMS)]

    fid = (o.get("format_id") or "").strip()
    if fid and not all(FMT_ID_RE.match(x) for x in fid.split("+")):
        raise ValueError("format_id tidak valid")

    if mode == "audio":
        af = o.get("audio_format", "mp3")
        if af not in AUDIO_FORMATS:
            raise ValueError("format audio tidak dikenal")
        a += ["-f", fid if fid else "ba/b"]
        a += ["-x", "--audio-format", af]
        q = str(o.get("audio_quality", "0"))
        if not re.match(r"^(\d{1,2}|\d{2,3}K)$", q):
            raise ValueError("kualitas audio tidak valid")
        a += ["--audio-quality", q]
        if o.get("sample_rate") in ("44100", "48000"):
            a += ["--postprocessor-args", "ExtractAudio:-ar " + o["sample_rate"]]
        if o.get("thumbnail", True) and af not in NO_COVER:
            a += ["--embed-thumbnail", "--convert-thumbnails", "jpg"]
        if o.get("metadata", True):
            a += ["--embed-metadata"]
            # nicer tags for music: artist from the track/artist field or the channel name
            a += ["--parse-metadata", "%(artist,creator,uploader)s:%(meta_artist)s",
                  "--parse-metadata", "%(album,playlist_title|)s:%(meta_album)s"]
            if playlist:
                a += ["--parse-metadata", "%(playlist_index)s:%(meta_track)s"]
        if o.get("split_chapters"):
            a += ["--split-chapters", "-o",
                  "chapter:%(title).100B/%(section_number)02d - %(section_title).120B.%(ext)s"]
    else:
        cont = o.get("container", "mp4")
        if cont not in VIDEO_CONTAINERS:
            raise ValueError("container tidak dikenal")
        h = o.get("height")
        h = int(h) if str(h or "").isdigit() else None
        if fid:
            # a picked video-only format still needs an audio track
            f = fid if "+" in fid or o.get("format_kind") != "video" else f"{fid}+ba/{fid}"
        else:
            f = f"bv*[height<={h}]+ba/b[height<={h}]/bv*+ba/b" if h else "bv*+ba/b"
        a += ["-f", f]
        sort = []
        if h:
            sort.append(f"res:{h}")
        fps = o.get("fps")
        if fps in ("30", "60"):
            sort.append(f"fps:{fps}")
        vc = VCODECS.get(o.get("vcodec") or "")
        if vc:
            sort.append(vc)
        if cont in ("mp4", "mov") and not vc:
            # "most compatible": keep the resolution/fps, then prefer H.264 + AAC
            # (YouTube has H.264 up to 1080p; above that VP9/AV1 is still picked)
            if not h:
                sort.insert(0, "res")
            if fps not in ("30", "60"):
                sort.append("fps")
            sort += ["vcodec:h264", "acodec:m4a"]
        elif cont == "webm" and not vc:
            sort.append("ext:webm:webm")
        if sort and not fid:
            a += ["-S", ",".join(sort)]
        merge = cont if cont in REMUXABLE else "mkv"
        a += ["--merge-output-format", merge]
        if o.get("recode") or cont not in REMUXABLE:
            a += ["--recode-video", cont]
        elif cont != "mkv":
            a += ["--remux-video", f"{cont}"]
        if o.get("metadata", True):
            a += ["--embed-metadata"]
        if o.get("thumbnail") and cont in ("mp4", "mkv", "mov"):
            a += ["--embed-thumbnail"]
        if o.get("chapters", True) and cont in ("mp4", "mkv", "mov", "webm"):
            a += ["--embed-chapters"]

    subs = o.get("subs", "none")
    if subs in ("embed", "file"):
        langs = (o.get("sub_langs") or "id.*,en.*").strip()
        if not SUB_LANG_RE.match(langs):
            raise ValueError("bahasa subtitle tidak valid")
        a += ["--write-subs", "--sub-langs", langs]
        if o.get("auto_subs"):
            a += ["--write-auto-subs"]
        if subs == "embed" and mode != "audio":
            a += ["--embed-subs"]
        else:
            a += ["--convert-subs", "srt"]

    start, end = (o.get("start") or "").strip(), (o.get("end") or "").strip()
    if start or end:
        for t in (start, end):
            if t and not TIME_RE.match(t):
                raise ValueError("format waktu salah (contoh 1:30 atau 90)")
        a += ["--download-sections", f"*{start or '0'}-{end or 'inf'}"]
        if mode != "audio" and o.get("precise_cut"):
            a += ["--force-keyframes-at-cuts"]

    sb = [c for c in (o.get("sponsorblock") or []) if c in SPONSOR_CATS]
    if sb:
        a += ["--sponsorblock-remove", ",".join(sb)]

    lim = str(o.get("rate_limit") or "").strip()
    if lim:
        if not re.match(r"^\d{1,5}[KM]$", lim):
            raise ValueError("limit kecepatan salah (contoh 2M)")
        a += ["-r", lim]

    a += ["--", url]
    return a


# ---------------------------------------------------------------- jobs

def public(j):
    keys = ("id", "title", "thumbnail", "mode", "label", "status", "percent", "speed", "eta",
            "stage", "item", "items", "error", "filename", "size", "created", "finished", "url",
            "downloaded", "total", "part")
    return {k: j.get(k) for k in keys}


def save_jobs():
    with jobs_lock:
        data = [dict(public(j), path=j.get("path"), owner=j.get("owner")) for j in jobs.values()
                if j["status"] in ("done", "error", "cancelled")]
    tmp = os.path.join(DATA, "jobs.json.tmp")
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, os.path.join(DATA, "jobs.json"))


def load_jobs():
    try:
        with open(os.path.join(DATA, "jobs.json")) as f:
            for j in json.load(f):
                if j.get("status") == "done" and not (j.get("path") and os.path.isfile(j["path"])):
                    continue
                jobs[j["id"]] = j
    except (OSError, ValueError):
        pass


def to_int(s):
    try:
        return int(float(s))
    except (TypeError, ValueError):
        return None


def collect_output(jobdir, title):
    listed = []
    fl = os.path.join(jobdir, ".files.txt")
    if os.path.isfile(fl):
        with open(fl, encoding="utf-8", errors="replace") as f:
            listed = [l.strip() for l in f if l.strip()]
    files = []
    for root, dirs, names in os.walk(jobdir):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for n in names:
            if n.startswith(".") or n.endswith((".part", ".ytdl")) or ".temp." in n:
                continue
            files.append(os.path.join(root, n))
    # with --split-chapters the original full file is also there; keep the chapters only
    chap = [f for f in files if os.path.dirname(f) != jobdir]
    if chap:
        files = chap
    elif listed:
        keep = [f for f in listed if os.path.isfile(f)]
        files = keep or files
    if not files:
        return None
    if len(files) == 1:
        return files[0]
    safe = re.sub(r'[\\/:*?"<>|]+', "_", title or "download").strip()[:120] or "download"
    zp = os.path.join(jobdir, ".zip", safe + ".zip")
    os.makedirs(os.path.dirname(zp), exist_ok=True)
    with zipfile.ZipFile(zp, "w", zipfile.ZIP_STORED, allowZip64=True) as z:
        for f in sorted(files):
            z.write(f, os.path.relpath(f, jobdir))
    return zp


def run_job(j, cmd):
    with slots:
        if j["status"] == "cancelled":
            return
        j["status"] = "running"
        j["stage"] = "Memulai"
        log = []
        dests = 0
        try:
            p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 start_new_session=True)
            j["_proc"] = p
            for raw in p.stdout:
                line = raw.decode("utf-8", "replace").rstrip()
                if not line:
                    continue
                m = PROG_RE.match(line)
                if m:
                    pc, sp, eta, dl, tot, est = (x.strip() for x in (m.group(1).split("|") + [""] * 6)[:6])
                    try:
                        j["percent"] = float(pc.rstrip("%"))
                    except ValueError:
                        pass
                    j["speed"] = None if sp in ("NA", "Unknown B/s") else sp
                    j["eta"] = None if eta in ("NA", "Unknown") else eta
                    j["downloaded"] = to_int(dl)
                    j["total"] = to_int(tot) or to_int(est)
                    j["stage"] = "Mengunduh"
                    continue
                log.append(line)
                del log[:-60]
                mi = ITEM_RE.search(line)
                if mi:
                    j["item"], j["items"] = int(mi.group(1)), int(mi.group(2))
                    dests = 0
                if line.startswith("[download] Destination:"):
                    dests += 1
                    j["part"] = dests
                    j["percent"] = 0
                for k, v in STAGES.items():
                    if line.startswith(k):
                        j["stage"] = v
                        j["speed"] = j["eta"] = None
                        break
            rc = p.wait()
        except Exception as e:  # noqa: BLE001
            rc = -1
            log.append("ERROR: " + str(e))
        j.pop("_proc", None)
        j["finished"] = now()
        j["log"] = log
        if j["status"] == "cancelled":
            shutil.rmtree(j["dir"], ignore_errors=True)
        else:
            out = collect_output(j["dir"], j.get("title")) if rc == 0 or j.get("items") else None
            if out:
                j.update(status="done", path=out, filename=os.path.basename(out),
                         size=os.path.getsize(out), percent=100, stage="Selesai", speed=None, eta=None)
                if rc != 0:
                    j["error"] = "Sebagian item gagal: " + last_error(log)
            else:
                j.update(status="error", stage="Gagal", error=last_error(log))
        save_jobs()


def last_error(log):
    errs = [l.replace("ERROR: ", "") for l in log if l.startswith("ERROR")]
    if errs:
        return errs[-1][:500]
    return (log[-1] if log else "yt-dlp keluar dengan error")[:500]


def cleanup_loop():
    while True:
        cutoff = now() - KEEP_HOURS * 3600
        pub_cutoff = now() - PUBLIC_KEEP_HOURS * 3600
        with jobs_lock:
            old = [j for j in jobs.values() if j["status"] in ("done", "error", "cancelled")
                   and (j.get("finished") or j["created"]) < (pub_cutoff if j.get("owner") else cutoff)]
            for j in old:
                jobs.pop(j["id"], None)
        for j in old:
            shutil.rmtree(os.path.join(JOBS_DIR, j["id"]), ignore_errors=True)
        known = set(jobs)
        for d in os.listdir(JOBS_DIR):
            pth = os.path.join(JOBS_DIR, d)
            if d not in known and os.path.getmtime(pth) < cutoff:
                shutil.rmtree(pth, ignore_errors=True)
        if old:
            save_jobs()
        time.sleep(300)


def ytdlp_version():
    try:
        return subprocess.run([*YTDLP, "--version"], stdout=subprocess.PIPE, timeout=20
                              ).stdout.decode().strip()
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------- http

class H(BaseHTTPRequestHandler):
    server_version = "yt-dl/1"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    # ---- public visitors (header set only by the public reverse proxy)
    def is_public(self):
        return self.headers.get("X-Ytdl-Public") == "1"

    def owner(self):
        if not self.is_public():
            return None
        if not hasattr(self, "_owner"):
            self._new_owner = False
            for part in (self.headers.get("Cookie") or "").split(";"):
                k, _, v = part.strip().partition("=")
                if k == OWNER_COOKIE and OWNER_RE.match(v):
                    self._owner = v
                    break
            else:
                self._owner, self._new_owner = secrets.token_hex(16), True
        return self._owner

    def end_headers(self):
        if self.is_public() and self.owner() and getattr(self, "_new_owner", False):
            self.send_header("Set-Cookie", f"{OWNER_COOKIE}={self._owner}; Path=/; Max-Age=2592000; "
                                           "HttpOnly; Secure; SameSite=Lax")
            self._new_owner = False
        super().end_headers()

    def mine(self, j):
        return not self.is_public() or (j.get("owner") and j.get("owner") == self.owner())

    def send_json(self, obj, code=200):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(b)

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > 100_000:
            raise ValueError("body terlalu besar")
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            return self.static("index.html", "text/html; charset=utf-8")
        if path == "/health":
            return self.send_json({"ok": True})
        if path == "/api/jobs":
            with jobs_lock:
                lst = sorted((public(j) for j in jobs.values() if self.mine(j)), key=lambda x: -x["created"])
            return self.send_json(lst)
        if path == "/api/version":
            if self.is_public():
                return self.send_json({"ytdlp": ytdlp_version(), "keep_hours": PUBLIC_KEEP_HOURS,
                                       "cookies": False, "can_update": False, "public": True,
                                       "limits": {"active": PUBLIC_MAX_ACTIVE, "max_hours": PUBLIC_MAX_SECONDS / 3600,
                                                  "max_filesize": PUBLIC_MAX_FILESIZE, "max_items": PUBLIC_MAX_ITEMS}})
            return self.send_json({"ytdlp": ytdlp_version(), "keep_hours": KEEP_HOURS,
                                   "cookies": os.path.isfile(COOKIES), "can_update": bool(UPDATE_CMD)})
        m = re.match(r"^/api/jobs/([0-9a-f]{12})/log$", path)
        if m and m.group(1) in jobs and self.mine(jobs[m.group(1)]):
            return self.send_json({"log": jobs[m.group(1)].get("log", [])})
        m = re.match(r"^/api/file/([0-9a-f]{12})$", path)
        if m:
            if m.group(1) in jobs and not self.mine(jobs[m.group(1)]):
                return self.send_json({"error": "file tidak ada / sudah dihapus"}, 404)
            return self.send_file(m.group(1))
        self.send_json({"error": "not found"}, 404)

    def static(self, name, ctype):
        with open(os.path.join(STATIC, name), "rb") as f:
            b = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(b)

    def send_file(self, jid):
        j = jobs.get(jid)
        if not j or j.get("status") != "done" or not os.path.isfile(j.get("path") or ""):
            return self.send_json({"error": "file tidak ada / sudah dihapus"}, 404)
        path = j["path"]
        size = os.path.getsize(path)
        start, end = 0, size - 1
        rng = self.headers.get("Range")
        m = re.match(r"bytes=(\d*)-(\d*)$", rng or "")
        if m and (m.group(1) or m.group(2)):
            if m.group(1):
                start = int(m.group(1))
                end = int(m.group(2)) if m.group(2) else size - 1
            else:
                start = max(0, size - int(m.group(2)))
            end = min(end, size - 1)
            if start > end:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        else:
            self.send_response(200)
        fn = os.path.basename(path)
        ascii_fn = re.sub(r'[^\x20-\x7e]|["\\]', "_", fn)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Disposition",
                         f"attachment; filename=\"{ascii_fn}\"; filename*=UTF-8''{quote(fn)}")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        self.end_headers()
        with open(path, "rb") as f:
            f.seek(start)
            left = end - start + 1
            while left > 0:
                chunk = f.read(min(1 << 20, left))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    return
                left -= len(chunk)

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            pub = self.is_public()
            if path in ("/api/info", "/api/download"):
                b = self.body()
                if not valid_url(b.get("url")):
                    return self.send_json({"error": "URL tidak valid"}, 400)
                if pub and not public_url_ok(b["url"]):
                    return self.send_json({"error": "Alamat ini tidak bisa dipakai di versi publik."}, 400)
            if path == "/api/info":
                return self.send_json(get_info(b["url"].strip(), bool(b.get("playlist")), pub))
            if path == "/api/download":
                if pub:
                    with jobs_lock:
                        active = [j for j in jobs.values() if j.get("owner") and j["status"] in ("queued", "running")]
                    if sum(1 for j in active if j["owner"] == self.owner()) >= PUBLIC_MAX_ACTIVE:
                        return self.send_json({"error": f"Maksimal {PUBLIC_MAX_ACTIVE} download berjalan sekaligus. "
                                                        "Tunggu yang lain selesai dulu."}, 429)
                    if len(active) >= PUBLIC_MAX_QUEUE:
                        return self.send_json({"error": "Server sedang ramai, coba lagi beberapa menit lagi."}, 503)
                jid = uuid.uuid4().hex[:12]
                jobdir = os.path.join(JOBS_DIR, jid)
                cmd = build_cmd(b, jobdir, pub)
                os.makedirs(jobdir, exist_ok=True)
                j = {"id": jid, "dir": jobdir, "url": b["url"].strip(),
                     "title": (b.get("title") or b["url"])[:300], "thumbnail": b.get("thumb_url") if isinstance(b.get("thumb_url"), str) else None,
                     "mode": b.get("mode", "video"), "label": str(b.get("label") or "")[:80],
                     "status": "queued", "stage": "Antre", "percent": 0, "created": now(),
                     "owner": self.owner()}
                with jobs_lock:
                    jobs[jid] = j
                threading.Thread(target=run_job, args=(j, cmd), daemon=True).start()
                return self.send_json(public(j))
            m = re.match(r"^/api/jobs/([0-9a-f]{12})/cancel$", path)
            if m and m.group(1) in jobs and self.mine(jobs[m.group(1)]):
                j = jobs[m.group(1)]
                if j["status"] in ("queued", "running"):
                    j["status"] = "cancelled"
                    j["stage"] = "Dibatalkan"
                    p = j.get("_proc")
                    if p:
                        try:
                            os.killpg(p.pid, signal.SIGTERM)
                        except OSError:
                            pass
                    else:
                        j["finished"] = now()
                return self.send_json(public(j))
            m = re.match(r"^/api/jobs/([0-9a-f]{12})/delete$", path)
            if m and m.group(1) in jobs and self.mine(jobs[m.group(1)]):
                j = jobs[m.group(1)]
                if j["status"] in ("queued", "running"):
                    return self.send_json({"error": "batalkan dulu"}, 409)
                with jobs_lock:
                    jobs.pop(j["id"], None)
                shutil.rmtree(os.path.join(JOBS_DIR, j["id"]), ignore_errors=True)
                save_jobs()
                return self.send_json({"ok": True})
            if path == "/api/clear":
                with jobs_lock:
                    done = [j for j in jobs.values() if j["status"] in ("done", "error", "cancelled") and self.mine(j)]
                    for j in done:
                        jobs.pop(j["id"], None)
                for j in done:
                    shutil.rmtree(os.path.join(JOBS_DIR, j["id"]), ignore_errors=True)
                save_jobs()
                return self.send_json({"ok": True, "removed": len(done)})
            if path == "/api/update":
                if pub:
                    return self.send_json({"error": "update hanya dari LAN"}, 403)
                if not UPDATE_CMD:
                    return self.send_json({"error": "update tidak diaktifkan"}, 400)
                p = subprocess.run(UPDATE_CMD, shell=True, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, timeout=300)
                return self.send_json({"ok": p.returncode == 0, "version": ytdlp_version(),
                                       "output": p.stdout.decode("utf-8", "replace")[-1500:]})
            self.send_json({"error": "not found"}, 404)
        except (ValueError, RuntimeError) as e:
            self.send_json({"error": str(e)}, 400)
        except subprocess.TimeoutExpired:
            self.send_json({"error": "timeout"}, 504)
        except Exception as e:  # noqa: BLE001
            self.send_json({"error": "server error: " + str(e)[:300]}, 500)


def main():
    load_jobs()
    threading.Thread(target=cleanup_loop, daemon=True).start()
    srv = ThreadingHTTPServer((HOST, PORT), H)
    srv.daemon_threads = True
    print(f"yt-dl listening on {HOST}:{PORT}, yt-dlp {ytdlp_version()}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
