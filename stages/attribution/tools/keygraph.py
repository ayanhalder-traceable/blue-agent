#!/usr/bin/env python3
"""Key graph: the chain of correlation keys that stitches the longest user journeys. Run after discover.py and journey.py, before derive_defs.py.
A journey starts from an ANCHOR (a value that names a person: the profile stage's profile.json, table `anchors`, verdict person) and grows through every request that
shares a key value with it: a cookie, a header, then whatever other key those requests carry, and so on. Keys are nodes' types, values are nodes, a request links
every value it carries. A connected group of requests with exactly one person in it is one person's journey; a group with two or more people means a key merged them.
Chain selection is greedy: at each step add the key that attributes the most new requests to exactly one person, as long as the share of anchored requests sitting in
merged groups stays within --collision-budget. Keys that never pay off, or merge people, are listed with the reason.
Candidates: journey.csv carriers that link something (a value seen on more than one request), plus the reusable carriers and issued cookies in profile.json `bridge_apis`.
APIs in the profile stage's probe and health lanes are left out. Values are hashed inside Trino; no raw value leaves the cluster.
Writes <dir>/journey-graph.json and <dir>/chain.csv.
The window comes from --start/--minutes, else from the profile's fact.journey-window (the window the profile stage measured anchors on). There is no built-in window.
Usage: keygraph.py --customer <uuid> --env <env> --profile <profile.json> --dir <discover out dir> [--start 'YYYY-MM-DD HH:MM' --minutes <n>] [--collision-budget 0.02] [--max-candidates 60]"""
import argparse, csv, json, os, re, subprocess, sys, collections, statistics

ap = argparse.ArgumentParser()
ap.add_argument("--customer", required=True); ap.add_argument("--env", required=True); ap.add_argument("--profile", required=True); ap.add_argument("--dir", required=True)
ap.add_argument("--start"); ap.add_argument("--minutes", type=int); ap.add_argument("--collision-budget", type=float, default=0.02); ap.add_argument("--max-candidates", type=int, default=60)
ap.add_argument("--min-values", type=int, default=20)
a = ap.parse_args()
P = json.load(open(a.profile))
ENT = {e["id"]: e for e in P.get("entries", [])}
def T(name):
    t = P.get("tables", {}).get(name)
    return [r if isinstance(r, dict) else dict(zip(t["columns"], r)) for r in t["rows"]] if t else []
if not a.start:
    w = ENT.get("fact.journey-window", {}).get("value") or sys.exit("no --start/--minutes and no fact.journey-window in the profile: run the profile stage tools/anchors.py, or pass the window")
    a.start, a.minutes = w["start"], int(w["minutes"])
if not a.minutes: sys.exit("--minutes is required with --start")
assert re.fullmatch(r"[0-9a-f-]{36}", a.customer) and re.fullmatch(r"[A-Za-z0-9._-]+", a.env) and re.fullmatch(r"\d{4}-\d\d-\d\d \d\d:\d\d", a.start)
BASE = f"customer_id = '{a.customer}' AND environment = '{a.env}'"
WIN = f"start_time_millis_ts >= timestamp '{a.start}:00' AND start_time_millis_ts < timestamp '{a.start}:00' + interval '{a.minutes}' minute"
q = lambda s: "'" + s.replace("'", "''") + "'"
H = lambda e: f"substr(to_hex(xxhash64(to_utf8(nullif({e}, '')))), 1, 12)"     # 48-bit value hash: collisions negligible at this cardinality

def trino(sql):
    r = subprocess.run(["kubectl", "-n", "traceable", "exec", "-i", "deployment/trino-coordinator-ro", "-c", "trino-coordinator-ro", "--", "trino", "--server", "localhost:8080", "--catalog", "iceberg", "--schema", "iceberg_datalake", "--output-format", "CSV_HEADER"],
                       input=sql.rstrip().rstrip(";") + ";", capture_output=True, text=True)
    out = "\n".join(l for l in r.stdout.splitlines() if "JDK_JAVA_OPTIONS" not in l)
    if r.returncode != 0 or re.search(r"^(Query \S+ failed|line \d+:)", out + r.stderr, re.M): sys.exit("FAILED: " + (r.stderr or out)[:600])
    return list(csv.DictReader(out.splitlines()))

# anchors: person-verdict values in the same value space as each other (equal to user_identifier where both exist), coalesced in profile order
anc = [r for r in T("anchors") if r["verdict"] == "person" and (r["location"] == "column" or float(r["eq_user_identifier"] or 0) >= 0.9) and int(float(r.get("distinct") or 0)) >= a.min_values]
if not anc: sys.exit("the profile has no anchor with verdict person: nothing names a person, so no journey can be attributed")
def _jpath(k): return "$" + "".join("[" + json.dumps(x) + "]" for x in k.split("."))
def anchor_sql(r):   # every anchor is lowered and trimmed, so one person's id hashes the same from any source
    if r["location"] == "column": e = f"nullif({r['key']}, '')"
    elif r["location"] == "jwt":
        e = (f"try(json_extract_scalar(from_utf8(from_base64url(split_part(regexp_replace(element_at(hl, {q(r['key'].lower())}), '(?i)^bearer\\s+', ''), '.', 2))), "
             f"{q('$[' + json.dumps(r['claim']) + ']')}))")
    else: e = field_sql(r)
    return f"nullif(lower(trim({e})), '')"
def field_sql(r):   # body, response_body, query field: only on the APIs the profile stage saw it on (the anchor's claim column)
    col = {"body": "request_body", "response_body": "response_body"}.get(r["location"])
    v = f"try(json_extract_scalar({col}, {q(_jpath(r['key']))}))" if col else f"element_at(query_params, {q(r['key'])})"
    apis = [x for x in (r.get("claim") or "").split(";") if x]
    return f"CASE WHEN api_name IN ({', '.join(map(q, apis))}) THEN {v} END" if apis else v
ANCHOR = "coalesce(" + ", ".join(anchor_sql(r) for r in anc) + ")" if len(anc) > 1 else anchor_sql(anc[0])

# candidates ---------------------------------------------------------------------------------------------------------------------
cand = {}
jp = os.path.join(a.dir, "journey.csv")
for r in (csv.DictReader(open(jp)) if os.path.exists(jp) else []):
    if int(float(r["values"] or 0)) < a.min_values or float(r["spans_per_value_p95"] or 0) <= 1: continue   # a value never seen twice links nothing
    cand[(r["carrier"], r["name"] if r["carrier"] == "cookie" else r["name"].lower())] = {"source": "journey", "reach_p95": float(r["apis_per_value_p95"] or 0), "values": int(float(r["values"]))}
for b in T("bridge_apis"):
    for c in filter(None, (b.get("carriers") or "").split(";")):
        kind, name = c.rsplit("=", 1)[0].split(":", 1)
        cand.setdefault((kind, name if kind in ("cookie", "query") else name.lower()), {"source": "bridge_apis", "reach_p95": 0, "values": 0})
    for name in filter(None, (b.get("issues") or "").split(";")):
        cand.setdefault(("cookie", name), {"source": "issued", "reach_p95": 0, "values": 0})
# URL query parameters: a value that repeats across requests (a user id, a visit or tracking id passed in links) can stitch like a cookie
for r in trino(f"""SELECT k, count(*) spans, approx_distinct(v) dv FROM span_event_view CROSS JOIN UNNEST(query_params) AS t(k, v)
WHERE {BASE} AND {WIN} AND length(v) >= 6 GROUP BY 1 HAVING approx_distinct(v) >= {a.min_values} AND count(*) >= 1.5 * approx_distinct(v)"""):
    cand.setdefault(("query", r["k"]), {"source": "query", "reach_p95": 0, "values": int(float(r["dv"]))})
# person-named fields that are not in user_identifier's id space (an internal customer number, an email where the column holds a username):
# one key per field name, across request bodies, response bodies and query parameters. A sign-in response that returns `data.userId` next to
# `data.userName` then links a back-end call carrying `UserId` to the person. Scored like any key: kept only if it attributes without merging people.
FIELD = collections.defaultdict(list)
for r in T("anchors"):
    if r["location"] in ("body", "response_body", "query") and r["verdict"] == "person-candidate" and int(float(r.get("distinct") or 0)) >= a.min_values:
        FIELD[r["key"].rsplit(".", 1)[-1].lower()].append(r)
for leaf, rs in FIELD.items():
    cand.setdefault(("field", leaf), {"source": "anchors", "reach_p95": 0, "values": sum(int(float(r["distinct"])) for r in rs)})
def key_sql(k, n):
    if k == "cookie": return f"element_at(request_cookies, {q(n)})"
    if k == "query": return f"element_at(query_params, {q(n)})"
    if k == "field": return "nullif(lower(trim(coalesce(" + ", ".join(field_sql(r) for r in FIELD[n]) + ("" if len(FIELD[n]) > 1 else ", NULL") + "))), '')"
    return f"element_at(hl, {q(n)})"
_new = {k: v for k, v in cand.items() if v["source"] in ("anchors", "query")}        # few, and only measured here: never cut by the cap
cand = dict(sorted(((k, v) for k, v in cand.items() if k not in _new), key=lambda kv: (kv[1]["source"] != "journey", -kv[1]["reach_p95"], -kv[1]["values"]))[:a.max_candidates]) | _new
KEYS = list(cand)
DROP_LANES = {"probe", "health"}
dropped = [r["api_name"] for r in T("apis") if r.get("lane") in DROP_LANES]

# hashed, collapsed tuples ---------------------------------------------------------------------------------------------------------
cols = [f"{H(ANCHOR)} a"] + [f"{H(key_sql(k, n))} k{i}" for i, (k, n) in enumerate(KEYS)]
sql = f"""SELECT api_name, {', '.join(cols)}, count(*) n FROM (
  SELECT *, coalesce(try(transform_keys(request_headers, (k, v) -> lower(k))), request_headers) hl FROM span_event_view WHERE {BASE} AND {WIN}
  {('AND api_name NOT IN (' + ', '.join(map(q, dropped)) + ')') if dropped else ''})
GROUP BY {', '.join(str(i + 1) for i in range(len(cols) + 1))}"""
rows = trino(sql)
print(f"{len(rows)} collapsed tuples over {len(KEYS)} candidate keys; window {a.start} +{a.minutes}m", file=sys.stderr)
API = {}; R = []          # R: (api index, anchor hash or None, {key index: value hash}, spans)
for r in rows:
    R.append((API.setdefault(r["api_name"], len(API)), r["a"] or None, {i: r[f"k{i}"] for i in range(len(KEYS)) if r[f"k{i}"]}, int(r["n"])))
TOTAL = sum(x[3] for x in R)
by_key = collections.defaultdict(list)
for ri, x in enumerate(R):
    for i in x[2]: by_key[i].append(ri)

# union-find over request rows, key values and anchors --------------------------------------------------------------------------------
parent = {}
def find(x):
    root = x
    while parent.get(root, root) != root: root = parent[root]
    while parent.get(x, x) != root: parent[x], x = root, parent[x]
    return root
def union(x, y):
    x, y = find(x), find(y)
    if x != y: parent[x] = y
for ri, x in enumerate(R):
    if x[1]: union(("r", ri), ("a", x[1]))
def components():
    C = collections.defaultdict(lambda: {"spans": 0, "anchors": set(), "rows": []})
    for ri, x in enumerate(R):
        c = C[find(("r", ri))]; c["spans"] += x[3]; c["rows"].append(ri)
        if x[1] and len(c["anchors"]) < 2: c["anchors"].add(x[1])
    return C
def state(C):
    att = sum(c["spans"] for c in C.values() if len(c["anchors"]) == 1)
    merged = sum(c["spans"] for c in C.values() if len(c["anchors"]) > 1)
    return att, merged
def journeys(C):
    """per attributed person: distinct APIs and spans in their journey"""
    per = collections.defaultdict(lambda: [set(), 0])
    for c in C.values():
        if len(c["anchors"]) != 1: continue
        p = per[next(iter(c["anchors"]))]
        for ri in c["rows"]: p[0].add(R[ri][0]); p[1] += R[ri][3]
    apis = sorted(len(v[0]) for v in per.values()); spans = sorted(v[1] for v in per.values())
    pct = lambda xs, p: xs[min(len(xs) - 1, int(p * len(xs)))] if xs else 0
    covered = set().union(*(v[0] for v in per.values())) if per else set()
    return {"people": len(per), "journey_apis_p50": pct(apis, .5), "journey_apis_p95": pct(apis, .95), "journey_spans_p50": pct(spans, .5), "journey_spans_p95": pct(spans, .95), "apis_attributed": len(covered)}

C = components(); att, merged = state(C)
anchored0 = sum(x[3] for x in R if x[1])
steps = [{"step": 0, "key": "anchor only", "carrier": "", "gain_spans": att, "attributed_spans": att, "attributed_share": round(att / TOTAL, 4), "merged_spans": merged,
          "collision_rate": round(merged / max(att + merged, 1), 4), **journeys(C)}]
chosen, rejected, remaining = [], {}, set(range(len(KEYS)))

def evaluate(i, C):
    """add key i on top of the current components: new attributed and merged spans, without committing"""
    root_of = {}; tp = {}
    def tf(x):
        while tp.get(x, x) != x: x = tp[x]
        return x
    for ri in by_key[i]:
        cr = find(("r", ri)); v = ("k", i, R[ri][2][i])
        x, y = tf(cr), tf(v)
        if x != y: tp[x] = y
        root_of[cr] = True
    G = collections.defaultdict(lambda: [0, set(), 0, 0])     # spans, anchors, previously attributed spans, previously merged spans
    for cr in root_of:
        c = C[cr]; g = G[tf(cr)]
        g[0] += c["spans"]; g[1] |= c["anchors"]
        if len(c["anchors"]) == 1: g[2] += c["spans"]
        elif len(c["anchors"]) > 1: g[3] += c["spans"]
    d_att = d_merged = 0
    for g in G.values():
        n = len(g[1])
        d_att += (g[0] if n == 1 else 0) - g[2]
        d_merged += (g[0] if n > 1 else 0) - g[3]
    return d_att, d_merged

while remaining:
    best = None
    for i in sorted(remaining):
        if not by_key[i]: rejected[i] = {"reason": "not present on any request in the window"}; continue
        d_att, d_merged = evaluate(i, C)
        rate = (merged + d_merged) / max(att + d_att + merged + d_merged, 1)
        if d_att <= 0: rejected[i] = {"reason": "attributes no request the chain does not already reach" if d_merged <= 0 else "only merges people", "gain_spans": d_att, "merged_spans_added": d_merged, "collision_rate_if_added": round(rate, 4)}; continue
        if rate > a.collision_budget: rejected[i] = {"reason": f"would put {rate:.1%} of anchored requests in groups with more than one person (budget {a.collision_budget:.1%})", "gain_spans": d_att, "merged_spans_added": d_merged, "collision_rate_if_added": round(rate, 4)}; continue
        rejected.pop(i, None)
        if best is None or (d_att, -d_merged) > (best[1], -best[2]): best = (i, d_att, d_merged)
    remaining -= set(rejected)
    if best is None: break
    i, d_att, d_merged = best
    for ri in by_key[i]: union(("r", ri), ("k", i, R[ri][2][i]))
    remaining.discard(i); chosen.append(i)
    C = components(); att, merged = state(C)
    k, n = KEYS[i]
    steps.append({"step": len(chosen), "key": n, "carrier": k, "gain_spans": d_att, "attributed_spans": att, "attributed_share": round(att / TOTAL, 4), "merged_spans": merged,
                  "collision_rate": round(merged / max(att + merged, 1), 4), **journeys(C)})
    print(f"  + {k}:{n}  +{d_att} spans  attributed {att / TOTAL:.1%}  merged {merged}", file=sys.stderr)

# what the chain does not reach: requests in groups with no person, by API, and whether they carry any candidate key at all
INV = {v: k for k, v in API.items()}
un = collections.defaultdict(lambda: [0, 0, collections.Counter()])
CH = set(chosen)
for c in C.values():
    if c["anchors"]: continue
    for ri in c["rows"]:
        u = un[INV[R[ri][0]]]; u[0] += R[ri][3]
        if not CH & set(R[ri][2]): u[1] += R[ri][3]
        for i in R[ri][2]: u[2][i] += R[ri][3]
pa = {}
for r in T("anchors"):
    if r["location"] in ("body", "response_body", "query") and r["verdict"] != "not-person":
        for x in filter(None, (r.get("claim") or "").split(";")): pa.setdefault(x, f"{r['location']}:{r['key']} ({r['verdict']})")
unreached = {"spans": sum(v[0] for v in un.values()), "spans_with_no_chain_key": sum(v[1] for v in un.values()),
             "top_apis": [{"api_name": k, "spans": v[0], "no_chain_key": v[1], "carries": [f"{KEYS[i][0]}:{KEYS[i][1]}={round(n / v[0], 2)}" for i, n in v[2].most_common(4)],
                           **({"anchor_candidate": pa[k]} if k in pa else {})} for k, v in sorted(un.items(), key=lambda kv: -kv[1][0])[:25]]}
out = {"schema": "blue-agent/journey-graph@1", "window": {"start": a.start, "minutes": a.minutes}, "spans": TOTAL, "anchored_spans": anchored0, "apis": len(API),
       "anchor": [r["anchor"] for r in anc], "collision_budget": a.collision_budget, "dropped_lanes": sorted(DROP_LANES), "dropped_apis": len(dropped),
       "chain": steps, "unreached": unreached,
       "rejected": [{"key": KEYS[i][1], "carrier": KEYS[i][0], "source": cand[KEYS[i]]["source"], **v} for i, v in sorted(rejected.items(), key=lambda kv: -kv[1].get("gain_spans", 0))],
       "note": "A journey here is every request reachable from one person's anchor through shared key values inside the window. The platform attributes a user per request "
               "(user attribution rules) and groups requests per session rule; whether a session's user is carried to the session's requests that lack an anchor must be checked on the platform."}
json.dump(out, open(os.path.join(a.dir, "journey-graph.json"), "w"), indent=1)
with open(os.path.join(a.dir, "chain.csv"), "w", newline="") as f:
    w = csv.DictWriter(f, fieldnames=list(steps[0])); w.writeheader(); w.writerows(steps)
print(f"chain: {len(chosen)} keys; attributed {steps[0]['attributed_share']:.1%} -> {steps[-1]['attributed_share']:.1%} of spans; "
      f"journey APIs p50 {steps[0]['journey_apis_p50']} -> {steps[-1]['journey_apis_p50']}, p95 {steps[0]['journey_apis_p95']} -> {steps[-1]['journey_apis_p95']}; {len(rejected)} rejected")
