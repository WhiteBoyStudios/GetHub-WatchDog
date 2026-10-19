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

import json
import os
import queue
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
from tkinter import messagebox, ttk

APP = "GetHub WatchDog"
API = "https://api.github.com"
CFG_PATH = os.path.join(os.path.expanduser("~"), ".gethub_watchdog.json")

COLORS = {
    "ok": "#1a7f37",     # passed
    "bad": "#cf222e",    # failed
    "run": "#0969da",    # running
    "wait": "#9a6700",   # queued
    "muted": "#6e7781",  # cancelled / skipped
}


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


def load_cfg():
    try:
        with open(CFG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_cfg(cfg):
    try:
        with open(CFG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    except Exception:
        pass


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


class Watcher(threading.Thread):
    """Polls GitHub in the background and posts results to a queue."""

    def __init__(self, repo, token, branch, interval, out_q):
        super().__init__(daemon=True)
        self.repo, self.branch, self.interval, self.q = repo, branch, interval, out_q
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
            try:
                hist = self.history(target["workflow_id"])
            except Exception:
                hist = None
        return {"repo": self.repo, "runs": runs, "target": target, "jobs": jobs,
                "hist": hist, "remaining": self.gh.remaining, "at": time.time()}

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
        self.seen_active = {}
        self.shown_run = None
        self.auto_opened = set()
        self.last_ok = None
        self.build_ui()
        root.after(200, self.drain)
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
                            text="Enter a repository and press Start.")
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
        for col, txt, w in (("wf", "Workflow", 170), ("status", "Status", 95), ("time", "Time", 60)):
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
        jf = ttk.LabelFrame(right, text=" Jobs & steps ", padding=4)
        jf.pack(fill="both", expand=True, padx=(10, 0))
        self.jobs_tv = ttk.Treeview(jf, columns=("status", "start", "dur", "eta"), show="tree headings")
        self.jobs_tv.heading("#0", text="Job / step")
        self.jobs_tv.column("#0", width=330)
        for col, txt, w in (("status", "Status", 110), ("start", "Started", 100),
                            ("dur", "Duration", 80), ("eta", "Estimate", 140)):
            self.jobs_tv.heading(col, text=txt)
            self.jobs_tv.column(col, width=w, anchor="w")
        jsb = ttk.Scrollbar(jf, orient="vertical", command=self.jobs_tv.yview)
        self.jobs_tv.configure(yscrollcommand=jsb.set)
        jsb.pack(side="right", fill="y")
        self.jobs_tv.pack(fill="both", expand=True)
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
        self.cfg.update(repo=repo, branch=self.branch_v.get().strip(), token=token,
                        interval=iv, sound=self.sound_v.get())
        save_cfg(self.cfg)

        self.seen_active.clear()
        self.data = None
        self.follow_v.set(True)
        self.watcher = Watcher(repo, token, self.branch_v.get().strip(), iv, self.q)
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
        check = list(d["runs"])
        t = d["target"]
        if t and all(r["id"] != t["id"] for r in check):
            check.append(t)
        for r in check:
            if r.get("status") != "completed":
                self.seen_active[r["id"]] = r
            elif r["id"] in self.seen_active:
                self.seen_active.pop(r["id"])
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
    def fill_runs(self):
        tv = self.runs_tv
        tv.delete(*tv.get_children())
        for r in self.data["runs"]:
            txt, tag = status_of(r)
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
        txt, tag = status_of(t)
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

    def row_vals(self, obj, est):
        txt, tag = status_of(obj)
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
            for stp in j.get("steps") or []:
                sid = f"{jid}s{stp['number']}"
                svals, stag = self.row_vals(stp, hist.get("steps", {}).get((j["name"], stp["name"])))
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
            txt, tag = status_of(j)
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
        ok = r.get("conclusion") == "success"
        s, e = run_span(r)
        dur = (e - s).total_seconds() if s and e else None
        if self.sound_v.get():
            self.alarm.start(ok)
        self.root.title(f"{APP} — {'✔ passed' if ok else '✖ ' + str(r.get('conclusion'))}")
        self.popup(r, ok, dur, e)

    def popup(self, r, ok, dur, end):
        w = tk.Toplevel(self.root)
        w.title("Build finished")
        w.resizable(False, False)
        try:
            w.attributes("-topmost", True)
        except tk.TclError:
            pass
        color = COLORS["ok"] if ok else COLORS["bad"]
        head = "✔  BUILD PASSED" if ok else f"✖  BUILD {str(r.get('conclusion', 'failed')).upper()}"
        tk.Label(w, text=head, font=("Segoe UI", 20, "bold"), fg="white", bg=color,
                 padx=40, pady=18).pack(fill="x")
        ttk.Label(w, padding=16, justify="center", text=(
            f"{r.get('name')}  #{r.get('run_number')}  on  {r.get('head_branch')}\n"
            f"Finished in {fmt_dur(dur)} at {local_clock(end)}")).pack()

        def close():
            self.alarm.stop()
            self.root.title(APP)
            w.destroy()

        btns = ttk.Frame(w, padding=(16, 0, 16, 16))
        btns.pack()
        ttk.Button(btns, text="Stop alarm", command=close).pack(side="left", padx=6)
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


def main():
    if sys.platform.startswith("win"):
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)  # crisp text on high-DPI screens
        except Exception:
            pass
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
