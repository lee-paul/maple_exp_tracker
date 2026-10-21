"""
MapleStory Classic World - EXP & Meso tracker (screen-reading overlay).
Reads the on-screen EXP text, e.g. "EXP. 55,784 [28.63%]", and the meso box
via OCR on screenshot regions you choose. It never touches the game process.
The EXP table is used to work out your level and to validate every reading.
"""
import csv, ctypes, json, re, threading, time, urllib.request, uuid, tkinter as tk
from collections import deque
from datetime import datetime
from pathlib import Path
from tkinter import simpledialog

import mss
import numpy as np
from PIL import Image, ImageOps
from rapidocr_onnxruntime import RapidOCR

try:  # make Tk coordinates match real pixels on scaled displays
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    pass

# EXP needed to go from level N to N+1 (from mscwexptable.txt)
NEED = {
    1: 15, 2: 34, 3: 57, 4: 92, 5: 135, 6: 372, 7: 560, 8: 840, 9: 1242, 10: 1716, 11:
    2360, 12: 3216, 13: 4200, 14: 5460, 15: 7050, 16: 8840, 17: 11040, 18: 13716, 19:
    16680, 20: 20216, 21: 24402, 22: 28980, 23: 34320, 24: 40512, 25: 47216, 26: 54900,
    27: 63666, 28: 73080, 29: 83720, 30: 95700, 31: 108480, 32: 122760, 33: 138666, 34:
    155540, 35: 174216, 36: 194832, 37: 216600, 38: 240500, 39: 266682, 40: 294216, 41:
    324240, 42: 356916, 43: 391160, 44: 428280, 45: 468450, 46: 510420, 47: 555680, 48:
    604416, 49: 655200, 50: 709716, 51: 748608, 52: 789631, 53: 832902, 54: 878545, 55:
    926689, 56: 977471, 57: 1031036, 58: 1087536, 59: 1147132, 60: 1209994, 61: 1276301,
    62: 1346242, 63: 1420016, 64: 1497832, 65: 1579913, 66: 1666492, 67: 1757815, 68:
    1854143, 69: 1955750, 70: 2062925, 71: 2175973, 72: 2295216, 73: 2420993, 74: 2553663,
    75: 2693603, 76: 2841212, 77: 2996910, 78: 3161140, 79: 3334370, 80: 3517093, 81:
    3709829, 82: 3913127, 83: 4127566, 84: 4353756, 85: 4592341, 86: 4844001, 87: 5109452,
    88: 5389449, 89: 5684790, 90: 11938059, 91: 25069923, 92: 52646838, 93: 110558359, 94:
    232172553, 95: 487562361, 96: 1023880958, 97: 2150150011, 98: 4515315023, 99:
    9482161548
}

CFG_PATH = Path.home() / ".maple_exp_tracker.json"
LOG_PATH = Path.home() / "maple_sessions.csv"
BG, PANEL, FG, DIM = "#0b0f17", "#141b28", "#e6edf7", "#6b7a90"
ACCENT, MESO, WARN, GOOD = "#00f0c8", "#ffd166", "#ffb347", "#7CFF8A"
PCT_RE = re.compile(r"(?<!\d)(\d{1,3})\s*[.,]\s*(\d{2})\s*%")
MESO_RE = re.compile(r"\d{1,3}(?:,\d{3})+|\d+")
W = 290
API_URL = "https://shbotrebianwkibexqcn.supabase.co"   # your Supabase project URL, e.g. https://abcdxyz.supabase.co  (leave empty = no uploads)
API_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InNoYm90cmViaWFud2tpYmV4cWNuIiwicm9sZSI6ImFub24iLCJpYXQiOjE3OTI1NTUzNTAsImV4cCI6MjEwODEzMTM1MH0.gSwds8itoLIoKl-_Gj2Ea5lMed8tNzfNT-x8RGDFm9w"   # your Supabase anon (public) key
JSONL_PATH = Path.home() / "maple_sessions.jsonl"
# tiers by % of a level gained per hour (same thresholds as the web leaderboard)
TIERS = [("Bronze", 0, "#cd7f32"), ("Silver", 4, "#b8c2cc"), ("Gold", 8, "#f5c542"),
         ("Platinum", 14, "#5ad1c8"), ("Diamond", 22, "#6aa7ff"), ("Master", 35, "#c77dff"),
         ("Legend", 55, "#ff6b6b")]


def tier_of(pct_hr):
    cur = [t for t in TIERS if pct_hr >= t[1]][-1]
    nxt = next((t for t in TIERS if t[1] > pct_hr), None)
    return cur, nxt


def history(n=5):
    """pct/hr of your last n saved sessions (>=10 min) - the efficiency baseline."""
    out = []
    try:
        for line in JSONL_PATH.read_text().splitlines():
            d = json.loads(line)
            if d.get("elapsed_sec", 0) >= 600 and d.get("pct_per_hr", 0) > 0:
                out.append(d["pct_per_hr"])
    except Exception:
        pass
    return out[-n:]


def fmt(n, d=2):
    if n is None:
        return "--"
    a = abs(n)
    for div, s in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if a >= div:
            return f"{n / div:.{d}f}{s}"
    return f"{n:,.0f}"


def signed(n):
    return "--" if n is None else ("+" if n >= 0 else "-") + fmt(abs(n))


def fmt_time(sec):
    if sec is None or sec != sec or sec > 99 * 3600:
        return "--"
    sec = int(sec)
    return f"{sec // 3600}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


# ---------- parsing / validation ----------
def exp_candidates(text):
    """-> (raw_candidates, pct_candidates). Includes variants for a '[' misread as a digit."""
    pcts = []
    for a, b in PCT_RE.findall(text):
        v = float(f"{a}.{b}")
        if v <= 100 and v not in pcts:
            pcts.append(v)
    m = PCT_RE.search(text)
    head = text[:m.start()] if m else text
    digs = [re.sub(r"\D", "", t) for t in re.findall(r"\d[\d,.\s]*", head)]
    digs.append(re.sub(r"\D", "", head))
    raws = []
    for d in digs:
        if 0 < len(d) <= 12:
            for v in (d, d[:-1], d[1:]):   # as read / trailing junk / leading junk
                if v and int(v) not in raws:
                    raws.append(int(v))
    return raws, pcts


def parse_meso(text):
    text = re.sub(r"(?<=\d)[.\s](?=\d{3}\b)", ",", text)
    m = MESO_RE.search(text)
    if not m:
        return None
    digits = m.group(0).replace(",", "")
    return int(digits) if len(digits) <= 12 else None


def rate(hist, ck, window, i):
    """Per-minute rate of column i over the last `window` seconds (step-function hist)."""
    if not hist:
        return None
    ws = ck - window
    base = hist[0]
    for h in reversed(hist):
        if h[0] <= ws:
            base = h
            break
    dt = ck - max(ws, base[0])
    if dt < 20:
        return None
    return (hist[-1][i] - base[i]) / dt * 60


class Tracker:
    def __init__(self):
        self.lock = threading.Lock()
        self.reset()

    # ----- state -----
    def reset(self):
        with self.lock:
            self.t0, self.paused_total, self.paused_at = time.time(), 0.0, None
            self._reset_exp()
            self._reset_meso()

    def reset_exp(self):
        with self.lock:
            self._reset_exp()

    def reset_meso(self):
        with self.lock:
            self._reset_meso()

    def _reset_exp(self):
        self.level, self.cands, self.last, self.start = None, None, None, None
        self.cum_exp, self.cum_pct, self.levels = 0, 0.0, 0
        self.hist, self.pending_lvl, self.rejects = [], None, 0
        self.exp_gain_t, self.last_ck = None, 0
        self.cand_hits, self.base_pending, self.jump, self.jump_hits = 0, None, None, 0
        self.first = None
        self.spark, self.next_spark = deque(maxlen=60), 0

    def _reset_meso(self):
        self.m_last, self.m_start, self.m_pending = None, None, None
        self.m_gain, self.m_spent, self.m_hist, self.m_gain_t = 0, 0, [], None

    def clk(self, now):
        return (self.paused_at or now) - self.t0 - self.paused_total

    @property
    def paused(self):
        return self.paused_at is not None

    def toggle_pause(self):
        with self.lock:
            now = time.time()
            if self.paused_at:
                self.paused_total += now - self.paused_at
                self.paused_at = None
                self.last, self.m_last, self.base_pending = None, None, None  # re-baseline
            else:
                self.paused_at = now

    def set_level(self, lv):
        with self.lock:
            self.level, self.cands, self.last, self.rejects = lv, None, None, 0
            self.base_pending, self.jump, self.jump_hits = None, None, 0

    # ----- EXP -----
    @staticmethod
    def _pairs(raws, pcts, levels):
        """(level, raw, pct) combos where the raw EXP agrees with the % for that level."""
        out = []
        for lv in levels:
            need = NEED.get(lv)
            if not need:
                continue
            for p in pcts:
                for r in raws:
                    if r <= need and abs(r / need * 100 - p) <= 0.03:
                        out.append((lv, r, p))
        return out

    def _infer(self, raws, pcts, ck):
        """Work out the level from the table; needs 3 consistent reads in a row."""
        pairs = self._pairs(raws[:1], pcts, range(1, 100)) or self._pairs(raws, pcts, range(1, 100))
        ms = {lv for lv, _, _ in pairs}
        if not ms:
            return False
        new = (self.cands & ms) if self.cands else ms
        if not new:
            new = ms
        if new == self.cands and len(new) == 1:
            self.cand_hits += 1
        else:
            self.cand_hits = 1 if len(new) == 1 else 0
            if len(new) == 1:   # remember the first reading so "Start" is the true starting point
                lv = next(iter(new))
                self.first = next((lv, r, p, ck) for l, r, p in pairs if l == lv)
        self.cands = new
        if self.cand_hits >= 3:
            lv, r, p, t = self.first
            self.level, self.last, self.last_ck, self.base_pending = lv, (r, p), t, None
            if self.start is None:
                self.start = (lv, r, p)
                self.hist.append((t, 0, 0.0))
        return True

    def update_exp(self, raws, pcts, now, final=True):
        """Validate an OCR reading against the EXP table AND the expected gain.
        Returns True if the reading was used / understood, False if it looks wrong
        (caller may then re-run OCR with other preprocessing). No side effects on False
        unless final=True."""
        with self.lock:
            if self.paused_at:
                return True
            ck = self.clk(now)
            if self.level is None:
                return self._infer(raws, pcts, ck)
            L = self.level
            pairs = self._pairs(raws, pcts, (L, L + 1, L + 2))
            if self.last is None:                      # (re)baseline, confirmed twice
                pairs = [p for p in pairs if p[0] == L] or pairs
                if not pairs:
                    self.rejects += 1 if final else 0
                    return False
                lv, r, p = pairs[0]
                bp = self.base_pending
                if bp and bp[0] == lv and abs(bp[1] - r) <= 0.02 * NEED[lv]:
                    self.base_pending, self.rejects = None, 0
                    self.level, self.last, self.last_ck = lv, (r, p), ck
                    if self.start is None:
                        self.start = (lv, r, p)
                        self.hist.append((ck, 0, 0.0))
                else:
                    self.base_pending = (lv, r)
                return True

            lraw, lpct = self.last
            need = NEED[L]
            dt = max(ck - self.last_ck, 1.0)
            h = self.hist
            span = ck - h[0][0] if h else 0
            r5 = rate(h, ck, 300, 1)
            per_min = r5 if r5 else (self.cum_exp / span * 60 if span >= 20 else 0)
            rs = per_min / 60
            cap = max(0.02 * need, 4 * rs * dt + 0.01 * need)   # biggest believable gain
            expect = rs * dt

            def gain_for(lv, r):
                return sum(NEED[k] for k in range(L, lv)) - lraw + r

            ok = []
            for lv, r, p in pairs:
                if lv == L and lraw - 0.15 * need <= r <= lraw + cap:
                    ok.append((lv, r, p))
                elif lv == L + 1 and lpct > 60 and p < 40 and gain_for(lv, r) <= cap:
                    ok.append((lv, r, p))
            if not ok:  # number unreadable/garbled but % fine -> derive EXP from the %
                for p in pcts:
                    r = round(need * p / 100)
                    if lraw <= r <= lraw + cap:
                        ok.append((L, r, p))
                    if lpct > 60 and p < 40 and NEED.get(L + 1):
                        r2 = round(NEED[L + 1] * p / 100)
                        if gain_for(L + 1, r2) <= cap:
                            ok.append((L + 1, r2, p))
            if ok:  # choose the candidate closest to the gain we expect
                lv, r, p = min(ok, key=lambda c: (abs((gain_for(c[0], c[1]) if c[0] != L else c[1] - lraw) - expect), c[0]))
                if lv != L and self.pending_lvl != lv:   # level-up must be seen twice
                    self.pending_lvl = lv
                    return True
                self.pending_lvl, self.jump, self.jump_hits, self.rejects = None, None, 0, 0
                self._accept(lv, r, p, ck, now)
                return True
            if pairs:  # table-valid but a huge jump (quest reward / EXP potion?): need 3 identical reads
                lv, r, p = pairs[0]
                j = self.jump
                if j and j[0] == lv and abs(j[1] - r) <= 0.005 * NEED[lv]:
                    self.jump_hits += 1
                else:
                    self.jump, self.jump_hits = (lv, r), 1
                if self.jump_hits >= 3:
                    self.jump, self.jump_hits, self.rejects = None, 0, 0
                    self._accept(lv, r, p, ck, now)
                return True
            if final:
                self.rejects += 1
            return False

    def _accept(self, lv, r, p, ck, now):
        L, (lraw, lpct) = self.level, self.last
        dexp, dpct = 0, 0.0
        if lv != L:
            self.levels += lv - L
            dexp = sum(NEED[k] for k in range(L, lv)) - lraw + r
            dpct = (100 - lpct) + p + 100 * (lv - L - 1)
            self.level = lv
        elif r >= lraw:                     # (a drop at the same level = death penalty, not counted)
            dexp, dpct = r - lraw, max(0.0, p - lpct)
        dexp = max(0, dexp)
        if dexp > 0 or dpct > 0:
            self.cum_exp += dexp
            self.cum_pct += dpct
            self.exp_gain_t = now
            self.hist.append((ck, self.cum_exp, self.cum_pct))
        self.last, self.last_ck = (r, p), ck

    # ----- Meso -----
    def update_meso(self, v, now):
        with self.lock:
            if self.paused_at:
                return
            ck = self.clk(now)
            if self.m_last is None:
                self.m_last = v
                if self.m_start is None:
                    self.m_start = v
                    self.m_hist.append((ck, 0, 0))
                return
            if v == self.m_last:
                self.m_pending = None
                return
            if self.m_pending != v:      # a change must be read twice in a row
                self.m_pending = v
                return
            self.m_pending = None
            d = v - self.m_last
            if d > 0:
                self.m_gain += d
                self.m_gain_t = now
            else:
                self.m_spent += -d
            self.m_last = v
            self.m_hist.append((ck, self.m_gain, self.m_spent))

    # ----- snapshot -----
    def snapshot(self, now):
        with self.lock:
            ck = self.clk(now)
            e = dict(level=self.level, start=self.start, cur=self.last,
                     cum_exp=self.cum_exp, cum_pct=self.cum_pct, levels=self.levels)
            h = self.hist
            e["r1"] = (rate(h, ck, 60, 1), rate(h, ck, 60, 2))
            e["r5"] = (rate(h, ck, 300, 1), rate(h, ck, 300, 2))
            span = ck - h[0][0] if h else 0
            e["avg_exp"] = self.cum_exp / span * 3600 if span >= 20 else None
            e["avg_pct"] = self.cum_pct / span * 3600 if span >= 20 else None
            e["remaining"], e["eta"] = None, None
            if self.level and self.last and NEED.get(self.level):
                e["remaining"] = NEED[self.level] - self.last[0]
                per_min = e["r5"][0] or (e["avg_exp"] / 60 if e["avg_exp"] else None)
                if per_min and per_min > 0:
                    e["eta"] = e["remaining"] / per_min * 60
            if e["r5"][0] and ck >= self.next_spark:
                self.spark.append(e["r5"][0])
                self.next_spark = ck + 15
            e["spark"] = list(self.spark)

            mh = self.m_hist
            mspan = ck - mh[0][0] if mh else 0
            m = dict(start=self.m_start, cur=self.m_last, gain=self.m_gain, spent=self.m_spent,
                     net=self.m_gain - self.m_spent,
                     r1=rate(mh, ck, 60, 1), r5=rate(mh, ck, 300, 1),
                     avg=self.m_gain / mspan * 3600 if mspan >= 20 else None,
                     net_hr=(self.m_gain - self.m_spent) / mspan * 3600 if mspan >= 20 else None)
            gains = [t for t in (self.exp_gain_t, self.m_gain_t) if t]
            idle = not gains or now - max(gains) > 60
            return dict(exp=e, meso=m, elapsed=ck, paused=self.paused, unstable=self.rejects >= 8,
                        idle=idle and not self.paused)


class App:
    def __init__(self):
        self.cfg = {}
        if CFG_PATH.exists():
            try:
                self.cfg = json.loads(CFG_PATH.read_text())
            except Exception:
                pass
        if "region" in self.cfg:  # migrate from v1
            self.cfg["exp_region"] = self.cfg.pop("region")
        self.trk = Tracker()
        self.run = True
        self.ocr = {"exp": "", "meso": ""}
        # OCR stays off until region setup is finished (the selection overlay would pollute the screenshots)
        self.selecting = not (self.cfg.get("exp_region") and self.cfg.get("meso_region"))
        self.resume_at = 0.0
        self.hist_base = history()
        self.notice = ""
        if "player_id" not in self.cfg:
            self.cfg["player_id"] = str(uuid.uuid4())
        self.build_ui()
        threading.Thread(target=self.worker, daemon=True).start()
        self.refresh()
        if not self.cfg.get("exp_region"):
            self.root.after(400, lambda: self.select_region("exp", then=lambda: self.select_region("meso")))
        elif not self.cfg.get("meso_region"):
            self.root.after(400, lambda: self.select_region("meso"))

    # ---------- UI ----------
    def grid(self, parent, items):
        g = tk.Frame(parent, bg=BG)
        g.pack(fill="x", padx=14)
        cells = {}
        for i, (k, label, color) in enumerate(items):
            f = tk.Frame(g, bg=PANEL, padx=8, pady=3)
            f.grid(row=i // 2, column=i % 2, sticky="nsew", padx=2, pady=2)
            tk.Label(f, text=label, fg=DIM, bg=PANEL, font=("Segoe UI", 7)).pack(anchor="w")
            v = tk.Label(f, text="--", fg=color, bg=PANEL, font=("Segoe UI Semibold", 11))
            v.pack(anchor="w")
            cells[k] = v
        g.columnconfigure((0, 1), weight=1, uniform="a")
        return cells

    def build_ui(self):
        r = self.root = tk.Tk()
        r.overrideredirect(True)
        r.attributes("-topmost", True)
        r.attributes("-alpha", 0.92)
        r.configure(bg=ACCENT)
        r.geometry(self.cfg.get("pos", "+40+40"))
        body = tk.Frame(r, bg=BG)
        body.pack(padx=1, pady=1)

        hdr = tk.Frame(body, bg=PANEL)
        hdr.pack(fill="x")
        self.dot = tk.Label(hdr, text="●", fg=DIM, bg=PANEL, font=("Segoe UI", 10))
        self.dot.pack(side="left", padx=(8, 2))
        title = tk.Label(hdr, text="MAPLE TRACKER", fg=ACCENT, bg=PANEL, font=("Segoe UI Semibold", 10))
        title.pack(side="left", pady=5)
        for txt, tip, cmd in (("✕", "", self.quit), ("⟲", "", self.do_reset), ("⏸", "", self.do_pause),
                              ("◎", "", lambda: self.select_region("meso")),
                              ("⌖", "", lambda: self.select_region("exp"))):
            b = tk.Label(hdr, text=txt, fg=DIM, bg=PANEL, cursor="hand2", font=("Segoe UI Symbol", 11))
            b.pack(side="right", padx=5)
            b.bind("<Button-1>", lambda e, c=cmd: c())
            b.bind("<Enter>", lambda e, w=b: w.config(fg=ACCENT))
            b.bind("<Leave>", lambda e, w=b: w.config(fg=DIM))
            if txt == "⏸":
                self.pause_btn = b
        for w in (hdr, title, self.dot):
            w.bind("<ButtonPress-1>", self.drag_start)
            w.bind("<B1-Motion>", self.drag_move)

        # --- EXP ---
        hero = tk.Frame(body, bg=BG)
        hero.pack(fill="x", padx=14, pady=(8, 0))
        self.hero_lbl = tk.Label(hero, text="EXP / HOUR", fg=DIM, bg=BG, font=("Segoe UI", 8))
        self.hero_lbl.pack(anchor="w")
        self.hero = tk.Label(hero, text="--", fg=ACCENT, bg=BG, font=("Segoe UI Semibold", 26))
        self.hero.pack(anchor="w")
        self.tier_lbl = tk.Label(body, text="", fg=DIM, bg=BG, font=("Segoe UI Semibold", 9),
                                 wraplength=W, justify="left")
        self.tier_lbl.pack(anchor="w", padx=14)
        self.exp_line = tk.Label(body, text="", fg=DIM, bg=BG, font=("Segoe UI", 8), cursor="hand2",
                                 wraplength=W, justify="left")
        self.exp_line.pack(anchor="w", padx=14)
        self.exp_line.bind("<Button-1>", lambda e: self.ask_level())
        self.spark = tk.Canvas(body, width=W, height=32, bg=BG, highlightthickness=0)
        self.spark.pack(padx=14)
        self.cells = self.grid(body, [
            ("r1", "EXP / MIN (1m)", FG), ("r5", "EXP / MIN (5m)", FG),
            ("avg", "SESSION AVG / HR", FG), ("pcthr", "% / HOUR", FG),
            ("gain", "EXP GAINED", FG), ("rem", "EXP TO LEVEL", FG),
            ("eta", "TIME TO LEVEL", FG), ("time", "ELAPSED", FG)])
        self.bar = tk.Canvas(body, width=W, height=6, bg=PANEL, highlightthickness=0)
        self.bar.pack(padx=14, pady=(6, 4))

        # --- Meso ---
        tk.Frame(body, bg=PANEL, height=1).pack(fill="x", padx=14, pady=4)
        mh = tk.Frame(body, bg=BG)
        mh.pack(fill="x", padx=14)
        tk.Label(mh, text="MESOS / HOUR", fg=DIM, bg=BG, font=("Segoe UI", 8)).pack(side="left")
        self.meso_hero = tk.Label(mh, text="--", fg=MESO, bg=BG, font=("Segoe UI Semibold", 18))
        self.meso_hero.pack(side="right")
        self.meso_line = tk.Label(body, text="", fg=DIM, bg=BG, font=("Segoe UI", 8))
        self.meso_line.pack(anchor="w", padx=14)
        self.mcells = self.grid(body, [
            ("r1", "MESO / MIN (1m)", MESO), ("r5", "MESO / MIN (5m)", MESO),
            ("avg", "SESSION AVG / HR", MESO), ("net_hr", "NET / HR", MESO),
            ("gain", "GAINED", GOOD), ("spent", "SPENT", WARN)])

        self.status = tk.Label(body, text="", fg=DIM, bg=BG, font=("Consolas", 8),
                               wraplength=W, justify="left")
        self.status.pack(anchor="w", padx=14, pady=(6, 8))

    def drag_start(self, e):
        self._dx, self._dy = e.x_root - self.root.winfo_x(), e.y_root - self.root.winfo_y()

    def drag_move(self, e):
        self.root.geometry(f"+{e.x_root - self._dx}+{e.y_root - self._dy}")

    def ask_level(self):
        lv = simpledialog.askinteger("Level", "Current character level (1-99):",
                                     parent=self.root, minvalue=1, maxvalue=99)
        if lv:
            self.trk.set_level(lv)

    def select_region(self, key, then=None):
        self.selecting = True
        top = tk.Toplevel(self.root)
        top.attributes("-fullscreen", True)
        top.attributes("-alpha", 0.3)
        top.attributes("-topmost", True)
        top.configure(bg="black", cursor="crosshair")
        c = tk.Canvas(top, bg="black", highlightthickness=0)
        c.pack(fill="both", expand=True)
        what = ("the EXP text  (e.g.  EXP. 55,784 [28.63%] )" if key == "exp"
                else "the meso NUMBER  (Esc to skip)")
        c.create_text(top.winfo_screenwidth() // 2, 60, fill=ACCENT, font=("Segoe UI", 18),
                      text=f"Drag a tight box around {what}")
        st = {}

        def finish(region=None):
            top.destroy()
            if region:
                self.cfg[key + "_region"] = region
                (self.trk.reset_exp if key == "exp" else self.trk.reset_meso)()
            if then:
                self.root.after(150, then)       # next region: keep OCR off
            else:
                self.resume_at = time.time() + 0.6   # let the overlay fully disappear first
                self.selecting = False

        def press(e):
            st.update(x=e.x, y=e.y, r=c.create_rectangle(e.x, e.y, e.x, e.y, outline=ACCENT, width=2))

        def drag(e):
            if "r" in st:
                c.coords(st["r"], st["x"], st["y"], e.x, e.y)

        def release(e):
            if "r" not in st:
                return
            w, h = abs(e.x - st["x"]), abs(e.y - st["y"])
            ok = w > 20 and h > 6
            finish(dict(left=min(st["x"], e.x), top=min(st["y"], e.y), width=w, height=h) if ok else None)

        c.bind("<ButtonPress-1>", press)
        c.bind("<B1-Motion>", drag)
        c.bind("<ButtonRelease-1>", release)
        top.bind("<Escape>", lambda e: finish(None))
        top.focus_force()

    def do_pause(self):
        self.trk.toggle_pause()
        self.pause_btn.config(text="▶" if self.trk.paused else "⏸")

    def do_reset(self):
        self.session_form()

    def session_form(self):
        """On reset: ask for context (class/map/route/party/notes) and optionally upload the run."""
        snap = self.trk.snapshot(time.time())
        if not API_URL or snap["elapsed"] < 600:     # nothing uploadable
            self.save_session()
            self.trk.reset()
            return
        top = tk.Toplevel(self.root)
        top.title("Session details")
        top.configure(bg=BG)
        top.attributes("-topmost", True)
        fields = [("name", "Display name"), ("class", "Class"), ("map", "Map"),
                  ("route", "Route / how you farmed"), ("party_size", "Party size (1 = solo)"),
                  ("notes", "Other notes (buffs, coupons, gear…)")]
        ents = {}
        for i, (k, label) in enumerate(fields):
            tk.Label(top, text=label, fg=DIM, bg=BG).grid(row=i, column=0, sticky="w", padx=10, pady=3)
            e = tk.Entry(top, width=44, bg=PANEL, fg=FG, insertbackground=FG, relief="flat")
            e.grid(row=i, column=1, padx=10, pady=3)
            e.insert(0, self.cfg.get("last_" + k, ""))
            ents[k] = e

        def done(upload):
            d = {k: e.get().strip() for k, e in ents.items()}
            for k, v in d.items():
                self.cfg["last_" + k] = v
            top.destroy()
            if upload:
                ps = re.sub(r"\D", "", d["party_size"])
                d["party_size"] = min(6, max(1, int(ps))) if ps else 1
                d["name"] = d["name"][:24] or "Player"
            self.save_session(d if upload else None)
            self.trk.reset()

        bt = tk.Frame(top, bg=BG)
        bt.grid(row=len(fields), column=0, columnspan=2, pady=8)
        tk.Button(bt, text="Save + upload", command=lambda: done(True)).pack(side="left", padx=6)
        tk.Button(bt, text="Save only", command=lambda: done(False)).pack(side="left", padx=6)
        tk.Button(bt, text="Cancel", command=top.destroy).pack(side="left", padx=6)

    def _post(self, rec):
        cols = ("player_id name ended elapsed_sec start_level levels_gained pct_per_hr exp_per_hr "
                "meso_per_hr class map route party_size notes").split()
        body = json.dumps([{k: rec[k] for k in cols if k in rec}]).encode()
        req = urllib.request.Request(
            API_URL.rstrip("/") + "/rest/v1/sessions?on_conflict=player_id,ended", body, method="POST",
            headers={"apikey": API_KEY, "Authorization": "Bearer " + API_KEY,
                     "Content-Type": "application/json", "Prefer": "resolution=ignore-duplicates,return=minimal"})
        try:
            urllib.request.urlopen(req, timeout=15).read()
            self.notice = "✔ uploaded to leaderboard"
        except Exception as ex:
            self.notice = f"✖ upload failed: {str(ex)[:40]}"

    # ---------- OCR worker ----------
    @staticmethod
    def variants(img):
        """Image preprocessings to try, cheapest/most likely first."""
        big = img.resize((img.width * 4, img.height * 4), Image.LANCZOS)
        gray = ImageOps.autocontrast(big.convert("L"))
        yield big
        yield gray
        yield ImageOps.invert(gray)
        for sc in (2, 6):
            yield ImageOps.autocontrast(img.convert("L").resize((img.width * sc, img.height * sc), Image.LANCZOS))
        for t in (120, 170, 215):
            bw = gray.point(lambda v, t=t: 255 if v > t else 0)
            yield ImageOps.invert(bw)
            yield bw

    @staticmethod
    def ocr_text(engine, im):
        res, _ = engine(np.array(im.convert("RGB"))[:, :, ::-1])
        return " ".join(r[1] for r in sorted(res, key=lambda r: r[0][0][0])) if res else ""

    def grab(self, sct, reg):
        shot = sct.grab(reg)
        return Image.frombytes("RGB", shot.size, shot.rgb)

    def read_meso(self, sct, engine, reg):
        text = ""
        for var in self.variants(self.grab(sct, reg)):
            text = self.ocr_text(engine, var)
            v = parse_meso(text)
            if v is not None:
                self.trk.update_meso(v, time.time())
                break
        self.ocr["meso"] = text

    def read_exp(self, sct, engine, reg):
        """OCR, validate against level/expected gain, and re-run with other preprocessing if it looks wrong."""
        raws, pcts, text = [], [], ""
        for var in self.variants(self.grab(sct, reg)):
            text = self.ocr_text(engine, var)
            r, p = exp_candidates(text)
            raws += [x for x in r if x not in raws]
            pcts += [x for x in p if x not in pcts]
            self.ocr["exp"] = text
            if self.trk.update_exp(raws, pcts, time.time(), final=False):
                return
        self.trk.update_exp(raws, pcts, time.time(), final=True)

    def worker(self):
        engine = RapidOCR()
        with mss.mss() as sct:
            while self.run:
                if self.selecting or time.time() < self.resume_at:
                    time.sleep(0.2)
                    continue
                if not self.trk.paused:
                    for key, fn in (("exp", self.read_exp), ("meso", self.read_meso)):
                        reg = self.cfg.get(key + "_region")
                        if reg:
                            try:
                                fn(sct, engine, reg)
                            except Exception as ex:
                                self.ocr[key] = f"error: {ex}"
                time.sleep(0.5)

    # ---------- refresh ----------
    def refresh(self):
        s = self.trk.snapshot(time.time())
        e, m, c, mc = s["exp"], s["meso"], self.cells, self.mcells
        f = lambda v: fmt(v) if v is not None else "--"

        if e["level"] is None:
            self.exp_line.config(text="Lv ?  working out level from the EXP table… (click here to set it)")
        else:
            st, cur = e["start"], e["cur"]
            txt = f"Lv {e['level']}"
            if e["levels"]:
                txt += f" (+{e['levels']})"
            if st:
                txt += f"   Start {st[1]:,} ({st[2]:.2f}%)"
            if cur:
                txt += f"  →  Now {cur[0]:,} ({cur[1]:.2f}%)"
            self.exp_line.config(text=txt)
        hr = e["r5"][0] * 60 if e["r5"][0] else e["avg_exp"]
        self.hero.config(text=f(hr))
        self.hero_lbl.config(text="EXP / HOUR" + ("  (5m rolling)" if e["r5"][0] else "  (session avg)"))
        ph = e["r5"][1] * 60 if e["r5"][1] else e["avg_pct"]
        if ph:
            (name, _, col), nxt = tier_of(ph)
            txt = f"◆ {name.upper()}  {ph:.1f}%/hr"
            if nxt:
                txt += f"  ·  +{nxt[1] - ph:.1f} to {nxt[0]}"
            base = self.hist_base
            if base:
                txt += f"\n⚡ Efficiency {ph / (sum(base) / len(base)) * 100:.0f}% of your last {len(base)} runs"
            self.tier_lbl.config(text=txt, fg=col)
        else:
            self.tier_lbl.config(text="◆ UNRANKED  (needs ~20s of data)", fg=DIM)
        c["r1"].config(text=f(e["r1"][0]))
        c["r5"].config(text=f(e["r5"][0]))
        c["avg"].config(text=f(e["avg_exp"]))
        c["pcthr"].config(text=f"{e['avg_pct']:.2f}%" if e["avg_pct"] is not None else "--")
        c["gain"].config(text=f"{fmt(e['cum_exp'])} ({e['cum_pct']:.2f}%)")
        c["rem"].config(text=f(e["remaining"]))
        c["eta"].config(text=fmt_time(e["eta"]))
        c["time"].config(text=fmt_time(s["elapsed"]))

        mhr = m["r5"] * 60 if m["r5"] else m["avg"]
        self.meso_hero.config(text=f(mhr))
        if m["start"] is not None:
            self.meso_line.config(text=f"Start {m['start']:,}  →  Now {m['cur']:,}   (net {m['net']:+,})")
        else:
            self.meso_line.config(text="no meso region set (◎) or not read yet")
        mc["r1"].config(text=f(m["r1"]))
        mc["r5"].config(text=f(m["r5"]))
        mc["avg"].config(text=f(m["avg"]))
        mc["net_hr"].config(text=signed(m["net_hr"]))
        mc["gain"].config(text=fmt(m["gain"]))
        mc["spent"].config(text=fmt(m["spent"]))

        self.dot.config(fg=WARN if s["paused"] else (DIM if s["idle"] else GOOD))
        state = ("PAUSED  " if s["paused"] else "IDLE (no gains 60s)  " if s["idle"] else
                 "UNSTABLE: check EXP box / click level  " if s["unstable"] else "")
        state = (self.notice + "  " if self.notice else "") + state
        self.status.config(text=f"{state}EXP: {self.ocr['exp'][:34] or '…'}\nMESO: {self.ocr['meso'][:34] or '…'}")

        self.bar.delete("all")
        if e["cur"]:
            self.bar.create_rectangle(0, 0, W * e["cur"][1] / 100, 6, fill=ACCENT, width=0)
        self.spark.delete("all")
        pts = e["spark"]
        if len(pts) > 1:
            lo, hi = min(pts), max(pts)
            span = (hi - lo) or 1
            xy = []
            for i, v in enumerate(pts):
                xy += [i * W / (len(pts) - 1), 28 - (v - lo) / span * 24]
            self.spark.create_line(*xy, fill=ACCENT, width=2, smooth=True)
        self.root.after(500, self.refresh)

    # ---------- persistence ----------
    def save_session(self, details=None):
        s = self.trk.snapshot(time.time())
        e, m = s["exp"], s["meso"]
        if s["elapsed"] < 30 or (e["cum_exp"] == 0 and m["gain"] == 0 and m["spent"] == 0):
            return
        rec = dict(ended=datetime.now().isoformat(timespec="seconds"), elapsed_sec=int(s["elapsed"]),
                   start_level=e["start"][0] if e["start"] else 0, levels_gained=e["levels"],
                   exp_gained=int(e["cum_exp"]), pct_gained=round(e["cum_pct"], 3),
                   pct_per_hr=round(e["avg_pct"] or 0, 3), exp_per_hr=int(e["avg_exp"] or 0),
                   meso_gained=m["gain"], meso_per_hr=int(m["avg"] or 0))
        rec["player_id"] = self.cfg.get("player_id")
        if details:
            rec.update({k: v for k, v in details.items() if v not in ("", None)})
        with open(JSONL_PATH, "a") as fh:      # one JSON per line, paste/upload into the web dashboard
            fh.write(json.dumps(rec) + "\n")
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(json.dumps(rec))
            self.root.update()
        except Exception:
            pass
        self.hist_base = history()
        if details and API_URL:
            threading.Thread(target=self._post, args=(rec,), daemon=True).start()
        new = not LOG_PATH.exists()
        with open(LOG_PATH, "a", newline="") as fh:
            w = csv.writer(fh)
            if new:
                w.writerow(["ended", "elapsed_sec", "start_level", "levels_gained", "exp_gained",
                            "pct_gained", "exp_per_hr", "meso_gained", "meso_spent", "meso_per_hr"])
            w.writerow([datetime.now().isoformat(timespec="seconds"), int(s["elapsed"]),
                        e["start"][0] if e["start"] else "", e["levels"], int(e["cum_exp"]),
                        round(e["cum_pct"], 3), int(e["avg_exp"] or 0), m["gain"], m["spent"],
                        int(m["avg"] or 0)])

    def quit(self):
        self.run = False
        self.save_session()
        self.cfg["pos"] = f"+{self.root.winfo_x()}+{self.root.winfo_y()}"
        CFG_PATH.write_text(json.dumps(self.cfg))
        self.root.destroy()


if __name__ == "__main__":
    App().root.mainloop()
