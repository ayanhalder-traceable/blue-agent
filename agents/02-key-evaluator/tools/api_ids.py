#!/usr/bin/env python3
"""The platform ids of the APIs seen in a window, so a definition's API paths can be turned into the ids the platform's entity scope takes.
`span_event_view.api_id` and `service_id` are the same ids the platform stores in `entity_scope.entity_ids` (uuid format); the id is read from the span, not from the platform's own API table.
Writes <out>/api_ids.csv: api_name, api_id, service_id, service_name, spans. No tenant names in here, no user or credential values.
Usage: api_ids.py --customer <uuid> --env <environment> --start 'YYYY-MM-DD HH:MM' [--minutes 60] --out <dir>"""
import argparse, re, subprocess, sys, os
ap = argparse.ArgumentParser()
ap.add_argument("--customer", required=True); ap.add_argument("--env", required=True); ap.add_argument("--start", required=True)
ap.add_argument("--minutes", type=int, default=60); ap.add_argument("--out", required=True)
a = ap.parse_args()
assert re.fullmatch(r"[0-9a-f-]{36}", a.customer) and re.fullmatch(r"[A-Za-z0-9._-]+", a.env) and re.fullmatch(r"\d{4}-\d\d-\d\d \d\d:\d\d", a.start)
sql = f"""SELECT api_name, api_id, service_id, max(service_name) service_name, count(*) spans FROM span_event_view
WHERE customer_id = '{a.customer}' AND environment = '{a.env}' AND api_id IS NOT NULL AND api_name IS NOT NULL
AND start_time_millis_ts >= timestamp '{a.start}:00' AND start_time_millis_ts < timestamp '{a.start}:00' + interval '{a.minutes}' minute
GROUP BY 1, 2, 3"""
os.makedirs(a.out, exist_ok=True)
r = subprocess.run(["kubectl", "-n", "traceable", "exec", "-i", "deployment/trino-coordinator-ro", "-c", "trino-coordinator-ro", "--",
                    "trino", "--server", "localhost:8080", "--catalog", "iceberg", "--schema", "iceberg_datalake", "--output-format", "CSV_HEADER"],
                   input=sql + ";", capture_output=True, text=True)
out = "\n".join(l for l in r.stdout.splitlines() if "JDK_JAVA_OPTIONS" not in l)
if r.returncode != 0 or re.search(r"^(Query \S+ failed|line \d+:)", out + r.stderr, re.M): sys.exit("api_ids FAILED: " + (r.stderr or out)[:400])
open(f"{a.out}/api_ids.csv", "w").write(out + "\n"); print("api_ids:", out.count("\n"), "rows")
