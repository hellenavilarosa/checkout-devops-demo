#!/usr/bin/env python3
"""Commit local files to a GitHub repo through the REST API (no `git push`).

Uses the authenticated `gh` CLI for every call, so it works on machines where
the git protocol is blocked but HTTPS to api.github.com is allowed.

Usage:
  gh-commit.py OWNER/REPO BRANCH "message" [--base BASE] [--root DIR] PATH...

  PATH entries are files or directories, relative to --root (default: cwd).
  If BRANCH does not exist it is created from --base (default: repo default
  branch). The commit is built as a single tree on top of the branch head.

Examples:
  # initial import of the whole repo
  gh-commit.py hellenavilarosa/checkout-devops-demo main "Initial import" .
  # feature branch with one changed file, then open a PR with `gh pr create`
  gh-commit.py hellenavilarosa/checkout-devops-demo fix/redis-host \
      "fix: restore REDIS_HOST" overlays/prod/kustomization.yaml
"""

import argparse
import base64
import json
import os
import subprocess
import sys

IGNORE_DIRS = {".git", "__pycache__", ".kiro", "node_modules"}


def gh(method, path, **fields):
    cmd = ["gh", "api", "-X", method, path]
    for k, v in fields.items():
        cmd += ["-f" if isinstance(v, str) else "-F", f"{k}={v if isinstance(v, str) else json.dumps(v)}"]
    out = subprocess.run(cmd, capture_output=True, text=True)
    if out.returncode != 0:
        sys.exit(f"gh api {method} {path} failed:\n{out.stderr.strip()}")
    return json.loads(out.stdout) if out.stdout.strip() else {}


def gh_json(method, path, payload):
    """POST/PATCH with a raw JSON body (for nested structures like tree entries)."""
    out = subprocess.run(["gh", "api", "-X", method, path, "--input", "-"],
                         input=json.dumps(payload), capture_output=True, text=True)
    if out.returncode != 0:
        sys.exit(f"gh api {method} {path} failed:\n{out.stderr.strip()}")
    return json.loads(out.stdout)


def collect(root, paths):
    files = []
    for p in paths:
        full = os.path.join(root, p)
        if os.path.isdir(full):
            for dirpath, dirnames, filenames in os.walk(full):
                dirnames[:] = [d for d in dirnames if d not in IGNORE_DIRS]
                for fn in filenames:
                    files.append(os.path.relpath(os.path.join(dirpath, fn), root))
        elif os.path.isfile(full):
            files.append(os.path.relpath(full, root))
        else:
            sys.exit(f"not found: {full}")
    return sorted(set(f for f in files if not f.startswith(".git/")))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("repo")
    ap.add_argument("branch")
    ap.add_argument("message")
    ap.add_argument("paths", nargs="+")
    ap.add_argument("--base", default=None, help="branch to fork from if BRANCH is new")
    ap.add_argument("--root", default=os.getcwd())
    a = ap.parse_args()
    repo = a.repo
    root = os.path.abspath(a.root)

    # Resolve parent commit (branch head, or base head if branch is new)
    base = a.base or gh("GET", f"/repos/{repo}")["default_branch"]
    ref = subprocess.run(["gh", "api", f"/repos/{repo}/git/ref/heads/{a.branch}"],
                         capture_output=True, text=True)
    branch_exists = ref.returncode == 0
    if branch_exists:
        parent_sha = json.loads(ref.stdout)["object"]["sha"]
    else:
        base_ref = subprocess.run(["gh", "api", f"/repos/{repo}/git/ref/heads/{base}"],
                                  capture_output=True, text=True)
        parent_sha = json.loads(base_ref.stdout)["object"]["sha"] if base_ref.returncode == 0 else None

    # Upload blobs
    files = collect(root, a.paths)
    entries = []
    for rel in files:
        with open(os.path.join(root, rel), "rb") as fh:
            content = fh.read()
        blob = gh_json("POST", f"/repos/{repo}/git/blobs",
                       {"content": base64.b64encode(content).decode(), "encoding": "base64"})
        mode = "100755" if os.access(os.path.join(root, rel), os.X_OK) else "100644"
        entries.append({"path": rel.replace(os.sep, "/"), "mode": mode, "type": "blob", "sha": blob["sha"]})
        print(f"  blob {blob['sha'][:7]} {rel}")

    tree_payload = {"tree": entries}
    if parent_sha:
        tree_payload["base_tree"] = gh("GET", f"/repos/{repo}/git/commits/{parent_sha}")["tree"]["sha"]
    tree = gh_json("POST", f"/repos/{repo}/git/trees", tree_payload)

    commit_payload = {"message": a.message, "tree": tree["sha"]}
    if parent_sha:
        commit_payload["parents"] = [parent_sha]
    commit = gh_json("POST", f"/repos/{repo}/git/commits", commit_payload)

    if branch_exists:
        gh_json("PATCH", f"/repos/{repo}/git/refs/heads/{a.branch}", {"sha": commit["sha"], "force": False})
    else:
        gh_json("POST", f"/repos/{repo}/git/refs", {"ref": f"refs/heads/{a.branch}", "sha": commit["sha"]})

    print(f"committed {commit['sha'][:7]} to {repo}@{a.branch} ({len(files)} files)")
    print(f"https://github.com/{repo}/commit/{commit['sha']}")


if __name__ == "__main__":
    main()
