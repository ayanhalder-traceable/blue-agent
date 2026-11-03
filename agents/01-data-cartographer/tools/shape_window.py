#!/usr/bin/env python3
"""Classify what the window's traffic is: continuous, ramp, decline, step, weekly-cycle, irregular. No tenant names in here.
Input: daily.csv (d,spans) from merge_window.py. Optional --prior daily.csv of the previous run's window, and --hourly dir of series.csv files.
Output: JSON with class, the numbers behind it, confidence, and what to do with the window."""
import argparse, csv, json, math, statistics, datetime as dt
ap = argparse.ArgumentParser(); ap.add_argument("--daily", required=True); ap.add_argument("--prior"); a = ap.parse_args()
load = lambda p: [(dt.date.fromisoformat(r["d"]), int(r["spans"])) for r in csv.DictReader(open(p))]
d = load(a.daily); y = [s for _, s in d]; n = len(y)
if n < 5: raise SystemExit("shape needs at least 5 complete days")
mean = statistics.mean(y); cv = statistics.pstdev(y) / mean
xs = list(range(n)); lx = [math.log(max(v, 1)) for v in y]; mx = statistics.mean(xs); ml = statistics.mean(lx)
slope = sum((x - mx) * (l - ml) for x, l in zip(xs, lx)) / sum((x - mx) ** 2 for x in xs)   # log-slope per day
slope_pct = math.exp(slope) - 1
first, last = statistics.mean(y[:3]), statistics.mean(y[-3:]); fl = last / first
we = [s for day, s in d if day.weekday() >= 5]; wd = [s for day, s in d if day.weekday() < 5]
wk_ratio = (statistics.mean(we) / statistics.mean(wd)) if we and wd else None
jumps = [(d[i][0].isoformat(), y[i] / max(y[i - 1], 1)) for i in range(1, n) if y[i] / max(y[i - 1], 1) >= 3 or y[i] / max(y[i - 1], 1) <= 1 / 3]
wow = None
if a.prior:
    p = [s for _, s in load(a.prior)]; wow = sum(y) / max(sum(p), 1)
if jumps: cls, why = "step", f"daily spans jump or drop 3x or more on {jumps[0][0]} ({jumps[0][1]:.2f}x): a regime change, not a trend"
elif abs(fl - 1) >= 0.3 and abs(slope_pct) >= 0.04 and (fl > 1) == (slope > 0):
    cls = "ramp" if fl > 1 else "decline"; why = f"last 3 days are {fl:.2f}x the first 3, trend {slope_pct:+.1%} a day"
elif wk_ratio is not None and (wk_ratio <= 0.8 or wk_ratio >= 1.25) and cv >= 0.1:
    cls = "weekly-cycle"; why = f"weekend days run at {wk_ratio:.2f}x weekdays"
elif cv < 0.15: cls, why = "continuous", f"daily spread (cv) {cv:.2f}, trend {slope_pct:+.1%} a day, no jump"
else: cls, why = "irregular", f"daily spread (cv) {cv:.2f} with no trend, jump or weekly pattern"
conf = "measured" if cls in ("step", "ramp", "decline") else ("inferred" if cls != "weekly-cycle" or wow is not None else "inferred")
notes = []
if cls == "weekly-cycle" and a.prior is None: notes.append("one week shows a weekend gap but cannot prove seasonality: confirm against the prior run's window")
if cls == "continuous": notes.append("window is a valid baseline; save the profile as the reference")
if cls in ("ramp", "decline", "step"): notes.append("do not treat this window as a stable baseline; carry the previous profile's baselines and flag drift")
if cls == "irregular": notes.append("window is noisy; widen to 14 days before using it as a baseline")
print(json.dumps(dict(**{"class": cls}, why=why, confidence=conf, days=n, window_start=d[0][0].isoformat(), window_end=d[-1][0].isoformat(),
    mean_spans=round(mean), cv=round(cv, 3), slope_pct_per_day=round(slope_pct, 4), last3_over_first3=round(fl, 3),
    weekend_over_weekday=None if wk_ratio is None else round(wk_ratio, 3), week_over_week=None if wow is None else round(wow, 3),
    jumps=jumps, notes=notes), indent=1))
