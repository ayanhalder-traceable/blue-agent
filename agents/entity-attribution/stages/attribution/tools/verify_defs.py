#!/usr/bin/env python3
"""Prove each definition works: compile its location, conditions and transformations to SQL and run it on the same window. No tenant names in here.
Counts only (no value is printed or stored). For user definitions it also reports how often the extracted id equals the platform's user column.
Usage: verify_defs.py --customer <uuid> --env <environment> --start 'YYYY-MM-DD HH:MM' ['...' ...] [--minutes 3] --defs <dir with the three json files> --out <dir>"""
import argparse, csv, json, re, subprocess, sys, os

ap = argparse.ArgumentParser()
ap.add_argument("--customer", required=True); ap.add_argument("--env", required=True); ap.add_argument("--start", required=True, nargs="+", help="one or more windows; a span in any of them counts")
ap.add_argument("--minutes", type=int, default=3); ap.add_argument("--defs", required=True); ap.add_argument("--out", required=True)
ap.add_argument("--only-source", help="entity source_agent filter, e.g. 06")
a = ap.parse_args()
assert re.fullmatch(r"[0-9a-f-]{36}", a.customer) and re.fullmatch(r"[A-Za-z0-9._-]+", a.env) and all(re.fullmatch(r"\d{4}-\d\d-\d\d \d\d:\d\d", t) for t in a.start)
WHERE = f"customer_id = '{a.customer}' AND environment = '{a.env}' AND (" + " OR ".join(f"(start_time_millis_ts >= timestamp '{t}:00' AND start_time_millis_ts < timestamp '{t}:00' + interval '{a.minutes}' minute)" for t in a.start) + ")"
q = lambda s: "'" + s.replace("'", "''") + "'"

def source(loc):
    t, k = loc["type"], loc["key"]
    if loc.get("key_pattern"):
        m = {"Request Header": "request_headers", "Request Cookie": "request_cookies", "Request Param": "request_params", "Response Cookie": "response_cookies"}[t]
        return f"element_at(map_filter({m}, (k, v) -> regexp_like(k, {q(loc['key_pattern'])})), element_at(map_keys(map_filter({m}, (k, v) -> regexp_like(k, {q(loc['key_pattern'])}))), 1))"
    return {"Request Header": f"element_at(request_headers, {q(k.lower())})", "Request Cookie": f"element_at(request_cookies, {q(k)})", "Request Param": f"coalesce(element_at(query_params, {q(k)}), element_at(request_params, {q(k)}))",  # query string first: request_params holds form fields, not it
            "Request Body": f"try(json_extract_scalar(request_body, {q(jpath(k))}))", "Response Cookie": f"element_at(response_cookies, {q(k)})",
            "Response Body": f"try(json_extract_scalar(response_body, {q(jpath(k))}))"}[t]

def apply(expr, tr):
    t = tr["type"]
    if t == "Regex Capture Group": return f"regexp_extract({expr}, {q(tr['value'])}, {int(tr.get('group', 1))})"
    if t == "JWT Payload Decode": return f"try(from_utf8(from_base64url(split_part({expr}, '.', 2))))"
    if t == "Base64 Decode": return f"try(from_utf8(from_base64(rpad({expr}, length({expr}) + (4 - length({expr}) % 4) % 4, '='))))"
    if t == "JSON Path": return f"try(json_extract_scalar({expr}, {q(tr['value'].replace('$.', '$[\"', 1) + '\"]') if re.fullmatch(r'\$\.[^.\[]+', tr['value']) else q(tr['value'])}))"
    raise ValueError(f"unknown transformation {t}")

def chain(at):  # attribute -> (condition sql, value sql)
    base = source(at["location"]); v = base
    for tr in at.get("value_transformations", []): v = apply(v, tr)
    conds = [f"regexp_like({source(c['location'])}, {q(c['value'])})" for c in at.get("conditions", []) if c.get("operator") == "Matches"]
    return (" AND ".join(conds) or "TRUE"), v

def scope_sql(sc):
    rx = sc.get("url_regexes") or []
    return "(" + " OR ".join(f"regexp_like(http_url_path, {q(r)})" for r in rx) + ")" if rx else "TRUE"

def run(sql):
    r = subprocess.run(["kubectl", "-n", "traceable", "exec", "-i", "deployment/trino-coordinator-ro", "-c", "trino-coordinator-ro", "--", "trino", "--server", "localhost:8080", "--catalog", "iceberg", "--schema", "iceberg_datalake", "--output-format", "CSV_HEADER"], input=sql.rstrip().rstrip(";") + ";", capture_output=True, text=True)
    out = "\n".join(l for l in r.stdout.splitlines() if "JDK_JAVA_OPTIONS" not in l)
    if r.returncode or re.search(r"^(Query \S+ failed|line \d+:)", out + r.stderr, re.M): return None, " ".join(l for l in (r.stderr + out).splitlines() if "JDK_JAVA_OPTIONS" not in l and l.strip())[:300]
    return list(csv.DictReader(out.splitlines())), None


# ---- entity locations: nested json paths, path params, label/value pairs, combined keys -------------------------------------------
BODY = {"Request Body": "request_body", "Response Body": "response_body"}
def jpath(key):
    """dotted path with [*] -> JSONPath; a list is checked on its first element, which is enough to show the location extracts."""
    out = "$"
    for part in ([] if key == "$" else key.split(".")):
        name, n = part.replace("[*]", ""), part.count("[*]")
        if name: out += '["' + name.replace('"', '') + '"]'
        out += "[0]" * n
    return out
def ent_value(loc, comb):
    t, k = loc["type"], loc["key"]
    if t in BODY:
        col = BODY[t]
        if comb: return f"IF(try(json_extract({col}, {q(jpath(k) if k else '$')})) IS NOT NULL, try(json_format(json_extract({col}, {q(jpath(k) if k else '$')}))))"
        if loc.get("pair"):
            lab, vk = loc["pair"]; lab = re.escape(lab); vk = re.escape(vk); ids = "(?:name|key|type|field|label|id)"
            r1 = f'\\{{[^{{}}]*"{ids}"\\s*:\\s*"{lab}"[^{{}}]*"{vk}"\\s*:\\s*"?([^",}}]*)'
            r2 = f'\\{{[^{{}}]*"{vk}"\\s*:\\s*"?([^",}}]*)"?[^{{}}]*"{ids}"\\s*:\\s*"{lab}"'
            return f"coalesce(nullif(regexp_extract({col}, {q(r1)}, 1), ''), nullif(regexp_extract({col}, {q(r2)}, 1), ''))"
        return f"try(json_extract_scalar({col}, {q(jpath(k))}))"
    if t == "Request Param": return f"coalesce(element_at(query_params, {q(k)}), element_at(request_params, {q(k)}))"
    m = {"Request Header": ("request_headers", k.lower()), "Request Cookie": ("request_cookies", k), "Path Param": ("path_params", k), "Response Cookie": ("response_cookies", k)}[t]
    return f"element_at({m[0]}, {q(m[1])})"
def verify_entities(ents):
    """One scan per chunk of entities. Per location: spans in its scope that extract; per entity: spans that extract by any location and distinct values (counts only)."""
    out, chunk, cols, cmap = [], [], [], []
    def flush():
        if not chunk: return
        res, e = run("SELECT count(*) spans, " + ", ".join(cols) + f" FROM span_event_view WHERE {WHERE}")
        for ei, d in chunk:
            r = dict(kind="entity", name=d["name"], status="error" if e else "ok", error=e or "")
            if res:
                R = res[0]; r.update(spans=R["spans"], in_scope=R[f"s{ei}"], extracted=R[f"x{ei}"], distinct_values=R[f"d{ei}"], locations_total=len(d["definitions"]),
                                     locations_alive=",".join(str(i) for i in range(len(d["definitions"])) if int(R[f"l{ei}_{i}"] or 0) > 0))
            out.append(r); print("entity", d["name"][:60], r.get("extracted"), r.get("distinct_values"), r.get("error", "")[:120])
        chunk.clear(); cols.clear()
    for ei, d in enumerate(ents):
        vals, scs = [], []
        for i, df in enumerate(d["definitions"]):
            sc = scope_sql(dict(url_regexes=df.get("url_regexes") or d["scope"].get("url_regexes")))
            v = f"IF({sc}, {ent_value(df['location'], bool(df.get('combine')))})"
            vals.append(v); scs.append(sc)
            cols.append(f"count_if({v} IS NOT NULL AND {v} <> '') l{ei}_{i}")
        anyv = f"nullif({vals[0]}, '')" if len(vals) == 1 else "coalesce(" + ", ".join(f"nullif({v}, '')" for v in vals) + ")"
        cols += [f"count_if({' OR '.join(scs)}) s{ei}", f"count_if({anyv} IS NOT NULL) x{ei}", f"approx_distinct({anyv}) d{ei}"]
        chunk.append((ei, d))
        if len(cols) >= 70: flush()
    flush()
    return out

rows = []
def one(kind, d, at, extra=""):
    cond, val = chain(at)
    sql = (f"SELECT count(*) spans, count_if(({scope_sql(d['scope'])})) in_scope, count_if({cond}) cond_met, count_if({cond} AND {val} IS NOT NULL AND {val} <> '') extracted, "
           f"approx_distinct(IF({cond}, {val})) distinct_values, count_if({cond} AND {val} = user_identifier AND user_identifier <> '') eq_user_col, "
           f"count_if({cond} AND {val} IS NOT NULL AND {val} <> '' AND user_identifier IS NOT NULL AND user_identifier <> '') both_present FROM span_event_view WHERE {WHERE}")
    res, e = run(sql)
    r = dict(kind=kind, name=d["name"], status="error" if e else "ok", error=e or "")
    if res: r.update(res[0])
    rows.append(r); print(kind, d["name"][:60], r.get("extracted"), r.get("distinct_values"), r.get("error", "")[:120])

defs = lambda f: json.load(open(f"{a.defs}/{f}.json"))["definitions"] if os.path.exists(f"{a.defs}/{f}.json") else []
for d in defs("user-attribution"): one("user", d, d["attributes"]["userId"])
for d in defs("session-identification"):
    i = d["identifiers"][0]; one("session", d, dict(location=i["token"]["location"], conditions=i.get("conditions", []), value_transformations=i.get("value_transformations", [])))
ents = [d for d in defs("entity-definitions") if not (a.only_source and d.get("source_agent") != a.only_source)]
rows += verify_entities(ents)
os.makedirs(a.out, exist_ok=True)
cols = ["kind", "name", "status", "spans", "in_scope", "cond_met", "extracted", "distinct_values", "eq_user_col", "both_present", "locations_total", "locations_alive", "error"]
with open(f"{a.out}/verify.csv", "w", newline="") as fh:
    w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore"); w.writeheader(); w.writerows(rows)
