#!/usr/bin/env python3
"""Key exploration: how far does each candidate carrier actually stitch a journey? Run after discover.py, before derive_defs.py.
For every cookie / request header that looks like a token (reused, many distinct values), group the window's spans by the carrier's VALUE and measure, per value, how many distinct APIs and how many spans it links.
A carrier whose values each touch many APIs stitches long journeys; two carriers that sit on different APIs both count, and derive_defs.py keeps a carrier past the cap when it reaches APIs the others do not.
Writes <dir>/journey.csv: carrier, name, values, apis_total, apis_per_value_p50, apis_per_value_p95, apis_per_value_max, spans_per_value_p50, spans_per_value_p95. Aggregates only: no value is written.
The window has no default: a journey is only as long as the window you let it be seen in, so choose it on purpose (a journey that outlasts the window is cut short). Load is bounded by --top and the name filter, not by a short window.
Usage: journey.py --customer <uuid> --env <env> --start 'YYYY-MM-DD HH:MM' --minutes <n> [--top 150] --dir <discover out dir>"""
import argparse, csv, re, subprocess, sys, os, collections

ap = argparse.ArgumentParser()
ap.add_argument("--customer", required=True); ap.add_argument("--env", required=True); ap.add_argument("--start", required=True)
ap.add_argument("--minutes", type=int, required=True); ap.add_argument("--top", type=int, default=150); ap.add_argument("--min-values", type=int, default=20); ap.add_argument("--dir", required=True)
a = ap.parse_args()
assert re.fullmatch(r"[0-9a-f-]{36}", a.customer) and re.fullmatch(r"[A-Za-z0-9._-]+", a.env) and re.fullmatch(r"\d{4}-\d\d-\d\d \d\d:\d\d", a.start)
BASE = f"customer_id = '{a.customer}' AND environment = '{a.env}'"
WIN = f"start_time_millis_ts >= timestamp '{a.start}:00' AND start_time_millis_ts < timestamp '{a.start}:00' + interval '{a.minutes}' minute"
TOKENISH = {"jwt", "dotted3", "uuid", "hex", "base64", "opaque", "digits13+", "other"}  # shapes a session or visitor id takes; short constants and flags are not tried

def candidates(fam, namecol):
    p = f"{a.dir}/{fam}.csv"
    if not os.path.exists(p): return []
    dv = collections.Counter()
    for r in csv.DictReader(open(p)):
        if None in r.values() or r["v_shape"] not in TOKENISH: continue
        dv[r[namecol].lower() if fam == "headers" else r[namecol]] = max(dv[r[namecol].lower() if fam == "headers" else r[namecol]], int(float(r["dv"] or 0)))
    return [n for n, d in dv.most_common(a.top) if d >= a.min_values and "'" not in n]

def trino(sql):
    r = subprocess.run(["kubectl", "-n", "traceable", "exec", "-i", "deployment/trino-coordinator-ro", "-c", "trino-coordinator-ro", "--", "trino", "--server", "localhost:8080", "--catalog", "iceberg", "--schema", "iceberg_datalake", "--output-format", "CSV_HEADER"],
                       input=sql.rstrip().rstrip(";") + ";", capture_output=True, text=True)
    out = "\n".join(l for l in r.stdout.splitlines() if "JDK_JAVA_OPTIONS" not in l)
    if r.returncode != 0 or re.search(r"^(Query \S+ failed|line \d+:)", out + r.stderr, re.M): print("FAILED:", (r.stderr or out)[:400], file=sys.stderr); return None
    return list(csv.DictReader(out.splitlines()))

rows = []
for fam, col, unnest, kname in (("cookies", "cookie", "request_cookies", "k"), ("headers", "header", "request_headers", "lower(k)")):
    names = candidates(fam, col)
    if not names: continue
    inl = ", ".join(f"'{n.lower() if fam == 'headers' else n}'" for n in names)
    sql = f"""SELECT name, count(*) vals, approx_percentile(apis, 0.5) a50, approx_percentile(apis, 0.95) a95, max(apis) amax, approx_percentile(spans, 0.5) s50, approx_percentile(spans, 0.95) s95
FROM (SELECT {kname} name, v, count(DISTINCT api_name) apis, count(*) spans FROM span_event_view CROSS JOIN UNNEST({unnest}) AS t(k, v)
      WHERE {BASE} AND {WIN} AND {kname} IN ({inl}) AND v <> '' GROUP BY 1, 2) GROUP BY 1"""
    res = trino(sql)
    if res is None: continue
    tot = trino(f"SELECT {kname} name, count(DISTINCT api_name) apis FROM span_event_view CROSS JOIN UNNEST({unnest}) AS t(k, v) WHERE {BASE} AND {WIN} AND {kname} IN ({inl}) GROUP BY 1") or []
    at = {r["name"]: r["apis"] for r in tot}
    for r in res: rows.append([("cookie" if fam == "cookies" else "header"), r["name"], r["vals"], at.get(r["name"], ""), r["a50"], r["a95"], r["amax"], r["s50"], r["s95"]])
with open(f"{a.dir}/journey.csv", "w", newline="") as f:
    w = csv.writer(f); w.writerow(["carrier", "name", "values", "apis_total", "apis_per_value_p50", "apis_per_value_p95", "apis_per_value_max", "spans_per_value_p50", "spans_per_value_p95"]); w.writerows(rows)
print(f"journey.csv: {len(rows)} carriers measured")
