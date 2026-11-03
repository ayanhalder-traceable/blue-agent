#!/usr/bin/env python3
"""Validate the attribution definition files written by agents 02 and 06 against contracts/schemas/definitions.json. Standard library only.
Usage: validate_defs.py <dir holding user-attribution.json, session-identification.json, entity-definitions.json> [--verify verify.csv]
With --verify, a definition that did not extract any span on the window (extracted = 0 or error) is a failure."""
import csv, json, os, sys

here = os.path.dirname(os.path.abspath(__file__))
S = json.load(open(f"{here}/schemas/definitions.json"))
errs = []
def err(f, n, m): errs.append(f"{f}: {n}: {m}")

def loc_ok(f, n, l):
    if not isinstance(l, dict) or l.get("type") not in S["location_types"] or not l.get("key"): err(f, n, f"bad location {l}"); return
def chain_ok(f, n, a):
    loc_ok(f, n, a.get("location"))
    for c in a.get("conditions", []):
        loc_ok(f, n, c.get("location"))
        if c.get("operator") not in S["operators"] or "value" not in c: err(f, n, f"bad condition {c}")
    for t in a.get("value_transformations", []):
        if t.get("type") not in S["transformation_types"]: err(f, n, f"unknown transformation {t.get('type')}")
def scope_ok(f, n, sc):
    for k in S["scope_required"]:
        if k not in sc: err(f, n, f"scope missing {k}")

def load(name):
    p = sys.argv[1] + "/" + name
    if not os.path.exists(p): err(name, "-", "missing"); return None
    d = json.load(open(p))
    if d.get("schema") != S["schema"]: err(name, "-", f"schema {d.get('schema')} != {S['schema']}")
    return d

d = load("user-attribution.json")
if d:
    sp = S["user-attribution"]
    for x in d.get("definitions", []):
        n = x.get("name", "?")
        for k in sp["required"]:
            if k not in x: err("user-attribution", n, f"missing {k}")
        for k in sp["required_attributes"]:
            if k not in x.get("attributes", {}): err("user-attribution", n, f"missing attribute {k}")
        for k, a in x.get("attributes", {}).items():
            if k not in sp["required_attributes"] + sp["optional_attributes"]: err("user-attribution", n, f"unknown attribute {k}")
            if set(a) == {"constant"} and isinstance(a["constant"], str): continue  # authType is a fixed label, not extracted
            chain_ok("user-attribution", n, a)
        scope_ok("user-attribution", n, x.get("scope", {}))
        if x.get("confidence") not in S["confidence"]: err("user-attribution", n, "bad confidence")
    for x in d.get("excluded", []):
        for k in sp["excluded_required"]:
            if k not in x: err("user-attribution", x.get("carrier", "?"), f"excluded missing {k}")

d = load("session-identification.json")
if d:
    sp = S["session-identification"]
    for x in d.get("definitions", []):
        n = x.get("name", "?")
        for k in sp["required"]:
            if k not in x: err("session-identification", n, f"missing {k}")
        scope_ok("session-identification", n, x.get("scope", {}))
        for i in x.get("identifiers", []):
            for k in sp["identifier_required"]:
                if k not in i: err("session-identification", n, f"identifier missing {k}")
            if i.get("type") not in sp["identifier_types"]: err("session-identification", n, "bad identifier type")
            chain_ok("session-identification", n, dict(location=i.get("token", {}).get("location"), conditions=i.get("conditions", []), value_transformations=i.get("value_transformations", [])))
        if x.get("confidence") not in S["confidence"]: err("session-identification", n, "bad confidence")

d = load("entity-definitions.json")
if d:
    sp = S["entity"]
    for x in d.get("definitions", []):
        n = x.get("name", "?")
        for k in sp["required"]:
            if k not in x and not (x.get("source_agent") == "06" and k in sp["gap_fill_relaxed"]): err("entity", n, f"missing {k}")
        if x.get("entity_kind") not in S["entity_kinds"]: err("entity", n, f"bad entity_kind {x.get('entity_kind')}")
        if x.get("source_agent") == "02":
            if x.get("entity_type") not in S["entity_types"]: err("entity", n, f"bad entity_type {x.get('entity_type')}")
            if x.get("population") not in S["entity_populations"]: err("entity", n, "bad population")
            if x.get("hunt_use") not in S["entity_hunt_use"]: err("entity", n, "bad hunt_use")
            if x.get("population") == "feed" and x.get("hunt_use") != "investigate-only": err("entity", n, "a feed entity must be investigate-only")
            if x.get("whose") not in S["entity_whose"]: err("entity", n, f"bad whose {x.get('whose')}")
            if n != f"{x.get('entity_type')}.{x.get('whose')}": err("entity", n, "name must be <entity_type>.<whose>")
            c = x.get("confidence")
            if not isinstance(c, dict) or not all(k in c for k in ("score", "band", "components", "gates")): err("entity", n, "confidence must be {score, band, components, gates}")
            elif c["band"] not in ("extract", "review"): err("entity", n, "a rejected entity must not be shipped")
            elif c["band"] == "review" and x.get("hunt_use") == "hunt": err("entity", n, "a review-band entity must be investigate-only")
            if not x.get("description"): err("entity", n, "description is required")
            for df in x.get("definitions", []):
                if "pair" in df and not (isinstance(df["pair"], list) and len(df["pair"]) == 2): err("entity", n, "pair must be [label, valueKey]")
                if "combine" in df and df["combine"].get("op") != "concat": err("entity", n, "combine.op must be concat")
        if x.get("source_agent") not in sp["source_agents"]: err("entity", n, "bad source_agent")
        if x.get("source_agent") == "06":
            for k in sp["gap_fill_required"]:
                if k not in x: err("entity", n, f"the entity gap-fill stage (not in this repo) definition missing {k}: it exists only to fill a named gap")
        for df in x.get("definitions", []): chain_ok("entity", n, df)
        scope_ok("entity", n, x.get("scope", {}))

if "--verify" in sys.argv:
    for r in csv.DictReader(open(sys.argv[sys.argv.index("--verify") + 1])):
        if r["status"] != "ok": err("verify", r["name"], f"did not run: {r['error'][:120]}")
        elif int(r.get("extracted") or 0) == 0: err("verify", r["name"], f"extracted 0 of {r.get('spans')} spans in the window")
print("\n".join(errs) if errs else "OK")
sys.exit(1 if errs else 0)
