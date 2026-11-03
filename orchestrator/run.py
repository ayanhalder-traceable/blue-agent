#!/usr/bin/env python3
"""Scheduler and readiness checker for the Blue agents. Standard library only.
It does not run agents. It answers "what is due, what is blocked, what is stale" from the plan and a run ledger, and records finished runs after their output passes its contract check.
  run.py status --tenant T --env E          every node: never-run | blocked | stale | due | fresh, with the reason
  run.py due    --tenant T --env E          only the nodes that should run now, in dependency order
  run.py record NODE --tenant T --env E --output contract=path [...] [--status ok|failed]
The ledger is a JSON-lines file (--store, default ./tmp/store/runs.jsonl). Swap it for the database later; the record shape stays the same."""
import argparse, datetime as dt, json, os, subprocess, sys, uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLAN = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "plan.json")))
NODES, CONTRACTS = PLAN["nodes"], PLAN["contracts"]
now = lambda: dt.datetime.now(dt.timezone.utc)
parse = lambda s: dt.datetime.fromisoformat(s)

def load(store, tenant, env):
    if not os.path.exists(store): return []
    rows = [json.loads(l) for l in open(store) if l.strip()]
    return [r for r in rows if r["tenant"] == tenant and r["env"] == env and r["status"] == "ok"]

def last(rows, node):
    r = [x for x in rows if x["node"] == node]
    return max(r, key=lambda x: x["at"]) if r else None

def producer(contract):
    return next(n for n, d in NODES.items() if contract in d["produces"])

def order():
    seen, out = set(), []
    def visit(n):
        if n in seen: return
        seen.add(n)
        for need in NODES[n]["needs"]: visit(producer(need["contract"]))
        out.append(n)
    for n in NODES: visit(n)
    return out

def state(rows, node):
    d, me = NODES[node], last(rows, node)
    for need in d["needs"]:
        p = last(rows, producer(need["contract"]))
        if not p: return "blocked", f"input '{need['contract']}' has never been produced"
        age = (now() - parse(p["at"])).days
        if age > need["max_age_days"]: return "blocked", f"input '{need['contract']}' is {age} days old (max {need['max_age_days']}); run {producer(need['contract'])} first"
    if not me: return "never-run", "no successful run yet"
    for need in d["needs"]:
        p = last(rows, producer(need["contract"]))
        if p["at"] > me["at"]: return "stale", f"input '{need['contract']}' was produced after this node last ran"
    age = (now() - parse(me["at"])).days
    if age >= d["schedule_days"]: return "due", f"last ran {age} days ago (schedule every {d['schedule_days']})"
    return "fresh", f"last ran {age} days ago"

def main():
    ap = argparse.ArgumentParser(); ap.add_argument("cmd", choices=["status", "due", "record"]); ap.add_argument("node", nargs="?")
    ap.add_argument("--tenant", required=True); ap.add_argument("--env", required=True)
    ap.add_argument("--store", default=os.path.join(ROOT, "tmp", "store", "runs.jsonl"))
    ap.add_argument("--output", action="append", default=[]); ap.add_argument("--status", default="ok", choices=["ok", "failed"])
    a = ap.parse_args(); rows = load(a.store, a.tenant, a.env)
    if a.cmd in ("status", "due"):
        for n in order():
            s, why = state(rows, n)
            if a.cmd == "status" or s in ("never-run", "stale", "due"): print(f"{s:10} {n:36} {why}")
        return
    if a.node not in NODES: sys.exit(f"unknown node {a.node}")
    outs = dict(o.split("=", 1) for o in a.output)
    if a.status == "ok":
        if set(outs) != set(NODES[a.node]["produces"]): sys.exit(f"{a.node} must record outputs for exactly: {NODES[a.node]['produces']}")
        for c, path in outs.items():  # a run counts only if its output passes the contract check
            cmd = [x.replace("{path}", path) for x in CONTRACTS[c]["validate"]]
            r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
            if r.returncode: sys.exit(f"contract '{c}' check failed, run not recorded:\n{r.stdout[-600:]}{r.stderr[-300:]}")
    inputs = [{"contract": n["contract"], "run_id": (last(rows, producer(n["contract"])) or {}).get("run_id")} for n in NODES[a.node]["needs"]]
    rec = dict(run_id=uuid.uuid4().hex[:12], node=a.node, tenant=a.tenant, env=a.env, at=now().isoformat(), status=a.status,
               outputs=[dict(contract=c, path=p, version=CONTRACTS[c]["version"]) for c, p in outs.items()], inputs=inputs)
    os.makedirs(os.path.dirname(a.store), exist_ok=True); open(a.store, "a").write(json.dumps(rec) + "\n"); print("recorded", rec["run_id"])
main()
