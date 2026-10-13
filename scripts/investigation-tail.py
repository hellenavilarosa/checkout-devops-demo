#!/usr/bin/env python3
"""Tail the journal of an AWS DevOps Agent investigation from the terminal.

Usage:
  investigation-tail.py [EXECUTION_ID] [--all] [--n N]

Defaults to the Agent Space in ../.agent-space-id and the newest INVESTIGATION
task. Prints assistant thinking/text and tool calls, newest last.
"""

import argparse
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REGION = "us-east-1"


def aws(*args):
    out = subprocess.run(["aws", "devops-agent", *args, "--region", REGION, "--output", "json"],
                         capture_output=True, text=True)
    if out.returncode != 0:
        sys.exit(out.stderr.strip())
    return json.loads(out.stdout)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("execution_id", nargs="?")
    ap.add_argument("--n", type=int, default=15, help="records to show (default 15)")
    ap.add_argument("--all", action="store_true", help="include tool results")
    a = ap.parse_args()

    with open(os.path.join(HERE, "..", ".agent-space-id")) as fh:
        space = fh.read().strip()

    ex = a.execution_id
    if not ex:
        tasks = aws("list-backlog-tasks", "--agent-space-id", space)
        inv = [t for t in tasks.get("tasks", tasks.get("backlogTasks", [])) if t.get("taskType") == "INVESTIGATION"]
        if not inv:
            sys.exit("no investigation found")
        t = sorted(inv, key=lambda x: x["createdAt"])[-1]
        ex = t["executionId"]
        print(f"task {t['taskId']} [{t['status']}] {t['title']}\nexecution {ex}\n")

    data = aws("list-journal-records", "--agent-space-id", space, "--execution-id", ex,
               "--record-type", "message")
    recs = next((v for v in data.values() if isinstance(v, list)), [])
    recs.sort(key=lambda r: r.get("createdAt", ""))

    lines = []
    for r in recs:
        c = r.get("content")
        try:
            m = json.loads(c) if isinstance(c, str) else c
        except (TypeError, ValueError):
            m = {"role": "?", "content": [{"text": str(c)}]}
        role = m.get("role", "?")
        for p in m.get("content", []):
            if "thinking" in p:
                txt = "[thinking] " + p["thinking"]
            elif "text" in p and role == "assistant":
                txt = p["text"]
            elif "toolUse" in p:
                tu = p["toolUse"]
                txt = f"[tool:{tu.get('name')}] " + json.dumps(tu.get("input", {}))[:200]
            elif "name" in p and "input" in p:
                txt = f"[tool:{p.get('name')}] " + json.dumps(p.get("input", {}))[:200]
            elif a.all:
                txt = "[result] " + json.dumps(p)[:200]
            else:
                continue
            lines.append(f"{r.get('createdAt', '')[11:19]} {role:9s} {txt[:400]}".replace("\n", " "))

    for ln in lines[-a.n:]:
        print(ln)


if __name__ == "__main__":
    main()
