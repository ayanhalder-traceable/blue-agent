#!/usr/bin/env python3
"""Apply the lane and verdict rubric (SKILL.md) to measured numbers. No API or tenant names in here.

Inputs (CSV from tools/pull.sh): coverage.csv, ipconc.csv, tenant.csv
        Q3 rules ("does this API do something worth abusing for this business?"): columns pattern,answer,reason.
        Regex on api_name, first match wins, answer yes|no. The agent's --q3 file (optional) is checked before
        q3_default.csv. No match: Q3 unknown, verdict baseline-only, listed in summary.q3_unmatched.
Usage:  assign_lanes.py --dir <folder with the csvs> [--q3 q3.csv] --auth-usable yes|no --out apis.json
"""
import argparse, csv, json, os, re, collections

ap = argparse.ArgumentParser()
ap.add_argument("--dir", required=True); ap.add_argument("--q3")
ap.add_argument("--tiny-spans", type=int, default=100)
ap.add_argument("--consumer-ips", type=int, default=1000)
ap.add_argument("--min-callers", type=int, default=5)
ap.add_argument("--list-share", type=float, default=0.0001)
ap.add_argument("--list-all-under", type=int, default=600, help="list every API when the tenant has this many names or fewer")
ap.add_argument("--burst-ratio", type=float, default=20, help="peak minute / median minute at or above this marks an API bursty")
ap.add_argument("--auth-usable", required=True, choices=["yes", "no"])
ap.add_argument("--out", required=True)
a = ap.parse_args()
rd = lambda f: list(csv.DictReader(open(f"{a.dir}/{f}")))
cov = rd("coverage.csv"); conc = {r["api_name"]: r for r in rd("ipconc.csv")}; ten = rd("tenant.csv")[0]
bur = {r["api_name"]: r for r in rd("burst.csv")} if os.path.exists(f"{a.dir}/burst.csv") else {}
T = sum(int(r["spans"]) for r in cov); TIPS = int(ten["ips"])
# Tenant rules match the full name (method included); default rules match the path only, so "POST" never matches "post".
q3 = [(re.compile(r["pattern"]), r["answer"], r["reason"], scope)
      for path, scope in ([(a.q3, "name")] if a.q3 else []) + [(os.path.join(os.path.dirname(os.path.abspath(__file__)), "q3_default.csv"), "path")]
      for r in csv.DictReader(open(path))]
f = lambda r, k: int(r.get(k) or 0)
notes = []
if "ext_both" not in cov[0]: notes.append("ext_both not measured: no API can be placed in the internal lane")

def lane_of(r):
    sp = f(r, "spans"); ips = f(r, "ips"); p = lambda k: f(r, k) / sp
    e4, bot, auth, ck, uid = p("s4xx"), p("bot_scan"), p("has_auth"), p("has_cookie"), p("has_uid")
    rb = p("has_reqbody"); uas = f(r, "uas"); paths = f(r, "paths")
    c = conc.get(r["api_name"]); top1 = (int(c["top1"]) / max(1, int(c["spans"]))) if c else 0
    ipsh = ips / max(1, TIPS)
    ev = f"{sp} spans, {ips} IPs ({ipsh:.2%} of tenant), {uas} UAs, {paths} paths, 4xx {e4:.0%}, auth {auth:.0%}, cookies {ck:.0%}, uid {uid:.0%}, top-1 IP {top1:.0%}"
    if e4 >= 0.8 or bot >= 0.5 or (paths >= 20 and ips <= 10 and e4 >= 0.5):
        return "probe", ev + "; mostly rejected or scanner-flagged", dict(e4=e4, auth=auth, ck=ck, uid=uid, ipsh=ipsh, top1=top1)
    if auth < .01 and ck < .01 and uid < .01 and rb < .01 and paths == 1 and e4 < .2 and (uas <= 5 or ips <= 5):
        return "health", ev + "; no auth, cookie, id or body; one path; few callers", dict(e4=e4, auth=auth, ck=ck, uid=uid, ipsh=ipsh, top1=top1)
    if "ext_both" in r and f(r, "ext_both") / sp < .05:
        return "internal", ev + f"; external API and IP on {f(r, 'ext_both') / sp:.0%} of spans (under 5%)", dict(e4=e4, auth=auth, ck=ck, uid=uid, ipsh=ipsh, top1=top1)
    if sp < a.tiny_spans:
        return "tiny", ev + f"; under {a.tiny_spans} spans a day: too small to baseline", dict(e4=e4, auth=auth, ck=ck, uid=uid, ipsh=ipsh, top1=top1)
    if (ipsh >= .01 or ips >= a.consumer_ips) and (uas >= 20 or ck >= .2 or uid >= .2):
        return "consumer", ev + f"; {a.consumer_ips}+ callers (or 1%+ of tenant IPs) with mixed UAs, cookies or ids", dict(e4=e4, auth=auth, ck=ck, uid=uid, ipsh=ipsh, top1=top1)
    if ips < a.consumer_ips and ipsh < .01 and (auth >= .3 or top1 >= .2 or ips <= 200):
        return "partner", ev + "; few callers, credentials or concentrated", dict(e4=e4, auth=auth, ck=ck, uid=uid, ipsh=ipsh, top1=top1)
    return "other", ev + "; fits neither consumer nor partner: IP spread and key mix are in between", dict(e4=e4, auth=auth, ck=ck, uid=uid, ipsh=ipsh, top1=top1)

if len(cov) <= a.list_all_under: a.list_share = 0
rows, tail = [], []
unmatched = []
for r in sorted(cov, key=lambda r: -f(r, "spans")):
    sp = f(r, "spans")
    if sp < a.list_share * T:
        tail.append(r); continue
    lane, ev, m = lane_of(r)
    q1 = (m["uid"] >= .5 or m["ck"] >= .5 or (a.auth_usable == "yes" and m["auth"] >= .5))
    q2 = f(r, "ips") >= a.min_callers
    ans = reason = None
    path = r["api_name"].split(" ", 1)[-1]
    for rx, an, rs, scope in q3:
        if rx.search(r["api_name"] if scope == "name" else path): ans, reason = an, rs; break
    if ans is None and lane in ("consumer", "partner", "other") and r["api_name"] != "<NULL>": unmatched.append(dict(api_name=r["api_name"], spans=sp, share=round(sp / T, 5)))
    if lane == "probe": v = "probe-lane"
    elif lane in ("health", "internal"): v = "drop"
    elif lane == "tiny": v = "baseline-only"
    else: v = "hunt" if (q1 and q2 and ans == "yes") else "baseline-only"
    ev += f"; Q1 key {'yes' if q1 else 'no'}, Q2 callers {'yes' if q2 else 'no'}, Q3 {ans or ('unknown' if lane in ('consumer', 'partner', 'other') else 'n/a')}" + (f" ({reason})" if reason else "")
    ipsh = m["ipsh"]
    hw = "probe lane" if v == "probe-lane" else ("no" if v != "hunt" else "yes" if ipsh >= .01 else "limited" if f(r, "ips") >= 100 else "per-client only")
    b = bur.get(r["api_name"]); ratio = float(b["burst_ratio"]) if b else None
    bursty = bool(b) and ratio >= a.burst_ratio and int(b["peak"]) >= 50
    if bursty: ev += f"; bursty: peak minute {b['peak']} spans vs median {b['median']} ({ratio:g}x) on {b['peak_day']}"
    # Who feeds the body fields: partner and internal lanes carry machine payloads, so body fields there are not end-user entities.
    ps = {"consumer": "end-user", "partner": "partner-feed", "internal": "internal"}.get(lane, "unknown")
    rows.append(dict(api_name=r["api_name"], payload_source=ps, burst_ratio=ratio, peak_minute_spans=int(b["peak"]) if b else None, bursty=bursty, spans_day=sp, ips=f(r, "ips"), clients=f(r, "users"),
                     error_share=round((f(r, "s4xx") + f(r, "s5xx")) / sp, 4), lane=lane, hunt_verdict=v, evidence=ev,
                     hunt_worthy=hw, top1_share=round(m["top1"], 3), clubbed=f(r, "paths") >= 20, paths=f(r, "paths")))

groups = collections.OrderedDict()
for r in tail:
    m, _, p = r["api_name"].partition(" "); seg = p.strip("/").split("/")[0][:30] if p else ""
    g = groups.setdefault(f"{m} /{seg}", dict(apis=0, spans=0, s4xx=0, one_path=0, ips=0))
    g["apis"] += 1; g["spans"] += f(r, "spans"); g["s4xx"] += f(r, "s4xx"); g["one_path"] += f(r, "paths") == 1; g["ips"] += f(r, "ips")
top = sorted(groups.items(), key=lambda kv: -kv[1]["spans"])
lt = []
rest = dict(apis=0, spans=0, s4xx=0, one_path=0, ips=0)
for i, (k, g) in enumerate(top):
    if i < 30 and g["spans"] >= 0.00005 * T: lt.append((k, g))
    else:
        for kk in rest: rest[kk] += g[kk]
if rest["apis"]: lt.append(("all other tail groups", rest))
long_tail = [dict(group=k, apis=g["apis"], spans=g["spans"], share=round(g["spans"] / T, 5), lane="tiny", hunt_verdict="baseline-only",
                  evidence=f"{g['apis']} names, {g['spans']} spans, 4xx {g['s4xx'] / max(1, g['spans']):.0%}, {g['one_path']} names with one path, each name under {a.list_share:.2%} of tenant spans") for k, g in lt]
out = dict(summary=dict(tenant_spans=T, tenant_ips=TIPS, named_total=len([r for r in cov if r["api_name"] != "<NULL>"]),
           listed=len(rows), tail_apis=len(tail), tail_spans=sum(f(r, "spans") for r in tail), list_share=a.list_share, notes=notes, q3_unmatched=unmatched),
           apis=rows, long_tail=long_tail)
json.dump(out, open(a.out, "w"), indent=1)
print(json.dumps({k: v for k, v in out["summary"].items() if k != "q3_unmatched"}, indent=1))
print(f"q3 unmatched: {len(unmatched)} APIs, {sum(u['spans'] for u in unmatched)} spans; heaviest:", [u["api_name"] for u in unmatched[:10]]); print(collections.Counter(r["lane"] for r in rows), collections.Counter(r["hunt_verdict"] for r in rows))
