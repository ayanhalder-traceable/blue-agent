#!/usr/bin/env python3
"""Anchors and bridge APIs: what in this tenant's traffic names a PERSON, and where a person-naming value sits on the same request as a session carrier.
Agent 02 builds user journeys by chaining session keys from these anchors (agents/02-key-evaluator/tools/keygraph.py), so this is measured here, once, and handed over in profile.json.
  anchors      every value that names a person: the user_identifier column, JWT claims in a bearer header, person-named fields in request bodies, response bodies and query parameters on every API.
               Measured per value: spans, distinct values, APIs, how often it equals user_identifier. verdict: person | person-candidate | not-person.
  bridge_apis  per API: spans, anchored spans, and the reusable cookies and headers on those anchored spans with the share of anchored spans each is on
               (a bridge: the anchor can be carried onto that carrier's other requests; reusable = token-shaped values that repeat across requests),
               and issuers: APIs whose response sets a cookie while the request carries no anchor (login and landing pages, where a journey starts before a person is known).
  fact.journey-window  the window these were measured on; agent 02 measures journeys on the same window unless told otherwise.
Writes the tables and entry into <profile>; creates a minimal blue-agent/handoff@1 profile (marked partial) when none exists. No raw value is written.
Usage: anchors.py --customer <uuid> --env <env> --start 'YYYY-MM-DD HH:MM' --minutes <n> --profile <profile.json> [--apis apis.json] [--min-values 20]"""
import argparse, csv, json, os, re, subprocess, sys, datetime

ap = argparse.ArgumentParser()
ap.add_argument("--customer", required=True); ap.add_argument("--env", required=True); ap.add_argument("--start", required=True); ap.add_argument("--minutes", type=int, required=True)
ap.add_argument("--profile", required=True); ap.add_argument("--apis"); ap.add_argument("--tenant"); ap.add_argument("--min-values", type=int, default=20)
a = ap.parse_args()
assert re.fullmatch(r"[0-9a-f-]{36}", a.customer) and re.fullmatch(r"[A-Za-z0-9._-]+", a.env) and re.fullmatch(r"\d{4}-\d\d-\d\d \d\d:\d\d", a.start)
BASE = f"customer_id = '{a.customer}' AND environment = '{a.env}'"
WIN = f"start_time_millis_ts >= timestamp '{a.start}:00' AND start_time_millis_ts < timestamp '{a.start}:00' + interval '{a.minutes}' minute"
UID = "nullif(user_identifier, '')"
# claim and body-key names that can name a person; time, issuer and nonce claims never do
PERSONISH = r"(?i)^(sub|email|e-?mail|upn|unique_name|preferred_username|username|user_?name|user_?id|uid|login|loginid|oid|cid|customer_?id|name|nameid)$"
NEVER = r"(?i)^(exp|iat|nbf|auth_time|jti|nonce|iss|aud|azp|scp|scope|ver|typ|alg|kid|amr|acr|sid|at_hash|c_hash)$"
JWT_PAYLOAD = "try(json_parse(from_utf8(from_base64url(split_part(regexp_replace(v, '(?i)^bearer\\s+', ''), '.', 2)))))"
TOKEN_SHAPE = "length(v) >= 16 AND regexp_like(v, '^[A-Za-z0-9._~+/=%:-]+$')"   # reusable carrier values: long, token-shaped (not flags, dates or constants)

def trino(sql):
    r = subprocess.run(["kubectl", "-n", "traceable", "exec", "-i", "deployment/trino-coordinator-ro", "-c", "trino-coordinator-ro", "--", "trino", "--server", "localhost:8080", "--catalog", "iceberg", "--schema", "iceberg_datalake", "--output-format", "CSV_HEADER"],
                       input=sql.rstrip().rstrip(";") + ";", capture_output=True, text=True)
    out = "\n".join(l for l in r.stdout.splitlines() if "JDK_JAVA_OPTIONS" not in l)
    if r.returncode != 0 or re.search(r"^(Query \S+ failed|line \d+:)", out + r.stderr, re.M): sys.exit("FAILED: " + (r.stderr or out)[:600])
    return list(csv.DictReader(out.splitlines()))

I = lambda r, k: int(float(r.get(k) or 0))

# 1. anchors -------------------------------------------------------------------------------------------------------------------------
anchors = []
u = trino(f"SELECT count(*) spans, count({UID}) uspans, approx_distinct({UID}) dv, count(DISTINCT CASE WHEN {UID} IS NOT NULL THEN api_name END) apis FROM span_event_view WHERE {BASE} AND {WIN}")[0]
total_spans = I(u, "spans")
if I(u, "uspans"):
    anchors.append({"anchor": "user_identifier", "location": "column", "key": "user_identifier", "claim": "", "spans": I(u, "uspans"), "distinct": I(u, "dv"), "apis": I(u, "apis"),
                    "eq_user_identifier": 1.0, "verdict": "person" if I(u, "dv") >= a.min_values else "person-candidate",
                    "evidence": f"{I(u, 'uspans')} of {total_spans} spans carry it"})
claims = trino(f"""SELECT lower(k) header, c claim, count(*) spans, approx_distinct(cv) dv, count(DISTINCT api_name) apis, count_if(cv = {UID}) eq, count({UID}) with_uid
FROM (SELECT k, api_name, user_identifier, c, coalesce(try(json_extract_scalar(json_format(j[c]), '$')), json_format(j[c])) cv
      FROM (SELECT k, api_name, user_identifier, try_cast({JWT_PAYLOAD} AS map(varchar, json)) j FROM span_event_view CROSS JOIN UNNEST(request_headers) AS t(k, v)
            WHERE {BASE} AND {WIN} AND regexp_like(v, '^(?i)(bearer\\s+)?[A-Za-z0-9_-]+\\.[A-Za-z0-9_-]+\\.[A-Za-z0-9_-]*$'))
      CROSS JOIN UNNEST(map_keys(coalesce(j, MAP()))) AS c(c))
GROUP BY 1, 2 HAVING count(*) >= {a.min_values}""")
for r in claims:
    if re.match(NEVER, r["claim"]): continue
    eq = I(r, "eq") / max(I(r, "with_uid"), 1) if I(r, "with_uid") else None
    person = (eq is not None and eq >= 0.9) or (re.match(PERSONISH, r["claim"]) and I(r, "dv") >= a.min_values)
    anchors.append({"anchor": f"jwt:{r['header']}:{r['claim']}", "location": "jwt", "key": r["header"], "claim": r["claim"], "spans": I(r, "spans"), "distinct": I(r, "dv"), "apis": I(r, "apis"),
                    "eq_user_identifier": round(eq, 3) if eq is not None else "", "verdict": ("person" if eq is not None and eq >= 0.9 else "person-candidate") if person else "not-person",
                    "evidence": f"equals user_identifier on {I(r, 'eq')} of {I(r, 'with_uid')} spans that have one" if I(r, "with_uid") else "no user_identifier on these spans to compare"})

# 2. bridge APIs and issuers ---------------------------------------------------------------------------------------------------------
per_api = trino(f"""SELECT api_name, count(*) spans, count({UID}) anchored,
  count_if(cardinality(response_cookies) > 0) sets_cookie, count_if(cardinality(response_cookies) > 0 AND {UID} IS NULL) sets_cookie_no_anchor
FROM span_event_view WHERE {BASE} AND {WIN} GROUP BY 1 HAVING count(*) >= {a.min_values}""")
# reusable carriers: request cookies, headers and URL query parameters whose token-shaped values repeat across requests (a nonce or a constant links nothing)
CARRIERS = f"""SELECT api_name, user_identifier, c, v FROM (
  SELECT api_name, user_identifier, 'cookie:' || k c, v FROM span_event_view CROSS JOIN UNNEST(request_cookies) AS t(k, v) WHERE {BASE} AND {WIN} AND {TOKEN_SHAPE}
  UNION ALL SELECT api_name, user_identifier, 'header:' || lower(k) c, v FROM span_event_view CROSS JOIN UNNEST(request_headers) AS t(k, v)
  WHERE {BASE} AND {WIN} AND {TOKEN_SHAPE} AND lower(k) <> 'authorization'
  UNION ALL SELECT api_name, user_identifier, 'query:' || k c, v FROM span_event_view CROSS JOIN UNNEST(query_params) AS t(k, v) WHERE {BASE} AND {WIN} AND {TOKEN_SHAPE})"""
reusable = {r["c"] for r in trino(f"SELECT c, count(*) spans, approx_distinct(v) dv FROM ({CARRIERS}) GROUP BY 1 HAVING approx_distinct(v) >= {a.min_values} AND count(*) >= 1.5 * approx_distinct(v)")}
co = {}
if reusable:
    inl = ", ".join("'" + c.replace("'", "''") + "'" for c in sorted(reusable))
    for r in trino(f"SELECT api_name, c, count(*) n FROM ({CARRIERS}) WHERE c IN ({inl}) AND {UID} IS NOT NULL GROUP BY 1, 2 HAVING count(*) >= {a.min_values}"):
        co.setdefault(r["api_name"], []).append((I(r, "n"), r["c"]))
issuer_cookies = {}
for r in trino(f"""SELECT api_name, k, count(*) n FROM span_event_view CROSS JOIN UNNEST(response_cookies) AS t(k, v)
WHERE {BASE} AND {WIN} AND {UID} IS NULL AND {TOKEN_SHAPE} GROUP BY 1, 2 HAVING count(*) >= 5"""):
    issuer_cookies.setdefault(r["api_name"], []).append((I(r, "n"), r["k"]))
login_keys = {}
iss_apis = [r["api_name"] for r in per_api if I(r, "sets_cookie_no_anchor") >= a.min_values and r["api_name"].startswith(("POST", "PUT"))]
if iss_apis:
    inl = ", ".join("'" + x.replace("'", "''") + "'" for x in iss_apis[:200])
    for r in trino(f"""SELECT api_name, k, count(*) n, approx_distinct(json_format(j[k])) dv FROM (SELECT api_name, try_cast(try(json_parse(request_body)) AS map(varchar, json)) j FROM span_event_view
      WHERE {BASE} AND {WIN} AND api_name IN ({inl})) CROSS JOIN UNNEST(map_keys(coalesce(j, MAP()))) AS t(k) GROUP BY 1, 2"""):
        if re.match(PERSONISH, r["k"]) and I(r, "dv") >= a.min_values: login_keys.setdefault(r["api_name"], []).append(r["k"])
# field anchors: every request-body, response-body and query-parameter field whose name can name a person, on every API (top level and one level
# nested, `data.userId`-style). Not only login bodies: server-side and pre-login calls often name the user only in a body or a query parameter.
# Each field is measured against user_identifier two ways: equality on spans that carry both, and the share of its distinct values that appear as a
# user_identifier anywhere in the window (same id space even where the span itself has no user_identifier). Path parameters are not scanned: the
# platform's attribution rules cannot read them.
LEAF = PERSONISH.replace("(?i)^", "(?i)^(.*\\.)?")
def flat(col):
    j = f"try_cast(try(json_parse({col})) AS map(varchar, json))"
    b = f"SELECT api_name, user_identifier, {j} j FROM span_event_view WHERE {BASE} AND {WIN} AND {col} LIKE '{{%'"
    return (f"SELECT api_name, user_identifier, k1 f, json_extract_scalar(v1, '$') val FROM ({b}) CROSS JOIN UNNEST(coalesce(j, MAP())) AS t(k1, v1)"
            f" UNION ALL SELECT api_name, user_identifier, k1 || '.' || k2, json_extract_scalar(v2, '$') FROM ({b}) CROSS JOIN UNNEST(coalesce(j, MAP())) AS t(k1, v1)"
            f" CROSS JOIN UNNEST(coalesce(try_cast(v1 AS map(varchar, json)), MAP())) AS t2(k2, v2)")
SRC = {"body": flat("request_body"), "response_body": flat("response_body"),
       "query": f"SELECT api_name, user_identifier, k f, v val FROM span_event_view CROSS JOIN UNNEST(query_params) AS t(k, v) WHERE {BASE} AND {WIN}"}
fields = {}
for loc, src in SRC.items():
    for r in trino(f"""WITH u AS (SELECT DISTINCT lower(trim(user_identifier)) v FROM span_event_view WHERE {BASE} AND {WIN} AND {UID} IS NOT NULL),
f AS (SELECT api_name, nullif(user_identifier, '') uid, f, lower(trim(val)) val FROM ({src}) WHERE regexp_like(f, '{LEAF}') AND length(val) BETWEEN 3 AND 128)
SELECT f.api_name, f.f, count(*) spans, count(DISTINCT f.val) dv, count(f.uid) with_uid, count_if(f.val = lower(trim(f.uid))) eq,
       count(DISTINCT CASE WHEN u.v IS NOT NULL THEN f.val END) in_uid
FROM f LEFT JOIN u ON u.v = f.val GROUP BY 1, 2 HAVING count(DISTINCT f.val) >= {a.min_values}"""):
        x = fields.setdefault((loc, r["f"]), {"spans": 0, "dv": 0, "with_uid": 0, "eq": 0, "in_uid": 0, "apis": []})
        for k in ("spans", "dv", "with_uid", "eq", "in_uid"): x[k] += I(r, k)
        x["apis"].append((I(r, "spans"), r["api_name"]))
for (loc, f), x in sorted(fields.items(), key=lambda kv: -kv[1]["spans"]):
    if re.match(NEVER, f.rsplit(".", 1)[-1]): continue
    eq = x["eq"] / x["with_uid"] if x["with_uid"] >= a.min_values else None      # equality where both are on the span
    ov = x["in_uid"] / max(x["dv"], 1)                                             # same id space as user_identifier
    m = eq if eq is not None else ov
    apis = [n for _, n in sorted(x["apis"], reverse=True)]
    anchors.append({"anchor": f"{loc}:{f}", "location": loc, "key": f, "claim": ";".join(apis[:30]), "spans": x["spans"], "distinct": x["dv"], "apis": len(apis),
                    "eq_user_identifier": round(m, 3), "verdict": "person" if m >= 0.9 else "person-candidate",
                    "evidence": (f"equals user_identifier on {x['eq']} of {x['with_uid']} spans that have one; " if eq is not None else "no user_identifier on enough of these spans to compare directly; ")
                                + f"{x['in_uid']} of {x['dv']} distinct values (summed per API) are user_identifier values in the window; claim lists the APIs (top 30 by spans)"
                                + ("; below 0.9 it may be a person id in another id space (an internal customer number): a user rule still names the person, but journeys join only where a request carries both" if m < 0.9 else "")})

bridges = []
for r in sorted(per_api, key=lambda r: -I(r, "anchored")):
    s, an, nocookie = I(r, "spans"), I(r, "anchored"), I(r, "sets_cookie_no_anchor")
    cs = sorted(co.get(r["api_name"], []), reverse=True)
    role = "issuer" if nocookie >= a.min_values and nocookie / s >= 0.5 else "bridge" if cs else "anchored" if an else "unanchored"
    if role == "unanchored": continue
    bridges.append({"api_name": r["api_name"], "role": role, "spans": s, "anchored_spans": an, "sets_cookie_no_anchor": nocookie,
                    "carriers": ";".join(f"{c}={round(n / max(an, 1), 2)}" for n, c in cs[:12]),
                    "issues": ";".join(k for _, k in sorted(issuer_cookies.get(r["api_name"], []), reverse=True)[:8]), "login_fields": ";".join(login_keys.get(r["api_name"], []))})

# 3. write into the profile ----------------------------------------------------------------------------------------------------------
today = datetime.date.today().isoformat()
end = (datetime.datetime.strptime(a.start, "%Y-%m-%d %H:%M") + datetime.timedelta(minutes=a.minutes)).strftime("%Y-%m-%d %H:%M")
if os.path.exists(a.profile): prof = json.load(open(a.profile))
else:
    prof = {"contract": "blue-agent/handoff@1", "agent": "01-data-cartographer", "tenant": a.tenant or a.customer, "environment": a.env, "window": {"start": a.start, "end": end},
            "source": "span_event_view", "produced_at": today, "partial": True, "report": "profile.md", "entries": [], "tables": {}}
    if a.apis and os.path.exists(a.apis):
        rows = json.load(open(a.apis))["apis"]
        cols = ["api_name", "spans_day", "ips", "clients", "error_share", "lane", "hunt_verdict", "evidence"]
        prof["tables"]["apis"] = {"columns": cols, "rows": [{c: x.get(c, "") for c in cols} for x in rows], "consumers": ["02-key-evaluator"]}
    prof["entries"].append({"id": "gap.profile-partial", "kind": "gap", "statement": "This profile holds only the apis table and the anchor tables; the full cartographer run has not been done for this tenant.",
                            "value": {}, "evidence": {"files": [a.apis] if a.apis else []}, "confidence": 0.95, "consumers": ["02-key-evaluator"]})
def table(rows, cols): return {"columns": cols, "rows": [{c: r.get(c, "") for c in cols} for r in rows], "consumers": ["02-key-evaluator"]}
prof["tables"]["anchors"] = table(anchors, ["anchor", "location", "key", "claim", "spans", "distinct", "apis", "eq_user_identifier", "verdict", "evidence"])
prof["tables"]["bridge_apis"] = table(bridges, ["api_name", "role", "spans", "anchored_spans", "sets_cookie_no_anchor", "carriers", "issues", "login_fields"])
ent = {"id": "fact.journey-window", "kind": "fact", "statement": f"Anchors and bridges were measured on {a.start} for {a.minutes} minutes ({total_spans} spans); agent 02 measures journeys on the same window.",
       "value": {"start": a.start, "minutes": a.minutes, "spans": total_spans}, "evidence": {"files": ["tools/anchors.py"]}, "confidence": 0.95, "consumers": ["02-key-evaluator"],
       "first_seen": today, "last_confirmed": today}
prof["entries"] = [e for e in prof["entries"] if e["id"] != "fact.journey-window"] + [ent]
os.makedirs(os.path.dirname(os.path.abspath(a.profile)), exist_ok=True)
json.dump(prof, open(a.profile, "w"), indent=1)
n = {v: sum(1 for x in anchors if x["verdict"] == v) for v in ("person", "person-candidate", "not-person")}
print(f"{a.profile}: anchors {n}, bridge_apis {sum(b['role'] == 'bridge' for b in bridges)} bridges, {sum(b['role'] == 'issuer' for b in bridges)} issuers, {sum(b['role'] == 'anchored' for b in bridges)} anchored-only")
