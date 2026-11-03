#!/usr/bin/env python3
"""Check a hand-off contract (and optionally a reader's ack). Standard library only."""
import json, os, sys, argparse

KINDS = {"fact", "decision", "filter", "exclude", "lane", "gap", "risk", "open_item"}
HERE = os.path.dirname(os.path.abspath(__file__))
errs = []

def err(m): errs.append(m)

def empty(v): return v is None or v == "" or v == [] or v == {}

def check_contract(c):
    for k in ("contract", "agent", "tenant", "environment", "window", "source", "produced_at", "report", "entries", "tables"):
        if empty(c.get(k)) and k != "tables": err(f"envelope: missing {k}")
    if c.get("contract") != "blue-agent/handoff@1": err("envelope: contract must be blue-agent/handoff@1")
    if c.get("source") not in ("spans", "profiling", "mixed"): err("envelope: source must be spans|profiling|mixed")
    ids = {}
    for e in c.get("entries", []):
        i = e.get("id", "?")
        if i in ids: err(f"entry {i}: duplicate id")
        ids[i] = e
        if e.get("kind") not in KINDS: err(f"entry {i}: kind {e.get('kind')!r} not in closed list")
        if not str(i).startswith(str(e.get("kind")) + "."): err(f"entry {i}: id must start with '{e.get('kind')}.'")
        if empty(e.get("statement")): err(f"entry {i}: empty statement")
        ev = e.get("evidence") or {}
        if not isinstance(e.get("confidence"), (int, float)) or isinstance(e.get("confidence"), bool) or not 0 <= e["confidence"] <= 1: err(f"entry {i}: confidence must be a number from 0 to 1")
        if e.get("kind") in ("filter", "exclude") and empty((e.get("value") or {}).get("sql")): err(f"entry {i}: filter/exclude needs value.sql")
        if isinstance(e.get("confidence"), (int, float)) and e["confidence"] < 0.5 and not any(o.get("kind") == "open_item" and i in str(o.get("value", "")) for o in c["entries"]):
            err(f"entry {i}: entries under 0.5 confidence need an open_item whose value names '{i}'")
    sp = os.path.join(HERE, "schemas", c.get("agent", "") + ".json")
    if not os.path.exists(sp):
        err(f"no schema file for agent {c.get('agent')}"); return ids
    s = json.load(open(sp))
    for r in s["required_entries"]:
        e = ids.get(r["id"])
        if not e: err(f"required entry missing: {r['id']}"); continue
        if e.get("kind") != r["kind"]: err(f"{r['id']}: kind must be {r['kind']}")
        for vk in r.get("value_keys", []):
            if empty((e.get("value") or {}).get(vk)): err(f"{r['id']}: value.{vk} missing")
    for k, n in s.get("min_entries_by_kind", {}).items():
        have = sum(1 for e in c["entries"] if e.get("kind") == k)
        if have < n: err(f"need at least {n} '{k}' entries, found {have}")
    tabs = c.get("tables", {})
    for t, cols in s["required_tables"].items():
        T = tabs.get(t)
        if not T or empty(T.get("rows")): err(f"table {t}: missing or empty"); continue
        if list(T.get("columns", [])) != cols: err(f"table {t}: columns must be {cols}")
        for n, row in enumerate(T["rows"]):
            for col in cols:
                if empty(row.get(col)) and row.get(col) != 0: err(f"table {t} row {n}: empty {col}")
    for t, spec in s.get("conditional_tables", {}).items():
        w = spec["when"]; got = (ids.get(w["entry"], {}).get("value") or {}).get(w["key"])
        if isinstance(got, (int, float)) and got > w.get("gt", 0):
            T = tabs.get(t)
            if not T or empty(T.get("rows")): err(f"table {t}: required because {w['entry']}.{w['key']}={got}"); continue
            if list(T.get("columns", [])) != spec["columns"]: err(f"table {t}: columns must be {spec['columns']}")
            for n, row in enumerate(T["rows"]):
                for col in spec["columns"]:
                    if empty(row.get(col)) and row.get(col) != 0: err(f"table {t} row {n}: empty {col}")
    for key, allowed in s.get("enums", {}).items():
        t, col = key.split(".")
        for n, row in enumerate(tabs.get(t, {}).get("rows", [])):
            if row.get(col) not in allowed: err(f"table {t} row {n}: {col}={row.get(col)!r} not in {allowed}")
    for ck in s.get("count_checks", []):
        want = (ids.get(ck["entry"], {}).get("value") or {}).get(ck["key"])
        have = len(tabs.get(ck["table"], {}).get("rows", []))
        if want is not None and want != have: err(f"table {ck['table']} has {have} rows but {ck['entry']}.{ck['key']} says {want}")
    lv = s.get("lane_verdicts", {})
    worthy = {r.get("api_name"): r.get("hunt_worthy") for r in tabs.get("distribution", {}).get("rows", [])}
    ok_w = s.get("verdict_consistency", {}).get("hunt_requires_worthy", [])
    for n, row in enumerate(tabs.get("apis", {}).get("rows", [])):
        l, h = row.get("lane"), row.get("hunt_verdict")
        if l in lv and h not in lv[l]: err(f"table apis row {n} ({row.get('api_name')}): lane {l} cannot have verdict {h}; allowed {lv[l]}")
        if h == "hunt" and row.get("api_name") in worthy and worthy[row["api_name"]] not in ok_w:
            err(f"table apis row {n} ({row.get('api_name')}): verdict hunt but distribution.hunt_worthy={worthy[row['api_name']]!r}")
        if row.get("evidence") and len(str(row["evidence"])) < 15: err(f"table apis row {n}: evidence too short")
    for t in s.get("shared_tables", []):
        if t in tabs and empty(tabs[t].get("consumers")): err(f"table {t}: must list its consumers")
    return ids

def check_ack(c, ack, reader):
    answers = {a.get("ref"): a for a in ack.get("answers", [])}
    for a in ack.get("answers", []):
        st = a.get("status")
        if st not in ("used", "overridden", "not_applicable"): err(f"ack {a.get('ref')}: bad status {st!r}")
        if st in ("overridden", "not_applicable") and empty(a.get("reason")): err(f"ack {a.get('ref')}: {st} needs a reason")
        if st == "used" and empty(a.get("where")): err(f"ack {a.get('ref')}: used needs 'where'")
    for e in c["entries"]:
        if reader in e.get("consumers", []) and e["id"] not in answers:
            err(f"ack: no answer for entry {e['id']}")
    for t, T in c.get("tables", {}).items():
        if reader in T.get("consumers", []) and ("table." + t) not in answers: err(f"ack: no answer for table.{t}")

if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("contract"); p.add_argument("--ack"); p.add_argument("--reader")
    a = p.parse_args()
    c = json.load(open(a.contract))
    check_contract(c)
    if a.ack:
        if not a.reader: sys.exit("--reader is required with --ack")
        check_ack(c, json.load(open(a.ack)), a.reader)
    if errs:
        print(f"FAIL ({len(errs)} problems)"); [print(" -", e) for e in errs[:80]]; sys.exit(1)
    print("OK:", len(c["entries"]), "entries,", sum(len(t["rows"]) for t in c["tables"].values()), "table rows")
