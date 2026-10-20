#!/usr/bin/env python3
"""
GetHub WatchDog
---------------
A desktop app that watches GitHub Actions builds in real time:
  * live list of recent builds (any workflow, any language / compiler)
  * job + step breakdown with start times and durations
  * a "build flow" timeline showing how the work is progressing
  * estimated time remaining, based on recent successful builds
  * an alarm + pop-up the moment a build finishes (pass or fail)

Only uses the Python standard library (tkinter + urllib). Run with:
    python gethub_watchdog.py
"""

import base64
import hashlib
import hmac
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime, timedelta, timezone
from statistics import median

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

APP = "GetHub WatchDog"
API = "https://api.github.com"
VERSION = "1.4"


def _app_dir():
    if getattr(sys, "frozen", False):          # running as the portable .exe
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


# Portable mode: the .exe (or a copy of the script with a settings file beside it)
# keeps its settings next to itself, e.g. on a USB stick, and never writes to the PC.
LOCAL_CFG = os.path.join(_app_dir(), "GetHubWatchDog-settings.json")
PORTABLE = getattr(sys, "frozen", False) or os.path.exists(LOCAL_CFG)
CFG_PATH = LOCAL_CFG if PORTABLE else os.path.join(os.path.expanduser("~"), ".gethub_watchdog.json")

COLORS = {
    "ok": "#1a7f37",     # passed
    "bad": "#cf222e",    # failed
    "run": "#0969da",    # running
    "wait": "#9a6700",   # queued
    "muted": "#6e7781",  # cancelled / skipped
    "warn": "#bc4c00",   # compiled fine, only publish/release failed
}

# Jobs or steps whose name contains one of these words are treated as
# "publish" work. If only those fail, the build itself still counts as passed.
# Edit "publish_keywords" in ~/.gethub_watchdog.json to change the list.
PUBLISH_KEYWORDS = ["release", "publish", "deploy", "pages", "upload",
                    "notify", "announce", "docker push"]
BAD = ("failure", "timed_out")


# ----------------------------------------------------------------- helpers
def now():
    return datetime.now(timezone.utc)


def parse_t(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def fmt_dur(sec):
    if sec is None:
        return "—"
    sec = int(max(0, sec))
    h, r = divmod(sec, 3600)
    m, s = divmod(r, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def local_clock(dt):
    if not dt:
        return "—"
    return dt.astimezone().strftime("%I:%M:%S %p").lstrip("0")


def normalize_repo(s):
    s = (s or "").strip().rstrip("/")
    if s.endswith(".git"):
        s = s[:-4]
    if "github.com" in s:
        s = s.split("github.com", 1)[1].lstrip("/:")
    parts = [p for p in s.split("/") if p]
    return "/".join(parts[:2]) if len(parts) >= 2 else None


def status_of(obj):
    st, c = obj.get("status"), obj.get("conclusion")
    if st == "completed":
        return {
            "success": ("✔ passed", "ok"),
            "failure": ("✖ failed", "bad"),
            "timed_out": ("⌛ timed out", "bad"),
            "startup_failure": ("✖ startup failure", "bad"),
            "cancelled": ("⦸ cancelled", "muted"),
            "skipped": ("↷ skipped", "muted"),
            "neutral": ("■ neutral", "muted"),
        }.get(c, (f"■ {c}", "muted"))
    if st == "in_progress":
        return ("▶ running", "run")
    return (f"⏳ {st}", "wait")  # queued / waiting / pending / requested


def is_publish(name, kws):
    n = (name or "").lower()
    return any(k in n for k in kws)


def failed_as_publish(job, kws):
    """True if this failed job only failed in publish/release work."""
    if is_publish(job.get("name"), kws):
        return True
    bad = [s for s in job.get("steps") or [] if s.get("conclusion") in BAD]
    return bool(bad) and all(is_publish(s.get("name"), kws) for s in bad)


def item_status(obj, kws, parent_publish=False):
    """Status for a job or step, marking publish-only failures in orange."""
    if obj.get("conclusion") in BAD:
        if "steps" in obj:
            pub = failed_as_publish(obj, kws)
        else:
            pub = parent_publish or is_publish(obj.get("name"), kws)
        if pub:
            return ("⚠ publish failed", "warn")
    return status_of(obj)


def verdict(run, jobs, kws):
    """(text, colour tag, kind) for a run. kind: running/pass/publish/fail/other."""
    if run.get("status") != "completed":
        t, g = status_of(run)
        return t, g, "running"
    c = run.get("conclusion")
    if c == "success":
        return "✔ passed", "ok", "pass"
    if c not in BAD or jobs is None:
        t, g = status_of(run)
        return t, g, ("fail" if g == "bad" else "other")
    real = pub = False
    for j in jobs:
        if j.get("conclusion") in BAD:
            if failed_as_publish(j, kws):
                pub = True
            else:
                real = True
    if pub and not real:
        return "✔ built · publish ✖", "warn", "publish"
    return "✖ failed", "bad", "fail"


def run_span(r):
    s = parse_t(r.get("run_started_at") or r.get("created_at"))
    e = parse_t(r.get("updated_at")) if r.get("status") == "completed" else None
    return s, e


def item_span(obj):
    if obj.get("status") in ("queued", "waiting", "pending", "requested"):
        return None, None
    s = parse_t(obj.get("started_at"))
    e = parse_t(obj.get("completed_at")) if obj.get("status") == "completed" else None
    return s, e


# ----------------------------------------------------------------- build logs
TS_RE = re.compile(r"^\ufeff?(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.\d+)?Z ?")
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
ERR_RE = re.compile(r"##\[error\]|\b(fatal )?error\b\s*([A-Z]+\d+)?\s*:|\bFAILED\b|undefined reference"
                    r"|unresolved external|Traceback \(most recent|exit code [1-9]|\bpanic:", re.I)
WARN_RE = re.compile(r"##\[warning\]|\bwarning\b\s*([A-Z]+\d+)?\s*:", re.I)
MAX_SHOWN = 30000


def parse_log(text):
    """GitHub job log -> list of (line number, time, text, tag)."""
    out, last = [], None
    for i, raw in enumerate(text.splitlines(), 1):
        m = TS_RE.match(raw)
        if m:
            last = datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
            line = raw[m.end():]
        else:
            line = raw.lstrip("\ufeff")
        line = ANSI_RE.sub("", line)
        tag = None
        if line.startswith("##[endgroup]"):
            continue
        if line.startswith("##[group]"):
            line, tag = "▾ " + line[9:], "group"
        elif line.startswith("##[error]"):
            line, tag = "✖ " + line[9:], "err"
        elif line.startswith("##[warning]"):
            line, tag = "⚠ " + line[11:], "warn"
        elif line.startswith("##[command]"):
            line, tag = "$ " + line[11:], "cmd"
        elif line.startswith(("##[debug]", "##[notice]")):
            line, tag = line.split("]", 1)[1], "cmd"
        elif ERR_RE.search(line):
            tag = "err"
        elif WARN_RE.search(line):
            tag = "warn"
        out.append((i, last, line, tag))
    return out


def step_lines(parsed, step):
    """The part of a job log written while one step was running."""
    s = parse_t(step.get("started_at"))
    if not s:
        return []
    e = parse_t(step.get("completed_at")) or now()
    lo, hi = s - timedelta(seconds=1), e + timedelta(seconds=1)
    return [p for p in parsed if p[1] and lo <= p[1] <= hi]


def load_cfg():
    try:
        with open(CFG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


# ----------------------------------------------------------------- locked vault
# Saved repositories and tokens are kept encrypted in the settings file.
# Passcode -> scrypt (deliberately slow, ~1 s and 128 MB per try) -> two keys.
# Encryption: HMAC-SHA256 in counter mode; integrity: HMAC-SHA256 tag
# (encrypt-then-MAC). Standard library only, so the Python version needs nothing extra.
VAULT_N = 2 ** 17
MAX_TRIES = 10            # wrong passcodes in a row before the vault is erased


def _b64(b):
    return base64.b64encode(b).decode("ascii")


def _kdf(pin, salt, n=VAULT_N):
    k = hashlib.scrypt(pin.encode("utf-8"), salt=salt, n=n, r=8, p=1,
                       maxmem=512 * 1024 * 1024, dklen=64)
    return k[:32], k[32:]


def _keystream(key, nonce, length):
    out, i = bytearray(), 0
    while len(out) < length:
        out += hmac.new(key, nonce + i.to_bytes(8, "big"), hashlib.sha256).digest()
        i += 1
    return bytes(out[:length])


def vault_keys(pin, salt=None):
    salt = salt or os.urandom(16)
    ek, mk = _kdf(pin, salt)
    return {"salt": salt, "ek": ek, "mk": mk}


def vault_seal(profiles, keys):
    nonce = os.urandom(16)
    pt = json.dumps(profiles).encode("utf-8")
    ct = bytes(a ^ b for a, b in zip(pt, _keystream(keys["ek"], nonce, len(pt))))
    head = f"gwd-vault-1:{VAULT_N}".encode()
    tag = hmac.new(keys["mk"], head + keys["salt"] + nonce + ct, hashlib.sha256).digest()
    return {"v": 1, "n": VAULT_N, "salt": _b64(keys["salt"]), "nonce": _b64(nonce),
            "data": _b64(ct), "tag": _b64(tag)}


def vault_open(v, pin):
    """Returns (profiles, keys). Raises ValueError for a wrong passcode."""
    salt, nonce = base64.b64decode(v["salt"]), base64.b64decode(v["nonce"])
    ct, tag = base64.b64decode(v["data"]), base64.b64decode(v["tag"])
    n = int(v.get("n", VAULT_N))
    ek, mk = _kdf(pin, salt, n)
    head = f"gwd-vault-{v.get('v', 1)}:{n}".encode()
    if not hmac.compare_digest(tag, hmac.new(mk, head + salt + nonce + ct, hashlib.sha256).digest()):
        raise ValueError("wrong passcode")
    pt = bytes(a ^ b for a, b in zip(ct, _keystream(ek, nonce, len(ct))))
    return json.loads(pt.decode("utf-8")), {"salt": salt, "ek": ek, "mk": mk}


def save_cfg(cfg):
    """Returns False if the settings could not be written (e.g. read-only drive)."""
    data = dict(cfg)
    if data.get("vault"):                      # never keep plain copies next to a vault
        for k in ("repo", "branch", "token"):
            data.pop(k, None)
    data.pop("remember_token", None)
    try:
        with open(CFG_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        return True
    except Exception:
        return False


# ----------------------------------------------------------------- GitHub API
class GitHub:
    def __init__(self, token):
        self.token = (token or "").strip()
        self.remaining = None

    def get(self, path, params=None):
        url = API + path
        if params:
            q = {k: v for k, v in params.items() if v not in (None, "")}
            if q:
                url += "?" + urllib.parse.urlencode(q)
        req = urllib.request.Request(url, headers={
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "gethub-watchdog",
        })
        if self.token:
            req.add_header("Authorization", "Bearer " + self.token)
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                self.remaining = r.headers.get("X-RateLimit-Remaining")
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "ignore")
            try:
                msg = json.loads(body).get("message", body)
            except Exception:
                msg = body[:200]
            if e.code == 404:
                msg = "Repository not found. Check the name — private repos need a token."
            elif e.code == 401:
                msg = "Token was rejected. Check that it is correct and not expired."
            elif e.code == 403 and "rate limit" in msg.lower():
                msg = "GitHub rate limit reached. Add a token or poll less often."
            raise RuntimeError(f"GitHub {e.code}: {msg}") from None
        except urllib.error.URLError as e:
            raise RuntimeError(f"Network error: {e.reason}") from None

    def get_text(self, path):
        """Download a plain-text resource such as a job log."""
        req = urllib.request.Request(API + path, headers={
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "gethub-watchdog",
        })
        if self.token:
            req.add_header("Authorization", "Bearer " + self.token)
        opener = urllib.request.build_opener(_NoRedirect)
        try:
            with opener.open(req, timeout=30) as r:
                return r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            loc = e.headers.get("Location") if e.headers else None
            if e.code in (301, 302, 303, 307, 308) and loc:
                try:
                    dl = urllib.request.Request(loc, headers={"User-Agent": "gethub-watchdog"})
                    with urllib.request.urlopen(dl, timeout=120) as r:
                        return r.read().decode("utf-8", "replace")
                except urllib.error.URLError as e2:
                    raise RuntimeError(f"Couldn't download the log: {getattr(e2, 'reason', e2)}") from None
            msg = {
                401: "Your token was rejected.",
                403: "Your token can't read build logs. Give it  Actions: Read-only  permission.",
                404: "GitHub has no log for this job yet — logs appear once the job has finished.",
                410: "This log has expired. GitHub only keeps build logs for a limited time.",
            }.get(e.code, f"GitHub error {e.code}.")
            raise RuntimeError(msg) from None
        except urllib.error.URLError as e:
            raise RuntimeError(f"Network error: {e.reason}") from None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class Watcher(threading.Thread):
    """Polls GitHub in the background and posts results to a queue."""

    def __init__(self, repo, token, branch, interval, out_q, kws):
        super().__init__(daemon=True)
        self.repo, self.branch, self.interval, self.q = repo, branch, interval, out_q
        self.kws = kws
        self.seen_active = set()    # run ids seen running (to detect "just finished")
        self.done_jobs = {}         # run id -> jobs, for finished runs that did not pass
        self.gh = GitHub(token)
        self.pinned = None          # run id the user clicked, or None = follow newest
        self.hist = {}              # workflow_id -> history estimate cache
        self._stop = threading.Event()
        self._wake = threading.Event()

    def stop(self):
        self._stop.set()
        self._wake.set()

    def poke(self):
        self._wake.set()

    def run(self):
        while not self._stop.is_set():
            try:
                data = self.poll()
                if not self._stop.is_set():
                    self.q.put(("data", data, self))
            except Exception as e:
                self.q.put(("error", str(e), self))
            self._wake.wait(self.interval)
            self._wake.clear()

    def poll(self):
        base = f"/repos/{self.repo}/actions"
        runs = self.gh.get(f"{base}/runs", {"per_page": 15, "branch": self.branch}).get("workflow_runs", [])

        target = None
        pinned = self.pinned
        if pinned:
            target = next((r for r in runs if r["id"] == pinned), None)
            if target is None:
                target = self.gh.get(f"{base}/runs/{pinned}")
        else:
            active = [r for r in runs if r.get("status") != "completed"]
            target = active[0] if active else (runs[0] if runs else None)

        jobs, hist = [], None
        if target:
            jobs = self.gh.get(f"{base}/runs/{target['id']}/jobs", {"per_page": 100}).get("jobs", [])
            if target.get("status") == "completed":
                self.done_jobs[target["id"]] = jobs
            try:
                hist = self.history(target["workflow_id"])
            except Exception:
                hist = None

        check = list(runs)
        if target and all(r["id"] != target["id"] for r in check):
            check.append(target)
        finished, fetched = [], 0
        for r in check:
            rid = r["id"]
            if r.get("status") != "completed":
                self.seen_active.add(rid)
                continue
            just_done = rid in self.seen_active
            # failed runs need their jobs to tell a compile failure from a publish failure
            needs = r.get("conclusion") in BAD and rid not in self.done_jobs
            if needs and (just_done or fetched < 3):
                try:
                    self.done_jobs[rid] = self.gh.get(f"{base}/runs/{rid}/jobs",
                                                      {"per_page": 100}).get("jobs", [])
                    fetched += 1
                except Exception:
                    pass
            if just_done:
                self.seen_active.discard(rid)
                finished.append(r)
        return {"repo": self.repo, "runs": runs, "target": target, "jobs": jobs,
                "hist": hist, "finished": finished, "done_jobs": dict(self.done_jobs),
                "remaining": self.gh.remaining, "at": time.time()}

    def history(self, wid):
        """Median durations from recent successful runs of the same workflow."""
        c = self.hist.get(wid)
        if c and time.time() - c["at"] < 300:
            return c
        base = f"/repos/{self.repo}/actions"
        runs = self.gh.get(f"{base}/workflows/{wid}/runs",
                           {"status": "success", "per_page": 10}).get("workflow_runs", [])
        durs = []
        for r in runs:
            s, e = parse_t(r.get("run_started_at")), parse_t(r.get("updated_at"))
            if s and e and e > s:
                durs.append((e - s).total_seconds())
        job_d, step_d = {}, {}
        for r in runs[:3]:
            for j in self.gh.get(f"{base}/runs/{r['id']}/jobs", {"per_page": 100}).get("jobs", []):
                s, e = parse_t(j.get("started_at")), parse_t(j.get("completed_at"))
                if s and e and e >= s:
                    job_d.setdefault(j["name"], []).append((e - s).total_seconds())
                for st in j.get("steps") or []:
                    s2, e2 = parse_t(st.get("started_at")), parse_t(st.get("completed_at"))
                    if s2 and e2 and e2 >= s2:
                        step_d.setdefault((j["name"], st["name"]), []).append((e2 - s2).total_seconds())
        c = {
            "at": time.time(),
            "run": median(durs) if durs else None,
            "count": len(durs),
            "jobs": {k: median(v) for k, v in job_d.items()},
            "steps": {k: median(v) for k, v in step_d.items()},
        }
        self.hist[wid] = c
        return c


# ----------------------------------------------------------------- alarm
def play_once(success):
    try:
        if sys.platform.startswith("win"):
            import winsound
            notes = [(784, 160), (988, 160), (1319, 320)] if success else [(523, 300), (392, 300), (262, 520)]
            for f, d in notes:
                winsound.Beep(f, d)
            return
        if sys.platform == "darwin":
            snd = "/System/Library/Sounds/Glass.aiff" if success else "/System/Library/Sounds/Basso.aiff"
            subprocess.run(["afplay", snd], check=False)
            return
        candidates = [
            ("paplay", "/usr/share/sounds/freedesktop/stereo/complete.oga"),
            ("paplay", "/usr/share/sounds/freedesktop/stereo/bell.oga"),
            ("aplay", "/usr/share/sounds/alsa/Front_Center.wav"),
        ]
        for player, f in candidates:
            if shutil.which(player) and os.path.exists(f):
                subprocess.run([player, f], check=False)
                return
        if sys.stdout:
            sys.stdout.write("\a")
            sys.stdout.flush()
    except Exception:
        pass


class Alarm:
    def __init__(self):
        self._evt = threading.Event()

    def start(self, success, duration=60):
        self.stop()
        evt = self._evt = threading.Event()

        def loop():
            end = time.time() + duration
            while not evt.is_set() and time.time() < end:
                play_once(success)
                evt.wait(1.0)

        threading.Thread(target=loop, daemon=True).start()

    def stop(self):
        self._evt.set()


# ----------------------------------------------------------------- UI
class App:
    def __init__(self, root):
        self.root = root
        root.title(APP)
        root.geometry("1200x780")
        root.minsize(920, 600)
        self.cfg = load_cfg()
        self.q = queue.Queue()
        self.watcher = None
        self.alarm = Alarm()
        self.data = None
        self.kws = [k.lower() for k in self.cfg.get("publish_keywords", PUBLISH_KEYWORDS) if k]
        self.shown_run = None
        self.auto_opened = set()
        self.log_cache = {}         # job id -> parsed log
        self.profiles = []          # saved repos: {"repo", "branch", "token"} (only while unlocked)
        self.vkeys = None           # vault keys while unlocked
        self.last_ok = None
        self.build_ui()
        root.after(200, self.drain)
        if self.cfg.get("vault") and "--selftest" not in sys.argv:
            root.after(300, self.unlock)
        elif self.cfg.get("token") and "--selftest" not in sys.argv:
            root.after(300, self.offer_protect)
        root.after(1000, self.tick)
        root.protocol("WM_DELETE_WINDOW", self.on_close)

    # ---------- layout
    def build_ui(self):
        r = self.root
        st = ttk.Style()
        try:
            st.theme_use("clam")
        except tk.TclError:
            pass
        st.configure("Treeview", rowheight=24)
        st.configure("Big.TLabel", font=("Segoe UI", 16, "bold"))
        st.configure("Stat.TLabel", font=("Segoe UI", 14, "bold"))
        st.configure("Cap.TLabel", foreground="#6e7781")
        st.configure("H.TLabel", font=("Segoe UI", 10, "bold"))
        st.configure("TProgressbar", background="#0969da", troughcolor="#eaeef2",
                     bordercolor="#d0d7de", lightcolor="#0969da", darkcolor="#0969da", thickness=14)

        # --- saved repositories (locked vault)
        pb = ttk.Frame(r, padding=(10, 8, 10, 0))
        pb.pack(fill="x")
        ttk.Label(pb, text="Saved repos", style="H.TLabel").pack(side="left")
        self.prof_v = tk.StringVar()
        self.prof_cb = ttk.Combobox(pb, textvariable=self.prof_v, state="readonly", width=44)
        self.prof_cb.pack(side="left", padx=(8, 8))
        self.prof_cb.bind("<<ComboboxSelected>>", self.on_profile)
        self.vbtn = {}
        for key, txt, cmd in (("save", "Save", self.save_profile), ("del", "Remove", self.delete_profile),
                              ("lock", "Lock", self.lock), ("pin", "Change passcode", self.change_pin)):
            b = ttk.Button(pb, text=txt, command=cmd)
            b.pack(side="left", padx=(0, 6))
            self.vbtn[key] = b
        self.vault_l = ttk.Label(pb, text="", style="Cap.TLabel")
        self.vault_l.pack(side="left", padx=(8, 0))

        # --- top controls
        top = ttk.Frame(r, padding=(10, 8))
        top.pack(fill="x")
        self.repo_v = tk.StringVar(value=self.cfg.get("repo", ""))
        self.branch_v = tk.StringVar(value=self.cfg.get("branch", ""))
        self.token_v = tk.StringVar(value=self.cfg.get("token", ""))
        self.interval_v = tk.StringVar(value=str(self.cfg.get("interval", 5)))
        self.follow_v = tk.BooleanVar(value=True)
        self.sound_v = tk.BooleanVar(value=self.cfg.get("sound", True))

        fields = [
            ("Repository (owner/name or URL)", self.repo_v, 30, None),
            ("Branch (optional)", self.branch_v, 14, None),
            ("Token (for private repos)", self.token_v, 22, "•"),
        ]
        for i, (cap, var, w, show) in enumerate(fields):
            ttk.Label(top, text=cap, style="Cap.TLabel").grid(row=0, column=i, sticky="w")
            e = ttk.Entry(top, textvariable=var, width=w, show=show or "")
            e.grid(row=1, column=i, padx=(0, 10), sticky="w")
            if i == 0:
                e.bind("<Return>", lambda _e: self.toggle())
        ttk.Label(top, text="Check every (sec)", style="Cap.TLabel").grid(row=0, column=3, sticky="w")
        ttk.Spinbox(top, from_=3, to=600, textvariable=self.interval_v, width=6).grid(row=1, column=3, padx=(0, 10))
        self.start_btn = ttk.Button(top, text="▶  Start watching", command=self.toggle)
        self.start_btn.grid(row=1, column=4, padx=(0, 12))
        ttk.Checkbutton(top, text="Alarm sound", variable=self.sound_v).grid(row=1, column=5, padx=(0, 8))
        ttk.Button(top, text="Test alarm", command=self.test_alarm).grid(row=1, column=6)

        # --- status bar (packed before the panes so it stays visible)
        self.sb = ttk.Label(r, anchor="w", padding=(10, 3), style="Cap.TLabel",
                            text="Enter a repository and press Start."
                                 + ("   ·   Portable mode: settings are saved next to the app, not on this PC."
                                    if PORTABLE else ""))
        self.refresh_vault_ui()
        self.sb.pack(fill="x", side="bottom")

        pw = ttk.Panedwindow(r, orient="horizontal")
        pw.pack(fill="both", expand=True, padx=10, pady=(0, 4))
        left, right = ttk.Frame(pw), ttk.Frame(pw)
        pw.add(left, weight=1)
        pw.add(right, weight=3)

        # --- left: build list
        lh = ttk.Frame(left)
        lh.pack(fill="x")
        ttk.Label(lh, text="Recent builds", style="H.TLabel").pack(side="left")
        ttk.Checkbutton(lh, text="Follow newest", variable=self.follow_v,
                        command=self.on_follow).pack(side="right")
        self.runs_tv = ttk.Treeview(left, columns=("wf", "status", "time"), show="headings", selectmode="browse")
        for col, txt, w in (("wf", "Workflow", 150), ("status", "Status", 140), ("time", "Time", 62)):
            self.runs_tv.heading(col, text=txt)
            self.runs_tv.column(col, width=w, anchor="w" if col != "time" else "e")
        self.runs_tv.pack(fill="both", expand=True, pady=(4, 0))
        self.runs_tv.bind("<ButtonRelease-1>", self.on_pick)
        for k, c in COLORS.items():
            self.runs_tv.tag_configure(k, foreground=c)

        # --- right: header
        hdr = ttk.Frame(right, padding=(10, 0, 0, 0))
        hdr.pack(fill="x")
        row0 = ttk.Frame(hdr)
        row0.pack(fill="x")
        self.title_l = ttk.Label(row0, text="Not watching yet", style="Big.TLabel")
        self.title_l.pack(side="left")
        self.status_l = tk.Label(row0, text="", font=("Segoe UI", 14, "bold"))
        self.status_l.pack(side="right")
        self.sub_l = ttk.Label(hdr, text="", style="Cap.TLabel")
        self.sub_l.pack(fill="x", pady=(2, 8))

        stats = ttk.Frame(hdr)
        stats.pack(fill="x")
        self.stat = {}
        for i, (k, cap) in enumerate([("elapsed", "Elapsed"), ("typical", "Typical build"),
                                      ("remaining", "Time to completion"), ("done", "Done at")]):
            f = ttk.Frame(stats)
            f.grid(row=0, column=i, sticky="w", padx=(0, 34))
            ttk.Label(f, text=cap, style="Cap.TLabel").pack(anchor="w")
            v = ttk.Label(f, text="—", style="Stat.TLabel")
            v.pack(anchor="w")
            self.stat[k] = v

        self.prog = ttk.Progressbar(hdr, maximum=1000, mode="determinate")
        self.prog.pack(fill="x", pady=(10, 2))
        prow = ttk.Frame(hdr)
        prow.pack(fill="x")
        self.prog_l = ttk.Label(prow, text="", style="Cap.TLabel")
        self.prog_l.pack(side="left")
        ttk.Button(prow, text="Open on GitHub ↗", command=self.open_run).pack(side="right")
        ttk.Button(prow, text="View log", command=self.open_log).pack(side="right", padx=(0, 6))

        # --- right: timeline
        tlf = ttk.LabelFrame(right, text=" Build flow ", padding=4)
        tlf.pack(fill="x", padx=(10, 0), pady=8)
        self.canvas = tk.Canvas(tlf, height=150, bg="white", highlightthickness=0)
        tl_sb = ttk.Scrollbar(tlf, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=tl_sb.set)
        tl_sb.pack(side="right", fill="y")
        self.canvas.pack(fill="x", expand=True)
        self.canvas.bind("<Configure>", lambda _e: self.draw_timeline())

        # --- right: jobs & steps
        jf = ttk.LabelFrame(right, text=" Jobs & steps  (double-click a job or step to see its log) ", padding=4)
        jf.pack(fill="both", expand=True, padx=(10, 0))
        self.jobs_tv = ttk.Treeview(jf, columns=("status", "start", "dur", "eta"), show="tree headings")
        self.jobs_tv.heading("#0", text="Job / step")
        self.jobs_tv.column("#0", width=330)
        for col, txt, w in (("status", "Status", 128), ("start", "Started", 100),
                            ("dur", "Duration", 80), ("eta", "Estimate", 140)):
            self.jobs_tv.heading(col, text=txt)
            self.jobs_tv.column(col, width=w, anchor="w")
        jsb = ttk.Scrollbar(jf, orient="vertical", command=self.jobs_tv.yview)
        self.jobs_tv.configure(yscrollcommand=jsb.set)
        jsb.pack(side="right", fill="y")
        self.jobs_tv.pack(fill="both", expand=True)
        self.jobs_tv.bind("<Double-1>", self.on_job_dclick)
        for k, c in COLORS.items():
            self.jobs_tv.tag_configure(k, foreground=c)

    # ---------- controls
    def toggle(self):
        if self.watcher:
            self.stop_watch()
            return
        repo = normalize_repo(self.repo_v.get())
        if not repo:
            messagebox.showerror(APP, "Enter a repository like  owner/name  or paste its GitHub URL.")
            return
        token = "".join(self.token_v.get().split())  # drop spaces / line breaks from pasting
        self.token_v.set(token)
        if token and not token.startswith(("github_pat_", "ghp_", "gho_", "ghu_", "ghs_")):
            if token.startswith("sk-ant"):
                hint = "That looks like a Claude/Anthropic API key, not a GitHub token."
            elif token.startswith(("ssh-", "-----BEGIN")):
                hint = "That looks like an SSH key, not a GitHub token."
            else:
                hint = "GitHub tokens start with  github_pat_  or  ghp_ ."
            messagebox.showerror(APP, f"{hint}\n\nCreate one at github.com/settings/tokens and paste it here.")
            return
        try:
            iv = max(3, int(self.interval_v.get()))
        except ValueError:
            iv = 5
        note = ""
        if not token and iv < 120:
            iv = 120
            note = "  (No token: GitHub allows only 60 requests/hour, so checking every 2 min. Add a token for live updates.)"
        self.interval_v.set(str(iv))
        self.repo_v.set(repo)
        self.cfg.update(interval=iv, sound=self.sound_v.get(), publish_keywords=self.kws)
        if not self.cfg.get("vault"):
            self.cfg.pop("token", None)        # tokens are only ever saved inside the vault
        save_cfg(self.cfg)
        self.update_saved_token(repo, self.branch_v.get().strip(), token)

        self.data = None
        self.follow_v.set(True)
        self.watcher = Watcher(repo, token, self.branch_v.get().strip(), iv, self.q, self.kws)
        self.watcher.start()
        self.start_btn.config(text="■  Stop watching")
        self.title_l.config(text=f"Connecting to {repo}…")
        self.set_status(f"Watching {repo}…{note}")

    def stop_watch(self):
        if self.watcher:
            self.watcher.stop()
        self.watcher = None
        self.start_btn.config(text="▶  Start watching")
        self.set_status("Stopped.")

    def on_follow(self):
        if self.watcher and self.follow_v.get():
            self.watcher.pinned = None
            self.watcher.poke()

    def on_pick(self, e):
        iid = self.runs_tv.identify_row(e.y)
        if not iid or not self.watcher:
            return
        self.follow_v.set(False)
        self.watcher.pinned = int(iid)
        self.watcher.poke()
        self.set_status("Loading selected build…")

    # ---------- saved repos / vault
    @staticmethod
    def profile_label(p):
        return p["repo"] + (f"  [{p['branch']}]" if p.get("branch") else "")

    def refresh_vault_ui(self, select=None):
        has_vault = bool(self.cfg.get("vault"))
        unlocked = self.vkeys is not None
        labels = [self.profile_label(p) for p in self.profiles]
        self.prof_cb.config(values=labels)
        if select is not None and select in labels:
            self.prof_v.set(select)
        elif self.prof_v.get() not in labels:
            self.prof_v.set("")
        if not has_vault:
            txt = "Press Save to keep this repo and token in a passcode-locked vault."
        elif not unlocked:
            txt = "Locked."
        else:
            txt = f"Unlocked · {len(self.profiles)} saved"
        self.vault_l.config(text=txt)
        self.vbtn["save"].config(text="Unlock" if has_vault and not unlocked else "Save")
        for k in ("del", "lock", "pin"):
            self.vbtn[k].state(["!disabled"] if unlocked else ["disabled"])

    def write_vault(self):
        self.cfg["vault"] = vault_seal(self.profiles, self.vkeys)
        for k in ("repo", "branch", "token"):
            self.cfg.pop(k, None)
        if not save_cfg(self.cfg):
            messagebox.showwarning(APP, "Couldn't save to this drive (it may be read-only). "
                                        "Your saved repos will be lost when you close the app.")

    def unlock(self):
        v = self.cfg.get("vault")
        if not v:
            return True
        if self.vkeys is not None:
            return True

        def check(pin):
            try:
                profiles, keys = vault_open(v, pin)
            except ValueError:
                fails = int(self.cfg.get("vault_fails", 0)) + 1
                if fails >= MAX_TRIES:
                    self.cfg.pop("vault", None)
                    self.cfg["vault_fails"] = 0
                    save_cfg(self.cfg)
                    dlg.action = "wiped"
                    dlg.w.after(10, dlg.w.destroy)
                    return "Too many wrong passcodes."
                self.cfg["vault_fails"] = fails
                save_cfg(self.cfg)
                left = MAX_TRIES - fails
                return ("Wrong passcode." + (f"  {left} {'try' if left == 1 else 'tries'} left before the vault is erased."
                                             if left <= 7 else ""))
            except Exception as e:
                return f"The vault could not be read ({e})."
            self.profiles, self.vkeys = profiles, keys
            self.cfg["vault_fails"] = 0
            save_cfg(self.cfg)
            return None

        dlg = PasscodeDialog(self.root, "Unlock GetHub WatchDog",
                             "Enter your passcode to unlock your saved repositories and tokens.")
        dlg.check = check
        pin = dlg.wait()
        if dlg.action == "wiped":
            self.profiles, self.vkeys = [], None
            messagebox.showwarning(APP, f"{MAX_TRIES} wrong passcodes in a row, so the vault was erased "
                                        "to protect your tokens. Nothing else was changed.")
        elif dlg.action == "reset":
            self.cfg.pop("vault", None)
            self.cfg["vault_fails"] = 0
            save_cfg(self.cfg)
            self.profiles, self.vkeys = [], None
            self.set_status("Vault erased. Paste a token and press Save to make a new one.")
        self.refresh_vault_ui()
        if pin and self.profiles:
            self.prof_v.set(self.profile_label(self.profiles[0]))
            self.on_profile()
        return pin is not None

    def offer_protect(self):
        """Older versions saved the token as plain text. Offer to lock it away."""
        if messagebox.askyesno(APP, "Your token is saved on this drive without protection.\n\n"
                                    "Lock it in a passcode-protected vault now?"):
            self.save_profile()
        else:
            self.cfg.pop("token", None)
            save_cfg(self.cfg)
            self.set_status("The unprotected copy of your token was removed. Press Save to keep it in the vault.")

    def new_vault(self):
        dlg = PasscodeDialog(self.root, "Create a passcode",
                             "Choose a passcode. You'll need it each time you open GetHub WatchDog "
                             "to use your saved repositories and tokens.", mode="new")
        pin = dlg.wait()
        if not pin:
            return False
        self.root.config(cursor="watch")
        self.root.update()
        self.vkeys = vault_keys(pin)
        self.root.config(cursor="")
        self.profiles = []
        return True

    def save_profile(self):
        if self.cfg.get("vault") and self.vkeys is None:
            self.unlock()
            return
        repo = normalize_repo(self.repo_v.get())
        if not repo:
            messagebox.showerror(APP, "Enter a repository first (owner/name).")
            return
        if self.vkeys is None and not self.new_vault():
            return
        p = {"repo": repo, "branch": self.branch_v.get().strip(),
             "token": "".join(self.token_v.get().split())}
        self.profiles = [x for x in self.profiles
                         if (x["repo"].lower(), x.get("branch", "")) != (repo.lower(), p["branch"])]
        self.profiles.append(p)
        self.profiles.sort(key=lambda x: self.profile_label(x).lower())
        self.write_vault()
        self.refresh_vault_ui(select=self.profile_label(p))
        self.set_status(f"Saved {repo} in your locked vault.")

    def update_saved_token(self, repo, branch, token):
        if self.vkeys is None:
            return
        for p in self.profiles:
            if (p["repo"].lower(), p.get("branch", "")) == (repo.lower(), branch) and p.get("token") != token:
                p["token"] = token
                self.write_vault()

    def on_profile(self, _e=None):
        p = next((x for x in self.profiles if self.profile_label(x) == self.prof_v.get()), None)
        if not p:
            return
        if self.watcher:
            self.stop_watch()
        self.repo_v.set(p["repo"])
        self.branch_v.set(p.get("branch", ""))
        self.token_v.set(p.get("token", ""))
        self.set_status(f"Loaded {p['repo']}. Press Start watching.")

    def delete_profile(self):
        label = self.prof_v.get()
        if not label or self.vkeys is None:
            return
        if not messagebox.askyesno(APP, f"Remove {label} from your saved repos?"):
            return
        self.profiles = [x for x in self.profiles if self.profile_label(x) != label]
        self.write_vault()
        self.refresh_vault_ui()

    def lock(self):
        if self.watcher:
            self.stop_watch()
        self.profiles, self.vkeys = [], None
        self.token_v.set("")
        self.repo_v.set("")
        self.branch_v.set("")
        self.refresh_vault_ui()
        self.set_status("Locked. Your saved repos and tokens are hidden until you enter the passcode.")
        self.unlock()

    def change_pin(self):
        if self.vkeys is None:
            return
        dlg = PasscodeDialog(self.root, "Change passcode", "Enter a new passcode.", mode="new")
        pin = dlg.wait()
        if not pin:
            return
        self.vkeys = vault_keys(pin)
        self.write_vault()
        self.set_status("Passcode changed.")

    def current_token(self):
        return "".join(self.token_v.get().split())

    def on_job_dclick(self, e):
        iid = self.jobs_tv.identify_row(e.y)
        if not iid.startswith("j"):
            return
        jid, _, sno = iid[1:].partition("s")
        self.open_log(int(jid), int(sno) if sno else None)
        return "break"

    def open_log(self, job_id=None, step_no=None, run=None, jobs=None):
        d = self.data
        if not d:
            return
        run = run or d.get("target")
        if not run:
            return
        if jobs is None:
            jobs = (d.get("done_jobs") or {}).get(run["id"])
            if jobs is None and d.get("target") and d["target"]["id"] == run["id"]:
                jobs = d.get("jobs")
        if not jobs:
            messagebox.showinfo(APP, "This build has no jobs with logs yet.")
            return
        LogWindow(self, d["repo"], run, jobs, job_id, step_no)

    def open_run(self):
        t = self.data and self.data.get("target")
        if t:
            webbrowser.open(t["html_url"])

    def test_alarm(self):
        self.alarm.start(True, duration=4)

    def set_status(self, text, error=False):
        self.sb.config(text=text, foreground=COLORS["bad"] if error else "#6e7781")

    def on_close(self):
        self.alarm.stop()
        if self.watcher:
            self.watcher.stop()
        self.cfg.update(sound=self.sound_v.get())
        save_cfg(self.cfg)
        self.root.destroy()

    # ---------- data flow
    def drain(self):
        try:
            while True:
                kind, payload, src = self.q.get_nowait()
                if src is not self.watcher:
                    continue
                if kind == "data":
                    self.on_data(payload)
                else:
                    self.set_status("⚠ " + payload, error=True)
                    if not self.data:
                        self.title_l.config(text="Couldn't load builds")
        except queue.Empty:
            pass
        self.root.after(200, self.drain)

    def on_data(self, d):
        self.data = d
        self.last_ok = time.time()
        for r in d.get("finished") or []:
            self.build_done(r)
        self.fill_runs()
        self.refresh_live()
        self.update_statusbar()

    def update_statusbar(self):
        d = self.data
        if not d or not self.watcher:
            return
        ago = int(time.time() - d["at"])
        rem = d.get("remaining") or "?"
        active = sum(1 for r in d["runs"] if r.get("status") != "completed")
        self.set_status(f"{d['repo']} · {active} build(s) running · updated {ago}s ago · "
                        f"API requests left this hour: {rem}")

    def tick(self):
        try:
            if self.data:
                t = self.data.get("target")
                if t and t.get("status") != "completed":
                    self.refresh_live()
                    self.fill_runs_times()
                self.update_statusbar()
        finally:
            self.root.after(1000, self.tick)

    # ---------- rendering
    def run_verdict(self, r):
        d = self.data or {}
        jobs = (d.get("done_jobs") or {}).get(r["id"])
        t = d.get("target")
        if jobs is None and t and t["id"] == r["id"] and r.get("status") == "completed":
            jobs = d.get("jobs")
        return verdict(r, jobs, self.kws)

    def fill_runs(self):
        tv = self.runs_tv
        tv.delete(*tv.get_children())
        for r in self.data["runs"]:
            txt, tag, _ = self.run_verdict(r)
            tv.insert("", "end", iid=str(r["id"]),
                      values=(f"{r.get('name', 'workflow')} #{r.get('run_number')}", txt, self.run_time(r)),
                      tags=(tag,))
        t = self.data.get("target")
        if t and tv.exists(str(t["id"])):
            tv.selection_set(str(t["id"]))
            tv.see(str(t["id"]))
        if not self.data["runs"]:
            tv.insert("", "end", values=("No Actions builds yet", "", ""), tags=("muted",))

    def fill_runs_times(self):
        for r in self.data["runs"]:
            iid = str(r["id"])
            if r.get("status") == "in_progress" and self.runs_tv.exists(iid):
                self.runs_tv.set(iid, "time", self.run_time(r))

    @staticmethod
    def run_time(r):
        s, e = run_span(r)
        if not s or r.get("status") in ("queued", "waiting", "pending", "requested"):
            return "—"
        return fmt_dur(((e or now()) - s).total_seconds())

    def refresh_live(self):
        d = self.data
        t = d.get("target")
        if not t:
            self.title_l.config(text="No builds found")
            self.sub_l.config(text="This repository has no GitHub Actions runs yet"
                                   + (" on that branch." if self.branch_v.get().strip() else "."))
            self.status_l.config(text="")
            for v in self.stat.values():
                v.config(text="—")
            self.prog["value"] = 0
            self.prog_l.config(text="")
            self.jobs_tv.delete(*self.jobs_tv.get_children())
            self.canvas.delete("all")
            return

        hist = d.get("hist") or {}
        txt, tag, kind = self.run_verdict(t)
        attempt = t.get("run_attempt") or 1
        self.title_l.config(text=f"{t.get('name', 'workflow')}  #{t.get('run_number')}"
                                 + (f"  (attempt {attempt})" if attempt > 1 else ""))
        msg = ((t.get("head_commit") or {}).get("message") or "").splitlines()
        actor = (t.get("triggering_actor") or t.get("actor") or {}).get("login", "")
        self.sub_l.config(text="  ·  ".join(x for x in [
            t.get("head_branch") or "", (t.get("head_sha") or "")[:7],
            (msg[0][:80] if msg else ""), (f"by {actor}" if actor else ""), t.get("event", "")] if x))
        self.status_l.config(text=txt, fg=COLORS[tag])

        s, e = run_span(t)
        running = t.get("status") != "completed"
        queued = t.get("status") in ("queued", "waiting", "pending", "requested")
        elapsed = ((e or now()) - s).total_seconds() if s and not queued else None
        typical = hist.get("run")
        jobs = d.get("jobs") or []
        jobs_done = sum(1 for j in jobs if j.get("status") == "completed")
        job_frac = jobs_done / len(jobs) if jobs else 0

        self.stat["elapsed"].config(text=fmt_dur(elapsed))
        self.stat["typical"].config(text=fmt_dur(typical) if typical else "unknown")

        if not running:
            remaining, done_at, frac = "done", local_clock(e), 1.0
            note = f"Finished in {fmt_dur(elapsed)}."
            if kind == "publish":
                note += "  Everything compiled — only the publish / release step failed."
        elif queued:
            remaining = "waiting for runner"
            done_at = local_clock(now() + timedelta(seconds=typical)) if typical else "—"
            frac, note = 0.0, "Build is queued — the clock starts when a runner picks it up."
        elif typical and elapsed is not None:
            rem = typical - elapsed
            if rem > 0:
                remaining = fmt_dur(rem)
                done_at = local_clock(now() + timedelta(seconds=rem))
                frac = min(elapsed / typical, 0.99)
            else:
                remaining = "any moment"
                done_at = f"+{fmt_dur(-rem)} over"
                frac = 0.99
            note = (f"{int(frac * 100)}% · estimate from the median of the last {hist.get('count')} "
                    f"successful build(s) of this workflow · {jobs_done}/{len(jobs)} jobs done")
        else:
            remaining, done_at, frac = "learning…", "—", job_frac
            note = (f"{jobs_done}/{len(jobs)} jobs done · no successful past builds of this workflow yet, "
                    f"so time estimates will appear after the first one passes.")
        self.stat["remaining"].config(text=remaining)
        self.stat["done"].config(text=done_at)
        self.prog["value"] = frac * 1000
        self.prog_l.config(text=note)

        self.sync_jobs()
        self.draw_timeline()

    def est_text(self, obj, est):
        st = obj.get("status")
        s, e = item_span(obj)
        if not est:
            return "—"
        if st == "in_progress" and s:
            rem = est - (now() - s).total_seconds()
            return f"≈ {fmt_dur(rem)} left" if rem > 0 else f"over by {fmt_dur(-rem)}"
        if st == "completed":
            return f"usually {fmt_dur(est)}"
        return f"≈ {fmt_dur(est)}"

    def row_vals(self, obj, est, parent_publish=False):
        txt, tag = item_status(obj, self.kws, parent_publish)
        s, e = item_span(obj)
        dur = ((e or now()) - s).total_seconds() if s else None
        return (txt, local_clock(s) if s else "—", fmt_dur(dur), self.est_text(obj, est)), tag

    def sync_jobs(self):
        tv = self.jobs_tv
        d = self.data
        t = d["target"]
        hist = d.get("hist") or {}
        if self.shown_run != t["id"]:
            tv.delete(*tv.get_children())
            self.shown_run = t["id"]
            self.auto_opened.clear()

        want = []
        for j in d.get("jobs") or []:
            jid = f"j{j['id']}"
            vals, tag = self.row_vals(j, hist.get("jobs", {}).get(j["name"]))
            want.append((jid, "", j["name"], vals, tag))
            jpub = is_publish(j.get("name"), self.kws)
            for stp in j.get("steps") or []:
                sid = f"{jid}s{stp['number']}"
                svals, stag = self.row_vals(stp, hist.get("steps", {}).get((j["name"], stp["name"])), jpub)
                want.append((sid, jid, stp["name"], svals, stag))

        existing = set()
        for jid in tv.get_children():
            existing.add(jid)
            existing.update(tv.get_children(jid))
        want_ids = set()
        for iid, parent, text, vals, tag in want:
            want_ids.add(iid)
            if tv.exists(iid):
                tv.item(iid, text=text, values=vals, tags=(tag,))
            else:
                tv.insert(parent, "end", iid=iid, text=text, values=vals, tags=(tag,))
        for iid in existing - want_ids:
            if tv.exists(iid):
                tv.delete(iid)
        # auto-expand jobs while they run so you can see the current step
        for j in d.get("jobs") or []:
            jid = f"j{j['id']}"
            if j.get("status") == "in_progress" and jid not in self.auto_opened and tv.exists(jid):
                tv.item(jid, open=True)
                self.auto_opened.add(jid)

    def draw_timeline(self):
        c = self.canvas
        c.delete("all")
        d = self.data
        if not d or not d.get("target"):
            return
        t, jobs, hist = d["target"], d.get("jobs") or [], d.get("hist") or {}
        rs, re_ = run_span(t)
        if not rs:
            return
        # a re-run keeps jobs from the earlier attempt, so start the axis at the earliest job
        starts = [item_span(j)[0] for j in jobs]
        rs = min([rs] + [x for x in starts if x])
        W = max(c.winfo_width(), 400)
        label_w, pad_r, row_h, top = 200, 70, 22, 28
        cur = re_ or now()

        ends = [cur]
        if hist.get("run"):
            ends.append(rs + timedelta(seconds=hist["run"]))
        for j in jobs:
            s, e = item_span(j)
            est = hist.get("jobs", {}).get(j["name"])
            if s and est:
                ends.append(s + timedelta(seconds=est))
            if e:
                ends.append(e)
        span = max((max(ends) - rs).total_seconds(), 1) * 1.04
        plot_w = W - label_w - pad_r

        def x(dt):
            return label_w + max(0.0, (dt - rs).total_seconds()) / span * plot_w

        height = top + max(len(jobs), 1) * row_h + 10

        # axis
        step = next((s for s in (5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200)
                     if span / s <= 8), 7200)
        k = 0
        while k * step <= span:
            xx = label_w + k * step / span * plot_w
            c.create_line(xx, top - 6, xx, height, fill="#eaeef2")
            c.create_text(xx, 10, text=fmt_dur(k * step), fill="#6e7781", font=("Segoe UI", 8))
            k += 1

        for i, j in enumerate(jobs):
            y = top + i * row_h
            name = j["name"] if len(j["name"]) <= 28 else j["name"][:27] + "…"
            c.create_text(8, y + row_h / 2, text=name, anchor="w", font=("Segoe UI", 9))
            txt, tag = item_status(j, self.kws)
            s, e = item_span(j)
            if not s:
                c.create_text(label_w + 4, y + row_h / 2, text="queued…", anchor="w",
                              fill=COLORS["wait"], font=("Segoe UI", 8, "italic"))
                continue
            est = hist.get("jobs", {}).get(j["name"])
            if est:
                c.create_rectangle(x(s), y + 4, x(s + timedelta(seconds=est)), y + row_h - 4,
                                   outline="#afb8c1", dash=(3, 2))
            x1 = x(s)
            x2 = max(x(e or now()), x1 + 3)
            c.create_rectangle(x1, y + 5, x2, y + row_h - 5, fill=COLORS[tag], outline="")
            dur = ((e or now()) - s).total_seconds()
            c.create_text(x2 + 5, y + row_h / 2, text=fmt_dur(dur), anchor="w",
                          fill="#57606a", font=("Segoe UI", 8))

        if hist.get("run"):
            xe = x(rs + timedelta(seconds=hist["run"]))
            c.create_line(xe, top - 6, xe, height, fill="#8250df", dash=(4, 3))
            c.create_text(xe, height - 2, text="expected finish", anchor="se",
                          fill="#8250df", font=("Segoe UI", 8))
        if not re_:
            xn = x(now())
            c.create_line(xn, top - 6, xn, height, fill=COLORS["bad"])
            c.create_text(xn + 3, top - 4, text="now", anchor="w", fill=COLORS["bad"],
                          font=("Segoe UI", 8, "bold"))
        if not jobs:
            c.create_text(label_w, top + 10, text="Waiting for jobs to start…", anchor="w",
                          fill="#6e7781", font=("Segoe UI", 9, "italic"))

        c.configure(height=min(height, 260), scrollregion=(0, 0, W, height))

    # ---------- completion
    def build_done(self, r):
        txt, tag, kind = self.run_verdict(r)
        built = kind in ("pass", "publish")
        s, e = run_span(r)
        dur = (e - s).total_seconds() if s and e else None
        if self.sound_v.get():
            self.alarm.start(built)
        self.root.title(f"{APP} — {txt}")
        self.popup(r, kind, dur, e)

    def popup(self, r, kind, dur, end):
        w = tk.Toplevel(self.root)
        w.title("Build finished")
        w.resizable(False, False)
        try:
            w.attributes("-topmost", True)
        except tk.TclError:
            pass
        if kind == "pass":
            color, head, extra = COLORS["ok"], "✔  BUILD PASSED", ""
        elif kind == "publish":
            color, head = COLORS["warn"], "✔  BUILD PASSED"
            extra = "\nThe code compiled. Only the publish / release step failed."
        else:
            color = COLORS["bad"] if kind == "fail" else COLORS["muted"]
            head = f"✖  BUILD {str(r.get('conclusion', 'failed')).upper().replace('_', ' ')}"
            extra = ""
        tk.Label(w, text=head, font=("Segoe UI", 20, "bold"), fg="white", bg=color,
                 padx=40, pady=18).pack(fill="x")
        ttk.Label(w, padding=16, justify="center", text=(
            f"{r.get('name')}  #{r.get('run_number')}  on  {r.get('head_branch')}\n"
            f"Finished in {fmt_dur(dur)} at {local_clock(end)}{extra}")).pack()

        def close():
            self.alarm.stop()
            self.root.title(APP)
            w.destroy()

        btns = ttk.Frame(w, padding=(16, 0, 16, 16))
        btns.pack()
        ttk.Button(btns, text="Stop alarm", command=close).pack(side="left", padx=6)
        if kind != "pass":
            jobs = ((self.data or {}).get("done_jobs") or {}).get(r["id"])
            ttk.Button(btns, text="Show failure log",
                       command=lambda: (close(), self.open_log(run=r, jobs=jobs))).pack(side="left", padx=6)
        ttk.Button(btns, text="Open on GitHub ↗",
                   command=lambda: (webbrowser.open(r["html_url"]), close())).pack(side="left", padx=6)
        w.protocol("WM_DELETE_WINDOW", close)

        try:
            self.root.deiconify()
            self.root.lift()
            w.update_idletasks()
            px = self.root.winfo_rootx() + (self.root.winfo_width() - w.winfo_width()) // 2
            py = self.root.winfo_rooty() + (self.root.winfo_height() - w.winfo_height()) // 3
            w.geometry(f"+{max(px, 0)}+{max(py, 0)}")
            w.lift()
            w.focus_force()
        except tk.TclError:
            pass


class PasscodeDialog:
    """Modal passcode prompt. mode: 'unlock', 'new' (enter twice) or 'confirm'."""

    def __init__(self, parent, title, message, mode="unlock", note=""):
        self.result = None
        self.action = None
        w = self.w = tk.Toplevel(parent)
        w.title(title)
        w.resizable(False, False)
        w.transient(parent)
        f = ttk.Frame(w, padding=18)
        f.pack(fill="both", expand=True)
        ttk.Label(f, text=message, wraplength=360, justify="left").pack(anchor="w")
        self.p1, self.p2 = tk.StringVar(), tk.StringVar()
        ttk.Label(f, text="Passcode", style="Cap.TLabel").pack(anchor="w", pady=(12, 0))
        e1 = ttk.Entry(f, textvariable=self.p1, show="•", width=24, font=("Segoe UI", 14))
        e1.pack(anchor="w")
        if mode == "new":
            ttk.Label(f, text="Type it again", style="Cap.TLabel").pack(anchor="w", pady=(8, 0))
            e2 = ttk.Entry(f, textvariable=self.p2, show="•", width=24, font=("Segoe UI", 14))
            e2.pack(anchor="w")
            e2.bind("<Return>", lambda _e: self.ok())
            note = note or ("At least 4 digits. A longer code (6–8 digits, or a few words) "
                            "is much harder to crack if someone copies your drive.")
        else:
            e1.bind("<Return>", lambda _e: self.ok())
        self.msg = ttk.Label(f, text=note, style="Cap.TLabel", wraplength=360, justify="left")
        self.msg.pack(anchor="w", pady=(8, 0))
        self.mode = mode
        b = ttk.Frame(f)
        b.pack(fill="x", pady=(14, 0))
        ttk.Button(b, text="Unlock" if mode == "unlock" else "OK", command=self.ok).pack(side="right")
        ttk.Button(b, text="Cancel" if mode != "unlock" else "Skip for now",
                   command=self.cancel).pack(side="right", padx=6)
        if mode == "unlock":
            ttk.Button(b, text="Forgot passcode…", command=self.forgot).pack(side="left")
        w.protocol("WM_DELETE_WINDOW", self.cancel)
        w.update_idletasks()
        try:
            px = parent.winfo_rootx() + (parent.winfo_width() - w.winfo_width()) // 2
            py = parent.winfo_rooty() + (parent.winfo_height() - w.winfo_height()) // 3
            w.geometry(f"+{max(px, 0)}+{max(py, 0)}")
        except tk.TclError:
            pass
        self.check = None          # set by caller for unlock: pin -> error text or None
        e1.focus_set()
        w.grab_set()

    def set_message(self, text, error=False):
        self.msg.config(text=text, foreground=COLORS["bad"] if error else "#6e7781")

    def ok(self):
        pin = self.p1.get()
        if self.mode == "new":
            if len(pin) < 4:
                return self.set_message("Use at least 4 characters (for example 4 digits).", True)
            if pin != self.p2.get():
                return self.set_message("The two passcodes don't match. Try again.", True)
        if not pin:
            return
        if self.check:
            self.set_message("Checking…")
            self.w.config(cursor="watch")
            self.w.update()
            err = self.check(pin)
            self.w.config(cursor="")
            if err:
                self.p1.set("")
                return self.set_message(err, True)
        self.result = pin
        self.w.destroy()

    def cancel(self):
        self.w.destroy()

    def forgot(self):
        if messagebox.askyesno(APP, "Without the passcode your saved repositories and tokens "
                               "can't be recovered.\n\nErase the vault and start again? "
                               "(Your tokens still work on GitHub — you can paste them again.)",
                               icon="warning", parent=self.w):
            self.action = "reset"
            self.w.destroy()

    def wait(self):
        self.w.wait_window()
        return self.result


class LogWindow:
    """Shows the GitHub log for a job, focused on the step that failed."""

    def __init__(self, app, repo, run, jobs, job_id=None, step_no=None):
        self.app, self.repo, self.run = app, repo, run
        self.cache = app.log_cache
        self.gh = GitHub(app.current_token())
        self.q = queue.Queue()
        self.step_no = step_no
        self.sel = []
        self.list_lines = []
        failed = [j for j in jobs if j.get("conclusion") in BAD]
        self.jobs = failed + [j for j in jobs if j not in failed]
        self.job = next((j for j in self.jobs if j["id"] == job_id), None) or self.jobs[0]

        w = self.w = tk.Toplevel(app.root)
        w.title(f"{APP} — log — {run.get('name')} #{run.get('run_number')}")
        w.geometry("1150x760")
        w.minsize(760, 480)
        mono = ("Consolas", 10) if sys.platform.startswith("win") else \
               ("Menlo", 11) if sys.platform == "darwin" else ("DejaVu Sans Mono", 10)

        bar = ttk.Frame(w, padding=(10, 8, 10, 4))
        bar.pack(fill="x")
        ttk.Label(bar, text="Job").pack(side="left")
        self.labels = {}
        for j in self.jobs:
            mark = "✖ " if j.get("conclusion") in BAD else "✔ " if j.get("conclusion") == "success" else "• "
            self.labels[mark + j["name"]] = j
        self.job_v = tk.StringVar()
        cb = ttk.Combobox(bar, textvariable=self.job_v, values=list(self.labels), state="readonly", width=44)
        cb.pack(side="left", padx=(6, 14))
        cb.bind("<<ComboboxSelected>>", self.on_job)
        self.mode = tk.StringVar(value="job")   # full job log by default
        ttk.Radiobutton(bar, text="Whole job log", value="job", variable=self.mode,
                        command=self.render).pack(side="left")
        ttk.Radiobutton(bar, text="Failed step only", value="step", variable=self.mode,
                        command=self.render).pack(side="left", padx=(6, 14))
        self.only_err = tk.BooleanVar(value=False)
        ttk.Checkbutton(bar, text="Errors & warnings only", variable=self.only_err,
                        command=self.render).pack(side="left")
        ttk.Button(bar, text="Open on GitHub ↗", command=self.open_gh).pack(side="right")
        ttk.Button(bar, text="Save log…", command=self.save).pack(side="right", padx=6)

        bar2 = ttk.Frame(w, padding=(10, 0, 10, 6))
        bar2.pack(fill="x")
        self.find_v = tk.StringVar()
        fe = ttk.Entry(bar2, textvariable=self.find_v, width=30)
        fe.pack(side="left")
        fe.bind("<Return>", lambda _e: self.find())
        ttk.Button(bar2, text="Find", command=self.find).pack(side="left", padx=(6, 14))
        self.info = ttk.Label(bar2, text="", style="Cap.TLabel")
        self.info.pack(side="left")

        sf = ttk.LabelFrame(w, text=" What went wrong  (click a line to jump to it) ", padding=4)
        sf.pack(fill="x", padx=10)
        self.summary = tk.Listbox(sf, height=7, font=mono, activestyle="none", borderwidth=0,
                                  highlightthickness=0, selectbackground="#ddf4ff", selectforeground="black")
        ssb = ttk.Scrollbar(sf, orient="vertical", command=self.summary.yview)
        self.summary.configure(yscrollcommand=ssb.set)
        ssb.pack(side="right", fill="y")
        self.summary.pack(fill="x", expand=True)
        self.summary.bind("<<ListboxSelect>>", self.on_summary)

        lf = ttk.Frame(w, padding=(10, 6, 10, 10))
        lf.pack(fill="both", expand=True)
        self.text = tk.Text(lf, wrap="none", font=mono, bg="#fbfcfd", fg="#1f2328",
                            borderwidth=1, relief="solid", undo=False)
        vs = ttk.Scrollbar(lf, orient="vertical", command=self.text.yview)
        hs = ttk.Scrollbar(lf, orient="horizontal", command=self.text.xview)
        self.text.configure(yscrollcommand=vs.set, xscrollcommand=hs.set)
        vs.pack(side="right", fill="y")
        hs.pack(side="bottom", fill="x")
        self.text.pack(fill="both", expand=True)
        t = self.text
        t.tag_configure("ln", foreground="#8c959f")
        t.tag_configure("err", foreground="#cf222e", background="#ffebe9")
        t.tag_configure("warn", foreground="#9a6700")
        t.tag_configure("group", foreground="#0969da", font=mono + ("bold",))
        t.tag_configure("cmd", foreground="#6e7781")
        t.tag_configure("failstep", background="#fff1f0")
        t.tag_configure("jump", background="#fff8c5")
        t.tag_raise("err")
        t.tag_configure("hit", background="#ffd33d")
        t.tag_raise("jump")
        t.tag_raise("hit")
        t.bind("<Key>", lambda e: None if (e.state & 0x4 and e.keysym.lower() in ("c", "a")) else "break")

        self.job_v.set(next(k for k, v in self.labels.items() if v is self.job))
        self.load()
        self.poll()

    # ----- loading
    def on_job(self, _e=None):
        self.job = self.labels[self.job_v.get()]
        self.step_no = None
        self.load()

    def load(self):
        jid = self.job["id"]
        if jid in self.cache:
            self.render()
            return
        self.show_message("Downloading log from GitHub…")
        job, gh, repo, q = self.job, self.gh, self.repo, self.q

        def work():
            try:
                q.put((jid, parse_log(gh.get_text(f"/repos/{repo}/actions/jobs/{jid}/logs")), None))
            except Exception as e:
                msg = str(e)
                if job.get("status") != "completed":
                    msg = "This job is still running. GitHub makes its log available once the job finishes."
                q.put((jid, None, msg))

        threading.Thread(target=work, daemon=True).start()

    def poll(self):
        try:
            while True:
                jid, parsed, err = self.q.get_nowait()
                if parsed is not None:
                    self.cache[jid] = parsed
                if jid == self.job["id"]:
                    self.render() if parsed is not None else self.show_message("⚠ " + err)
        except queue.Empty:
            pass
        if self.w.winfo_exists():
            self.w.after(150, self.poll)

    def show_message(self, msg):
        self.summary.delete(0, "end")
        self.list_lines = []
        self.text.delete("1.0", "end")
        self.text.insert("1.0", "\n   " + msg)
        self.info.config(text="")

    # ----- display
    def render(self):
        job = self.job
        parsed = self.cache.get(job["id"])
        if parsed is None:
            return
        steps = job.get("steps") or []
        failed_steps = [s for s in steps if s.get("conclusion") in BAD]
        focus = None
        if self.step_no is not None:
            focus = next((s for s in steps if s.get("number") == self.step_no), None)
        focus = focus or (failed_steps[0] if failed_steps else None)
        focus_lines = step_lines(parsed, focus) if focus else []
        focus_nums = {p[0] for p in focus_lines}
        if self.mode.get() == "step" and focus_lines:
            sel, where = focus_lines, f"step “{focus['name']}”"
        else:
            sel = parsed
            where = "whole job log" + (f" (step “{focus['name']}” shaded)" if focus_lines else "")
        self.sel = sel
        shown = [p for p in sel if p[3] in ("err", "warn")] if self.only_err.get() else sel
        cut = len(shown) > MAX_SHOWN
        if cut:
            shown = shown[-MAX_SHOWN:]

        t = self.text
        t.delete("1.0", "end")
        head = 1
        if cut:
            t.insert("end", f"   … showing the last {MAX_SHOWN:,} lines. Use Save log… for everything.\n", "cmd")
            head = 2
        t.insert("end", "".join(f"{n:>6}  {line}\n" for n, _ts, line, _tag in shown))
        self.row_of = {}
        for k, (n, _ts, _line, tag) in enumerate(shown):
            r = head + k
            self.row_of[n] = r
            t.tag_add("ln", f"{r}.0", f"{r}.8")
            if n in focus_nums and self.mode.get() == "job":
                t.tag_add("failstep", f"{r}.0", f"{r}.end+1c")
            if tag:
                t.tag_add(tag, f"{r}.8", f"{r}.end")

        # summary
        sm = self.summary
        sm.delete(0, "end")
        self.list_lines = []
        jt, jtag = item_status(job, self.app.kws)
        sm.insert("end", f"{jt}   {job['name']}")
        sm.itemconfig("end", foreground=COLORS[jtag])
        self.list_lines.append(None)
        for s in failed_steps:
            ss, se = item_span(s)
            dur = fmt_dur((se - ss).total_seconds()) if ss and se else "—"
            stt, stag = item_status(s, self.app.kws, is_publish(job.get("name"), self.app.kws))
            sm.insert("end", f"   {stt}   step {s.get('number')}: {s['name']}   (ran {dur})")
            sm.itemconfig("end", foreground=COLORS[stag])
            self.list_lines.append(("step", s.get("number")))
        errs = [p for p in sel if p[3] == "err"]
        for n, _ts, line, _tag in errs[:300]:
            sm.insert("end", f"      line {n}: {line.strip()[:220]}")
            sm.itemconfig("end", foreground=COLORS["bad"])
            self.list_lines.append(n)
        if not errs:
            sm.insert("end", "      No error lines found in this part of the log — the end of the log usually says why.")
            self.list_lines.append(None)
        warns = sum(1 for p in sel if p[3] == "warn")
        self.info.config(text=f"{job['name']}  ›  {where}   ·   {len(sel):,} lines   ·   "
                              f"{len(errs)} error line(s), {warns} warning(s)")
        first = next((p[0] for p in errs if p[0] in focus_nums), None) or (errs[0][0] if errs else None)
        if first in self.row_of:
            self.jump(first)
        else:
            t.see("end")

    def jump(self, n):
        r = self.row_of.get(n)
        if not r:
            return
        t = self.text
        t.tag_remove("jump", "1.0", "end")
        t.tag_add("jump", f"{r}.0", f"{r}.end")
        t.see(f"{max(r - 6, 1)}.0")
        t.see(f"{r}.0")

    def on_summary(self, _e=None):
        cur = self.summary.curselection()
        if not cur or cur[0] >= len(self.list_lines):
            return
        target = self.list_lines[cur[0]]
        if isinstance(target, int):
            if target not in self.row_of and self.only_err.get():
                self.only_err.set(False)
                self.render()
            self.jump(target)
        elif target and target[0] == "step":
            self.step_no = target[1]
            self.render()

    def find(self):
        q = self.find_v.get()
        t = self.text
        t.tag_remove("hit", "1.0", "end")
        if not q:
            return
        start = t.index("insert +1c")
        pos = t.search(q, start, nocase=True, stopindex="end") or t.search(q, "1.0", nocase=True, stopindex=start)
        if not pos:
            self.info.config(text=f"“{q}” not found")
            return
        end = f"{pos}+{len(q)}c"
        t.tag_add("hit", pos, end)
        t.mark_set("insert", pos)
        t.see(pos)

    def open_gh(self):
        url = self.job.get("html_url") or self.run.get("html_url")
        if url:
            webbrowser.open(url)

    def save(self):
        parsed = self.cache.get(self.job["id"])
        if not parsed:
            return
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", f"{self.run.get('name')}-{self.run.get('run_number')}-{self.job['name']}")
        path = filedialog.asksaveasfilename(parent=self.w, defaultextension=".log", initialfile=safe + ".log",
                                            filetypes=[("Log files", "*.log"), ("Text files", "*.txt")])
        if not path:
            return
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(line for _n, _ts, line, _tag in parsed) + "\n")
        self.info.config(text=f"Saved the whole job log to {path}")


def main():
    if sys.platform.startswith("win"):
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)  # crisp text on high-DPI screens
        except Exception:
            pass
    root = tk.Tk()
    try:
        root.iconbitmap(os.path.join(getattr(sys, "_MEIPASS", _app_dir()), "watchdog.ico"))
    except Exception:
        pass
    app = App(root)
    if "--selftest" in sys.argv:              # used by the automated Windows build check
        def check():
            out = os.path.join(_app_dir(), "selftest-result.txt")
            k = vault_keys("1234")
            v = vault_seal([{"repo": "a/b", "token": "t"}], k)
            ok = vault_open(v, "1234")[0][0]["token"] == "t"
            try:
                vault_open(v, "9999")
                ok = False
            except ValueError:
                pass
            with open(out, "w", encoding="utf-8") as f:
                f.write(f"{'ok' if ok else 'VAULT-FAILED'} {VERSION} portable={PORTABLE} cfg={CFG_PATH}\n")
            root.destroy()
        root.after(1500, check)
        app.sound_v.set(False)
    root.mainloop()


if __name__ == "__main__":
    main()
