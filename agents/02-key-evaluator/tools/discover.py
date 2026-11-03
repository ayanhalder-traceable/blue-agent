#!/usr/bin/env python3
"""Discover every credential, session and identity carrier in a short sample window. No tenant, API or header names in here.
Writes one csv per carrier family to <out>/. Aggregates only: no raw token, cookie or user value is ever written.
Every value is reduced to a SHAPE class (jwt, uuid, email, hex, ...) in SQL; counts are approximate distinct.
Usage: discover.py --customer <uuid> --env <environment> --start 'YYYY-MM-DD HH:MM' [--minutes 5] [--min-spans 20] --out <dir>
A bounded window (minutes, not days) keeps the load low; pick a busy hour so rare carriers still show up."""
import argparse, csv, re, subprocess, sys, os

ap = argparse.ArgumentParser()
ap.add_argument("--customer", required=True); ap.add_argument("--env", required=True); ap.add_argument("--start", required=True)
ap.add_argument("--minutes", type=int, default=5); ap.add_argument("--min-spans", type=int, default=20); ap.add_argument("--out", required=True)
a = ap.parse_args()
assert re.fullmatch(r"[0-9a-f-]{36}", a.customer) and re.fullmatch(r"[A-Za-z0-9._-]+", a.env) and re.fullmatch(r"\d{4}-\d\d-\d\d \d\d:\d\d", a.start)
BASE = f"customer_id = '{a.customer}' AND environment = '{a.env}'"
WIN = f"start_time_millis_ts >= timestamp '{a.start}:00' AND start_time_millis_ts < timestamp '{a.start}:00' + interval '{a.minutes}' minute"
M = a.min_spans

def shape(x):  # value -> shape class. Order matters: first match wins.
    return f"""CASE WHEN {x} IS NULL OR {x} = '' THEN 'empty' WHEN {x} LIKE '%***%' THEN 'masked'
 WHEN regexp_like({x}, '^eyJ[A-Za-z0-9_-]+\\.[A-Za-z0-9_-]+\\.[A-Za-z0-9_-]*$') THEN 'jwt'
 WHEN regexp_like({x}, '^[A-Za-z0-9_-]{{8,}}\\.[A-Za-z0-9_-]{{8,}}\\.[A-Za-z0-9_-]*$') THEN 'dotted3'
 WHEN regexp_like({x}, '^[0-9a-fA-F]{{8}}-[0-9a-fA-F]{{4}}-[0-9a-fA-F]{{4}}-[0-9a-fA-F]{{4}}-[0-9a-fA-F]{{12}}$') THEN 'uuid'
 WHEN regexp_like({x}, '^[^@ ]+@[^@ ]+\\.[A-Za-z]{{2,}}$') THEN 'email'
 WHEN regexp_like({x}, '^[0-9]+$') THEN IF(length({x}) >= 13, 'digits13+', 'digits')
 WHEN regexp_like({x}, '^(\\d{{1,3}}\\.){{3}}\\d{{1,3}}$') THEN 'ip'
 WHEN regexp_like({x}, '^[0-9a-fA-F]{{16,}}$') THEN 'hex'
 WHEN regexp_like({x}, '^[A-Za-z0-9+/]{{16,}}={{0,2}}$') THEN 'base64'
 WHEN regexp_like({x}, '^[A-Za-z0-9_-]{{16,}}$') THEN 'opaque'
 WHEN length({x}) < 16 THEN 'short' ELSE 'other' END"""

AUTH = "element_at(request_headers, 'authorization')"
SCHEME = f"coalesce(nullif(regexp_extract({AUTH}, '^([A-Za-z0-9_-]+) ', 1), ''), IF({AUTH} IS NULL, 'none', 'bare'))"
TOKEN = f"IF(regexp_like({AUTH}, '^[A-Za-z0-9_-]+ '), substr({AUTH}, strpos({AUTH}, ' ') + 1), {AUTH})"
NAME_OK = "length(k) <= 60"
# Identity-related claim/param/key names are NOT hardcoded: every key is listed with its shape and cardinality, and derive_defs.py ranks by evidence.
PLAIN = lambda x: f"approx_distinct({x}) dv, approx_distinct(ip_address) ips, count_if(user_identifier IS NOT NULL AND user_identifier <> '') with_uid"

def jwt_creds():  # every jwt-shaped value from the auth header, every cookie and every header, with its carrier
    parts = [f"SELECT api_name, 'header' ct, 'authorization' cn, {TOKEN} tok, user_identifier, ip_address FROM span_event_view WHERE {BASE} AND {WIN} AND regexp_like({AUTH}, '^([A-Za-z0-9_-]+ )?eyJ')",
             f"SELECT api_name, 'cookie' ct, k cn, v tok, user_identifier, ip_address FROM span_event_view CROSS JOIN UNNEST(request_cookies) AS t(k, v) WHERE {BASE} AND {WIN} AND regexp_like(v, '^eyJ[A-Za-z0-9_-]+\\.[A-Za-z0-9_-]+\\.')",
             f"SELECT api_name, 'header' ct, k cn, v tok, user_identifier, ip_address FROM span_event_view CROSS JOIN UNNEST(request_headers) AS t(k, v) WHERE {BASE} AND {WIN} AND lower(k) <> 'authorization' AND regexp_like(v, '^([A-Za-z0-9_-]+ )?eyJ[A-Za-z0-9_-]+\\.[A-Za-z0-9_-]+\\.')"]
    return " UNION ALL ".join(parts)

Q = {
 # 1. authorization header: scheme x token shape per API
 "auth": f"""SELECT api_name, {SCHEME} scheme, {shape(TOKEN)} tok_shape, count(*) spans, approx_distinct({TOKEN}) dv, approx_distinct(ip_address) ips,
   count_if(user_identifier IS NOT NULL AND user_identifier <> '') with_uid, count_if(is_ip_external) ext_ip
 FROM span_event_view WHERE {BASE} AND {WIN} AND {AUTH} IS NOT NULL GROUP BY 1,2,3 HAVING count(*) >= {M}""",
 # 2. Basic: username part only, never the secret
 "basic": f"""SELECT api_name, {shape('u')} user_shape, count(*) spans, approx_distinct(u) dv, approx_distinct(ip_address) ips, count_if(u = user_identifier AND u <> '' AND user_identifier <> '') eq_uid, count_if(user_identifier IS NOT NULL AND user_identifier <> '') with_uid
 FROM (SELECT api_name, ip_address, user_identifier, try(split_part(from_utf8(from_base64(rpad({TOKEN}, length({TOKEN}) + (4 - length({TOKEN}) % 4) % 4, '='))), ':', 1)) u
       FROM span_event_view WHERE {BASE} AND {WIN} AND {AUTH} LIKE 'Basic %' AND {AUTH} NOT LIKE '%***%') GROUP BY 1,2 HAVING count(*) >= {M}""",
 # 3. cookies: name x shape per API
 "cookies": f"""SELECT api_name, k cookie, {shape('v')} v_shape, count(*) spans, approx_distinct(v) dv, approx_distinct(ip_address) ips,
   count_if(user_identifier IS NOT NULL AND user_identifier <> '') with_uid, count_if(v = user_identifier AND user_identifier <> '') eq_uid
 FROM span_event_view CROSS JOIN UNNEST(request_cookies) AS t(k, v) WHERE {BASE} AND {WIN} AND {NAME_OK} GROUP BY 1,2,3 HAVING count(*) >= {M}""",
 # 4. every other request header: tenant-wide (headers are transport; per-API is too wide)
 "headers": f"""SELECT api_name, lower(k) header, {shape('v')} v_shape, count(*) spans, approx_distinct(v) dv, approx_distinct(ip_address) ips, count_if(v = user_identifier AND user_identifier <> '') eq_uid, count_if(user_identifier IS NOT NULL AND user_identifier <> '') with_uid
 FROM span_event_view CROSS JOIN UNNEST(request_headers) AS t(k, v) WHERE {BASE} AND {WIN} AND {NAME_OK} AND lower(k) <> 'authorization' GROUP BY 1,2,3 HAVING count(*) >= {M}""",
 # 5. the platform's own user column
 "uid": f"""SELECT api_name, {shape('user_identifier')} uid_shape, count(*) spans, approx_distinct(user_identifier) dv, approx_distinct(ip_address) ips
 FROM span_event_view WHERE {BASE} AND {WIN} GROUP BY 1,2 HAVING count(*) >= {M}""",
 # 6. JWT claims, decoded in SQL: claim name x claim value shape, and whether it equals user_identifier
 "jwt_claims": f"""SELECT ct carrier, cn name, claim, IF(cv IS NULL, IF(raw LIKE '[%', 'array', IF(raw LIKE '{{%', 'object', 'null')), {shape('cv')}) cv_shape, count(*) spans, approx_distinct(cv) dv, approx_distinct(ip_address) ips, approx_distinct(api_name) apis,
   count_if(cv = user_identifier AND cv <> '' AND user_identifier <> '') eq_uid, count_if(user_identifier IS NOT NULL AND user_identifier <> '') with_uid
 FROM (SELECT ct, cn, ip_address, api_name, user_identifier, k claim, json_format(m[k]) raw, json_extract_scalar(json_format(m[k]), '$') cv
       FROM (SELECT ct, cn, ip_address, api_name, user_identifier, try(cast(json_parse(from_utf8(from_base64url(split_part(tok, '.', 2)))) AS map(varchar,json))) m FROM ({jwt_creds()}))
       CROSS JOIN UNNEST(map_keys(m)) AS t(k) WHERE m IS NOT NULL) GROUP BY 1,2,3,4 HAVING count(*) >= {M}""",
 # 7. request params (query + path) and top-level and one-level-nested body keys (`data.key`): entity and identity candidates
 "params": f"""SELECT api_name, src, k pkey, {shape('v')} v_shape, count(*) spans, approx_distinct(v) dv, approx_distinct(ip_address) ips
 FROM (SELECT api_name, ip_address, 'param' src, k, v FROM span_event_view CROSS JOIN UNNEST(map_concat(coalesce(request_params, MAP()), coalesce(query_params, MAP()))) AS t(k, v) WHERE {BASE} AND {WIN}
       UNION ALL
       SELECT api_name, ip_address, 'body' src, k, json_extract_scalar(json_format(m[k]), '$') v
       FROM (SELECT api_name, ip_address, try(cast(json_parse(request_body) AS map(varchar,json))) m FROM span_event_view WHERE {BASE} AND {WIN} AND request_body LIKE '{{%' AND length(request_body) < 20000)
       CROSS JOIN UNNEST(map_keys(m)) AS t(k) WHERE m IS NOT NULL
       UNION ALL
       SELECT api_name, ip_address, 'body' src, k || '.' || k2, json_extract_scalar(json_format(m2[k2]), '$') v
       FROM (SELECT api_name, ip_address, k, try(cast(m[k] AS map(varchar,json))) m2
             FROM (SELECT api_name, ip_address, try(cast(json_parse(request_body) AS map(varchar,json))) m FROM span_event_view WHERE {BASE} AND {WIN} AND request_body LIKE '{{%' AND length(request_body) < 20000)
             CROSS JOIN UNNEST(map_keys(m)) AS t(k) WHERE m IS NOT NULL) CROSS JOIN UNNEST(map_keys(m2)) AS t2(k2) WHERE m2 IS NOT NULL)
 WHERE {NAME_OK} AND v IS NOT NULL GROUP BY 1,2,3,4 HAVING count(*) >= {M}""",
 # 8. credentials a RESPONSE hands out (session identifiers of type Response): Set-Cookie names and body keys that are token-shaped
 "response": f"""SELECT api_name, src, k rkey, {shape('v')} v_shape, count(*) spans, approx_distinct(v) dv, approx_distinct(ip_address) ips
 FROM (SELECT api_name, ip_address, 'cookie' src, k, v FROM span_event_view CROSS JOIN UNNEST(response_cookies) AS t(k, v) WHERE {BASE} AND {WIN}
       UNION ALL
       SELECT api_name, ip_address, 'body' src, k, json_extract_scalar(json_format(m[k]), '$') v
       FROM (SELECT api_name, ip_address, try(cast(json_parse(response_body) AS map(varchar,json))) m FROM span_event_view WHERE {BASE} AND {WIN} AND response_body LIKE '{{%' AND length(response_body) < 20000 AND try_cast(status_code AS integer) < 300)
       CROSS JOIN UNNEST(map_keys(m)) AS t(k) WHERE m IS NOT NULL
       UNION ALL
       SELECT api_name, ip_address, 'body' src, k || '.' || k2, json_extract_scalar(json_format(m2[k2]), '$') v
       FROM (SELECT api_name, ip_address, k, try(cast(m[k] AS map(varchar,json))) m2
             FROM (SELECT api_name, ip_address, try(cast(json_parse(response_body) AS map(varchar,json))) m FROM span_event_view WHERE {BASE} AND {WIN} AND response_body LIKE '{{%' AND length(response_body) < 20000 AND try_cast(status_code AS integer) < 300)
             CROSS JOIN UNNEST(map_keys(m)) AS t(k) WHERE m IS NOT NULL) CROSS JOIN UNNEST(map_keys(m2)) AS t2(k2) WHERE m2 IS NOT NULL)
 WHERE {NAME_OK} AND v IS NOT NULL AND {shape('v')} IN ('jwt','dotted3','uuid','hex','base64','opaque') GROUP BY 1,2,3,4 HAVING count(*) >= {M}""",
 # 9. sample size, so every count above can be read as a share
 "window": f"""SELECT count(*) spans, approx_distinct(ip_address) ips, approx_distinct(api_name) apis, count_if(user_identifier IS NOT NULL AND user_identifier <> '') with_uid FROM span_event_view WHERE {BASE} AND {WIN}""",
}

os.makedirs(a.out, exist_ok=True)
for name, sql in Q.items():
    path = f"{a.out}/{name}.csv"
    if os.path.getsize(path) > 0 if os.path.exists(path) else False: continue
    r = subprocess.run(["kubectl", "-n", "traceable", "exec", "-i", "deployment/trino-coordinator-ro", "-c", "trino-coordinator-ro", "--",
                        "trino", "--server", "localhost:8080", "--catalog", "iceberg", "--schema", "iceberg_datalake", "--output-format", "CSV_HEADER"],
                       input=sql.rstrip().rstrip(";") + ";", capture_output=True, text=True)
    out = "\n".join(l for l in r.stdout.splitlines() if "JDK_JAVA_OPTIONS" not in l)
    if r.returncode != 0 or re.search(r"^(Query \S+ failed|line \d+:)", out + r.stderr, re.M):
        print(f"{name} FAILED:", (r.stderr or out)[:400], file=sys.stderr); open(path + ".err", "w").write(r.stderr + out); continue
    open(path, "w").write(out + "\n"); print(f"{name}: {max(0, out.count(chr(10)))} rows")
