#!/usr/bin/env python3
"""Check a packaged agent-02 deliverable (blue-agent/attribution@4). Standard library only.
Usage: validate_attribution.py <Tenant>-<env>-attribution.json [...]"""
import json, os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "agents", "02-key-evaluator", "tools"))
import platform_shapes

HERE = os.path.dirname(os.path.abspath(__file__))
E = json.load(open(os.path.join(HERE, "schemas", "definitions.json")))["attribution_envelope"]
NAME = {"user": None, "session": None, "entity": r"^[a-z0-9]+(-[a-z0-9]+)*\.[a-z0-9]+(-[a-z0-9]+)*$"}

MUT = {"user": "createUserAttributionRuleV2", "session": "createSessionIdentificationRuleV2", "entity": "CreateEntityDerivationConfig"}

def check(f):
    errs = []; err = errs.append
    d = json.load(open(f))
    rf = f[:-5] + "-review.json"
    if not os.path.exists(rf): err(f"review file {os.path.basename(rf)} is missing"); return errs
    R = json.load(open(rf))
    if d.get("schema") != E["schema"]: err(f"schema must be {E['schema']}")
    if R.get("schema") != E["review_schema"]: err(f"review schema must be {E['review_schema']}")
    if set(d) != set(E["top_level"]): err(f"top-level keys must be exactly {E['top_level']}; got extra {sorted(set(d) - set(E['top_level']))} missing {sorted(set(E['top_level']) - set(d))}")
    if set(R) != set(E["review_top_level"]): err(f"review top-level keys must be exactly {E['review_top_level']}")
    seen = set()
    for sec, kind in (("users", "user"), ("sessions", "session"), ("entities", "entity")):
        if len(d.get(sec, [])) != len(R.get(sec, [])): err(f"{sec}: payload file and review file differ in length"); continue
        for it, rv in zip(d.get(sec, []), R.get(sec, [])):
            x = rv.get("assessment") if isinstance(rv.get("assessment"), dict) else {}
            n = x.get("entity_name", "?")
            if (kind, n) in seen: err(f"{sec} {n}: duplicate name")
            seen.add((kind, n))
            if set(it) != set(E["item_required"]): err(f"{sec} {n}: payload item keys must be exactly {E['item_required']} (no scores or evidence in the deliverable)")
            if set(rv) != set(E["review_item_required"]) or rv.get("kind") != kind or rv.get("mutation") != it.get("mutation"): err(f"{sec} {n}: review item must be {E['review_item_required']} and match its payload")
            if it.get("mutation") != MUT[kind] or not isinstance(it.get("variables"), dict): err(f"{sec} {n}: mutation must be the one for {kind} and variables an object")
            else:
                for p in platform_shapes.validate(it["mutation"], it["variables"])[:5]: err(f"{sec} {n}: payload does not fit the platform input: {p}")
            need = E["assessment_required"] + (E["entity_extra_required"] if kind == "entity" else [])
            for k in need:
                if k not in x: err(f"{sec} {n}: assessment missing {k}")
            for k in x:
                if k not in need: err(f"{sec} {n}: assessment key {k!r} is not in the contract")
            if not isinstance(x.get("ready_to_submit"), bool): err(f"{sec} {n}: ready_to_submit must be true or false")
            if kind == "entity" and x.get("ready_to_submit") and not all(c["scope"]["entity_scope"]["entity_ids"] for c in it["variables"]["input"]["data"]["span_projection"]["event_derivation_configs"]): err(f"{sec} {n}: ready_to_submit is true but an entity scope has no API id")
            c = x.get("confidence")
            if not isinstance(c, (int, float)) or isinstance(c, bool) or not .65 <= c <= 1: err(f"{sec} {n}: confidence must be a number from 0.65 to 1 (lower is not shipped)")
            ev = x.get("evidence", {}); ne = E["evidence_required"] + (E["entity_evidence_extra"] if kind == "entity" else [])
            for k in ne:
                if k not in ev: err(f"{sec} {n}: evidence missing {k}")
            for k in ev:
                if k not in ne: err(f"{sec} {n}: evidence key {k!r} is not in the contract")
            if (ev.get("requests_matched") or 0) <= 0: err(f"{sec} {n}: matched no requests; it must not be shipped")
            df = x.get("definitions", {}); sc = df.get("scope")
            if sc in ("all-apis", "listed-apis"):
                if not df.get("extract"): err(f"{sec} {n}: definitions.extract is empty")
            elif sc == "per-api":
                if len(df.get("per_api", [])) < 2: err(f"{sec} {n}: per-api needs at least two groups; otherwise use all-apis")
                for g in df.get("per_api", []):
                    if not all(k in g for k in ("apis", "methods", "extract")) or not g["extract"]: err(f"{sec} {n}: per_api group needs apis, methods, extract")
            else: err(f"{sec} {n}: definitions.scope {sc!r} not allowed")
            if kind == "entity":
                import re
                if not re.match(NAME["entity"], n): err(f"entities {n}: name must be <what>.<whose>")
                if x.get("population") not in ("end-user", "feed"): err(f"entities {n}: population must be end-user or feed")
                if x.get("use") not in ("hunt", "investigate-only"): err(f"entities {n}: use must be hunt or investigate-only")
                if x.get("use") == "hunt" and (c or 0) < .85: err(f"entities {n}: hunt needs confidence 0.85 or more")
                if x.get("use") == "hunt" and x.get("population") != "end-user": err(f"entities {n}: only end-user entities may be hunted")
                if not isinstance(x.get("sensitive"), bool): err(f"entities {n}: sensitive must be true or false")
    for r in R.get("not_extracted", []):
        if set(r) != set(E["not_extracted_row"]) or r.get("kind") not in E["kinds"] or not r.get("reason"): err(f"not_extracted row {r.get('entity_name')}: needs exactly kind, entity_name, reason")
    return errs

bad = 0
for f in sys.argv[1:]:
    e = check(f); bad += bool(e)
    print(("OK   " if not e else f"FAIL ({len(e)}) ") + os.path.basename(f))
    for m in e[:15]: print("  -", m)
sys.exit(1 if bad else 0)
