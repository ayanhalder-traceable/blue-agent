#!/usr/bin/env python3
"""Merge per-day pulls (pull.sh) into the three csvs assign_lanes.py reads. No tenant or API names in here.
Distinct counts (IPs, users) cannot be added across days, so the merged view is a TYPICAL DAY: the day whose total spans is the
median of the window. An API absent from that day takes its row from its busiest day. Adds days_seen and window_spans per API.
Usage: merge_window.py --dir <pull out_dir> --out <merged dir>"""
import argparse, csv, glob, os, statistics
ap = argparse.ArgumentParser(); ap.add_argument("--dir", required=True); ap.add_argument("--out", required=True); a = ap.parse_args()
def parts(d):
    p = f"{a.dir}/{d}"
    if os.path.isfile(f"{p}/coverage.csv"): return [p]
    return sorted(f"{p}/{s}" for s in os.listdir(p) if os.path.isfile(f"{p}/{s}/coverage.csv")) if os.path.isdir(p) else []
days = sorted(d for d in os.listdir(a.dir) if parts(d))
DISTINCT = {"ips", "users", "uas", "paths", "apis"}  # not additive across slices of one day: the largest slice is a lower bound
def rd(d, f):
    ps = parts(d)
    if len(ps) == 1: return list(csv.DictReader(open(f"{ps[0]}/{f}.csv")))
    key = {"coverage": "api_name", "ipconc": "api_name", "tenant": None}[f] if f in ("coverage", "ipconc", "tenant") else None
    out = {}
    for p in ps:
        for r in csv.DictReader(open(f"{p}/{f}.csv")):
            k = r[key] if key else "_"
            if k not in out: out[k] = dict(r); continue
            o = out[k]
            if f == "ipconc":
                if int(r["spans"]) > int(o["spans"]): out[k] = dict(r)
                continue
            for c, v in r.items():
                if c in DISTINCT: o[c] = str(max(int(o[c]), int(v)))
                elif c in o and v.lstrip("-").isdigit(): o[c] = str(int(o[c]) + int(v))
    return list(out.values())
def burst(d):
    out = {}
    for pth in parts(d):
        fp = f"{pth}/burst.csv"
        if not os.path.isfile(fp): continue
        for r in csv.DictReader(open(fp)):
            o = out.get(r["api_name"])
            if o is None or int(r["peak"]) > int(o["peak"]): out[r["api_name"]] = dict(r, minutes=str(int(r["minutes"]) + (int(o["minutes"]) if o else 0)))
            else: o["minutes"] = str(int(o["minutes"]) + int(r["minutes"]))
    return out
tot = {d: sum(int(r["spans"]) for r in rd(d, "coverage")) for d in days}
ref = sorted(days, key=lambda d: tot[d])[len(days) // 2]
cov = {d: {r["api_name"]: r for r in rd(d, "coverage")} for d in days}
conc = {d: {r["api_name"]: r for r in rd(d, "ipconc")} for d in days}
names = set().union(*[set(c) for c in cov.values()])
os.makedirs(a.out, exist_ok=True); rows, crow = [], []
for n in names:
    per = {d: int(cov[d][n]["spans"]) for d in days if n in cov[d]}
    src = ref if n in cov[ref] else max(per, key=per.get)
    r = dict(cov[src][n]); r["days_seen"] = len(per); r["window_spans"] = sum(per.values()); r["source_day"] = src
    rows.append(r)
    if n in conc[src]: crow.append(conc[src][n])
def w(name, rs):
    with open(f"{a.out}/{name}.csv", "w", newline="") as fh:
        wr = csv.DictWriter(fh, fieldnames=list(rs[0])); wr.writeheader(); wr.writerows(rs)
# Burst: the WINDOW's worst minute per API (a typical-day view would miss a burst on another day), against the median minute of the typical day.
bs = {d: burst(d) for d in days}; brow = []
for n in names:
    ds = [d for d in days if n in bs[d]]
    if not ds: continue
    pk = max(ds, key=lambda d: int(bs[d][n]["peak"])); md = ref if n in bs[ref] else pk
    med = max(1, int(float(bs[md][n]["median"]))); peak = int(bs[pk][n]["peak"])
    brow.append(dict(api_name=n, minutes=bs[md][n]["minutes"], peak=peak, median=med, burst_ratio=round(peak / med, 1), peak_day=pk, peak_minute=bs[pk][n]["peak_minute"]))
if brow: w("burst", brow)
w("coverage", sorted(rows, key=lambda r: -int(r["spans"]))); w("ipconc", crow); w("tenant", rd(ref, "tenant"))
with open(f"{a.out}/daily.csv", "w") as fh:
    fh.write("d,spans\n"); [fh.write(f"{d},{tot[d]}\n") for d in days]
print(f"{len(days)} days {days[0]}..{days[-1]}; typical day {ref} ({tot[ref]} spans); {len(names)} api names; "
      f"{sum(1 for r in rows if r['days_seen'] < len(days))} seen on fewer than all days")
