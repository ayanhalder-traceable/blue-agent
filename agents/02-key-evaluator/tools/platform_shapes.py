#!/usr/bin/env python3
"""What the platform's GraphQL accepts for the three attribution mutations, and a validator for a payload.
  platform_shapes.py refresh --url <graphql endpoint> [--token-file <file>]   introspect (read-only: schema queries only, no mutation) and write platform_shapes.json beside this file
  platform_shapes.py show                                                      list every mutation with its accepted enum values and fields
The token is read from the TRACEABLE_TOKEN env var, or from --token-file; it is never printed or written.
Library use: validate(mutation_name, variables) -> list of problems (empty = every field name, enum value and type is one the platform takes)."""
import json, os, sys, urllib.request, argparse

HERE = os.path.dirname(os.path.abspath(__file__))
SNAP = os.path.join(HERE, "platform_shapes.json")
MUTATIONS = ("createUserAttributionRuleV2", "createSessionIdentificationRuleV2", "CreateEntityDerivationConfig")
TR = "kind name ofType{kind name ofType{kind name ofType{kind name ofType{kind name}}}}"
SCALARS = {"String", "Boolean", "Int", "Float", "ID", "Long", "Map", "JSON", "Time", "DateTime"}

def _base(t):
    while t.get("ofType"): t = t["ofType"]
    return t["name"]

def _is_list(t):
    while t:
        if t["kind"] == "LIST": return True
        t = t.get("ofType")
    return False

def _is_required(t): return t["kind"] == "NON_NULL"

def refresh(url, token):
    def q(query, variables=None):
        r = urllib.request.Request(url, json.dumps({"query": query, "variables": variables or {}}).encode(), {"content-type": "application/json", "authorization": "Bearer " + token})
        return json.load(urllib.request.urlopen(r, timeout=60))
    ms = q("{__schema{mutationType{fields{name args{name type{%s}}}}}}" % TR)["data"]["__schema"]["mutationType"]["fields"]
    snap, todo = {"mutations": {}, "types": {}}, []
    for f in ms:
        if f["name"] in MUTATIONS:
            snap["mutations"][f["name"]] = {a["name"]: {"type": _base(a["type"]), "list": _is_list(a["type"]), "required": _is_required(a["type"])} for a in f["args"]}
            todo += [_base(a["type"]) for a in f["args"]]
    while todo:
        n = todo.pop()
        if n in snap["types"] or n in SCALARS: continue
        r = q('query($n:String!){__type(name:$n){kind name enumValues{name} inputFields{name type{%s}}}}' % TR, {"n": n})["data"]["__type"]
        if not r: continue
        if r["kind"] == "ENUM": snap["types"][n] = {"kind": "ENUM", "values": [e["name"] for e in r["enumValues"]]}
        elif r["kind"] == "INPUT_OBJECT":
            snap["types"][n] = {"kind": "INPUT", "fields": {x["name"]: {"type": _base(x["type"]), "list": _is_list(x["type"]), "required": _is_required(x["type"])} for x in r["inputFields"]}}
            todo += [_base(x["type"]) for x in r["inputFields"]]
        else: snap["types"][n] = {"kind": r["kind"]}
    missing = [m for m in MUTATIONS if m not in snap["mutations"]]
    if missing: sys.exit(f"mutations not found on the platform: {missing}")
    json.dump(snap, open(SNAP, "w"), indent=1, sort_keys=True)
    return snap

_snap = None
def snapshot():
    global _snap
    if _snap is None: _snap = json.load(open(SNAP)) if os.path.exists(SNAP) else {"mutations": {}, "types": {}}
    return _snap

def _check(val, tname, path, out, S):
    t = S["types"].get(tname)
    if t is None:
        if tname in SCALARS: return
        out.append(f"{path}: type '{tname}' unknown to the snapshot"); return
    if t["kind"] == "ENUM":
        if val not in t["values"]: out.append(f"{path}: '{val}' is not one of {tname}")
    elif t["kind"] == "INPUT":
        if not isinstance(val, dict): out.append(f"{path}: expected an object ({tname})"); return
        for k, v in val.items():
            f = t["fields"].get(k)
            if f is None: out.append(f"{path}.{k}: the platform's {tname} has no such field"); continue
            if v is None: continue
            if f["list"]:
                if not isinstance(v, list): out.append(f"{path}.{k}: expected a list"); continue
                for i, x in enumerate(v): _check(x, f["type"], f"{path}.{k}[{i}]", out, S)
            else: _check(v, f["type"], f"{path}.{k}", out, S)
        for k, f in t["fields"].items():
            if f["required"] and k not in val: out.append(f"{path}.{k}: required by {tname} and missing")

def validate(mutation, variables):
    S = snapshot(); out = []
    m = S["mutations"].get(mutation)
    if m is None: return [f"mutation '{mutation}' is not in the snapshot: run platform_shapes.py refresh"]
    for k, v in variables.items():
        a = m.get(k)
        if a is None: out.append(f"{k}: {mutation} has no such argument"); continue
        _check(v, a["type"], k, out, S)
    for k, a in m.items():
        if a["required"] and k not in variables: out.append(f"{k}: required by {mutation} and missing")
    return out

if __name__ == "__main__":
    p = argparse.ArgumentParser(); p.add_argument("cmd", choices=["refresh", "show"]); p.add_argument("--url", default="https://app-dev.traceable.ai/graphql"); p.add_argument("--token-file")
    a = p.parse_args()
    if a.cmd == "refresh":
        tok = os.environ.get("TRACEABLE_TOKEN") or (open(a.token_file).read().strip() if a.token_file else "")
        if not tok: sys.exit("set TRACEABLE_TOKEN or pass --token-file")
        s = refresh(a.url, tok); print(SNAP, {"mutations": len(s["mutations"]), "types": len(s["types"])})
    else:
        S = snapshot()
        for n, m in S["mutations"].items(): print(n, {k: v["type"] for k, v in m.items()})
        for n, t in sorted(S["types"].items()):
            print(n, t["values"] if t["kind"] == "ENUM" else {k: v["type"] + ("[]" if v["list"] else "") + ("!" if v["required"] else "") for k, v in t.get("fields", {}).items()})
