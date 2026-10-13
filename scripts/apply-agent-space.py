#!/usr/bin/env python3
"""Apply the Agent Space content under agent-space/ (skills, memory store + memories,
custom agent, scheduled trigger) through the AWS DevOps Agent Asset API.

Idempotent: assets are matched by (assetType, metadata.name); existing ones are
updated in place, missing ones are created. The trigger is matched by its target
custom agent. Usage:

  python3 scripts/apply-agent-space.py            # apply everything
  python3 scripts/apply-agent-space.py --dry-run  # show what would change

Reads the Agent Space id from .agent-space-id at the repo root.
"""

import argparse
import json
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REGION = os.environ.get("AWS_REGION", "us-east-1")
AGENT_SPACE_ID = open(os.path.join(ROOT, ".agent-space-id")).read().strip()

# Agent types that see each asset. INCIDENT_* targets keep triage skills out of
# unrelated sessions (less context, faster triage).
SKILLS = {
    "runbook-argocd-rollback": {"agent_types": ["GENERIC"]},
    "skip-canary-window": {"agent_types": ["INCIDENT_TRIAGE"]},
}
MEMORY_STORE = {
    "name": "checkout-directives",
    "description": "Standing conventions for the checkout-api service on EKS: how it is "
    "deployed (ArgoCD, GitOps), its dependencies and how mitigations must be applied. "
    "Open this store for any incident or question about checkout-api or namespace checkout.",
    "agent_types": ["GENERIC"],
}
MEMORIES = {
    "checkout-gitops-conventions": {
        "name": "services/checkout-api/deployment-conventions",
        "description": "How checkout-api is deployed via ArgoCD (auto-sync, self-heal, prune), "
        "why kubectl changes are reverted, its Redis dependency and probe behaviour.",
    }
}
CUSTOM_AGENT = {
    "name": "eks-daily-health",
    # Built-in tool identifiers documented for custom agents. Other tools (integrations,
    # MCP servers) are easiest to add through Chat: "Add the X tool to my eks-daily-health agent."
    "tools": ["use_aws", "query_cloudwatch_logs"],
    "skills": ["runbook-argocd-rollback"],
    "memory_stores": ["checkout-directives"],
}
TRIGGER_SCHEDULE = "cron(0 11 * * ? *)"  # 11:00 UTC = 08:00 America/Sao_Paulo


def aws(*args, payload=None):
    cmd = ["aws", "devops-agent", *args, "--region", REGION, "--output", "json"]
    if payload is not None:
        cmd += ["--cli-input-json", json.dumps(payload)]
    out = subprocess.run(cmd, capture_output=True, text=True)
    if out.returncode != 0:
        sys.exit(f"aws devops-agent {args[0]} failed:\n{out.stderr.strip()}")
    return json.loads(out.stdout) if out.stdout.strip() else {}


def frontmatter(md):
    m = re.match(r"^---\n(.*?)\n---\n", md, re.S)
    if not m:
        return {}
    data, key = {}, None
    for line in m.group(1).splitlines():
        if re.match(r"^\w+:", line):
            key, _, val = line.partition(":")
            data[key.strip()] = val.strip()
        elif key:
            data[key] += " " + line.strip()
    return data


def read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


class Space:
    def __init__(self, dry_run):
        self.dry = dry_run
        items, token = [], None
        while True:
            args = ["list-assets", "--agent-space-id", AGENT_SPACE_ID, "--max-results", "100"]
            if token:
                args += ["--next-token", token]
            page = aws(*args)
            items += page.get("items", [])
            token = page.get("nextToken")
            if not token:
                break
        self.by_key = {(a["assetType"], a.get("metadata", {}).get("name")): a for a in items}

    def upsert(self, asset_type, metadata, path, text):
        key = (asset_type, metadata["name"])
        existing = self.by_key.get(key)
        if self.dry:
            print(f"{'update' if existing else 'create'} {asset_type} {metadata['name']}")
            return existing["assetId"] if existing else f"<new-{metadata['name']}>"
        if existing:
            aid = existing["assetId"]
            upd = {k: v for k, v in metadata.items() if k != "memory_store_id"}  # create-only field
            aws("update-asset", payload={"agentSpaceId": AGENT_SPACE_ID, "assetId": aid, "metadata": upd})
            aws("update-asset-file", payload={"agentSpaceId": AGENT_SPACE_ID, "assetId": aid,
                                              "path": path, "content": {"text": text}})
            print(f"updated {asset_type} {metadata['name']} ({aid})")
        else:
            res = aws("create-asset", payload={
                "agentSpaceId": AGENT_SPACE_ID, "assetType": asset_type, "metadata": metadata,
                "content": {"file": {"path": path, "body": {"text": text}}}})
            aid = res.get("asset", res).get("assetId")
            self.by_key[key] = {"assetId": aid, "assetType": asset_type, "metadata": metadata}
            print(f"created {asset_type} {metadata['name']} ({aid})")
        return aid


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    sp = Space(a.dry_run)

    skill_ids = {}
    for name, extra in SKILLS.items():
        md = read(f"agent-space/skills/{name}/SKILL.md")
        fm = frontmatter(md)
        meta = {"name": name, "description": fm["description"], "skill_type": "USER",
                "status": "ACTIVE", **extra}
        skill_ids[name] = sp.upsert("skill", meta, "SKILL.md", md)

    store_id = sp.upsert("memory_store", MEMORY_STORE, "README.md",
                         "Standing conventions for checkout-api. Maintained by the platform team.\n")

    for file_stem, meta in MEMORIES.items():
        md = read(f"agent-space/memories/{file_stem}.md")
        sp.upsert("memory", {**meta, "memory_store_id": store_id}, f"{meta['name']}.md", md)

    agent_meta = {
        "name": CUSTOM_AGENT["name"],
        "tools": CUSTOM_AGENT["tools"],
        "skills": [skill_ids[s] for s in CUSTOM_AGENT["skills"]],
        "memory_stores": [store_id],
    }
    agent_id = sp.upsert("custom_agent", agent_meta, "AGENT.md",
                         read(f"agent-space/agents/{CUSTOM_AGENT['name']}/AGENT.md"))

    # Scheduled trigger for the custom agent
    target = f"custom:{agent_id}"
    triggers = aws("list-triggers", "--agent-space-id", AGENT_SPACE_ID).get("items", [])
    existing = next((t for t in triggers if json.dumps(t.get("action", {})).find(target) >= 0), None)
    if a.dry_run:
        print(f"{'keep' if existing else 'create'} trigger {TRIGGER_SCHEDULE} -> {target}")
        return
    if existing:
        print(f"trigger exists ({existing.get('triggerId')}) -> {target}")
    else:
        res = aws("create-trigger", payload={
            "agentSpaceId": AGENT_SPACE_ID, "type": "TIME_BASED",
            "condition": {"schedule": {"expression": TRIGGER_SCHEDULE}},
            "action": {"actionType": "create:task", "task": {"agent": target}},
            "status": "Active"})
        print(f"created trigger {res.get('trigger', res).get('triggerId')} {TRIGGER_SCHEDULE} -> {target}")


if __name__ == "__main__":
    main()
