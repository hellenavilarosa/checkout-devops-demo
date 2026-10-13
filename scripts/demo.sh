#!/usr/bin/env bash
# demo.sh — one command per live moment of DEMO-ROTEIRO.md. Run from the repo root.
#
#   scripts/demo.sh status          # alarms, pods, loadgen, latest investigations (one screen)
#   scripts/demo.sh break-memory    # scenario A: commit right-sizing 48Mi/56Mi -> OOMKilled in ~3 min
#   scripts/demo.sh break-latency   # scenario B: commit fraud scoring -> p99 3s, errors flat
#   scripts/demo.sh force-alarm [restarts|latency|errors]   # plan B: set alarm to ALARM now (fires EventBridge -> Lambda)
#   scripts/demo.sh watch           # live loop: alarms + backlog + pods every 10s (Ctrl-C to stop)
#   scripts/demo.sh tail            # tail the most recent investigation
#   scripts/demo.sh fix-pr          # scenario A manual fallback for Kiro CLI: branch + commit + PR
#   scripts/demo.sh merge           # merge the open fix PR, hard-refresh ArgoCD
#   scripts/demo.sh reset           # back to healthy baseline (one commit)
#
# Every change goes through Git (gh-commit.py) and ArgoCD — never kubectl apply.
set -euo pipefail
cd "$(dirname "$0")/.."

REPO=hellenavilarosa/checkout-devops-demo
REGION=us-east-1
CLUSTER=eks-agentic-troubleshooting
OVERLAY=overlays/prod/kustomization.yaml
AS_ID=$(cat .agent-space-id)
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
export CTX="arn:aws:eks:${REGION}:${ACCOUNT}:cluster/${CLUSTER}"
FRAUD_URL='http://10.255.255.1:8443/score'

commit() {  # commit <message> — commits the overlay and nudges ArgoCD
  python3 scripts/gh-commit.py "$REPO" main "$1" "$OVERLAY" | tail -1
  kubectl --context "$CTX" annotate application checkout-api -n argocd argocd.argoproj.io/refresh=hard --overwrite >/dev/null
  echo "ArgoCD refresh requested ($(date -u +%H:%M:%S) UTC)"
}

alarms() {
  aws cloudwatch describe-alarms --region "$REGION" --alarm-name-prefix checkout-api \
    --query 'MetricAlarms[].[AlarmName,StateValue]' --output text | column -t
}

backlog() {
  aws devops-agent list-backlog-tasks --agent-space-id "$AS_ID" --region "$REGION" --output json \
    | python3 -c '
import json,sys
ts=[t for t in json.load(sys.stdin)["tasks"] if t.get("taskType")=="INVESTIGATION"]
for t in sorted(ts,key=lambda t:t.get("createdAt",""),reverse=True)[:4]:
    print(t.get("createdAt","")[11:19], t.get("status","").ljust(13), t.get("title",""))'
}

pods() {
  kubectl --context "$CTX" get pods -n checkout -l app=checkout-api --no-headers \
    | awk '{printf "%-34s %-5s %-18s restarts=%s\n",$1,$2,$3,$4}'
}

loadgen() {
  kubectl --context "$CTX" logs -n checkout deploy/loadgen --since=30s 2>/dev/null \
    | awk '{s[$3]++; t=$4; sub("time=","",t); sub("s","",t); sum+=t; n++}
           END {for (k in s) printf "%d %s  ", s[k], k; if (n) printf "| avg=%.2fs over %ds\n", sum/n, 30}'
}

case "${1:-}" in
  status)
    echo "== alarms";   alarms
    echo "== pods";     pods
    echo "== loadgen (last 30s)"; loadgen
    echo "== investigations"; backlog
    echo "== overlay";  grep -nE 'memory:|FRAUD_CHECK_URL=|APP_VERSION=' "$OVERLAY" | sed 's/^/   /'
    ;;

  break-memory)
    sed -i '' 's/            memory: 64Mi/            memory: 48Mi/; s/            memory: 128Mi/            memory: 56Mi/' "$OVERLAY"
    grep -n 'memory:' "$OVERLAY"
    commit "perf(prod): right-size checkout-api after load test (64Mi/128Mi -> 48Mi/56Mi)"
    echo "expect: pods Ready in ~30s, first OOMKilled ~3 min later, alarm 'checkout-api-container-restarts' within 5 min of that."
    echo "next:   scripts/demo.sh watch"
    ;;

  break-latency)
    sed -i '' "s#      - FRAUD_CHECK_URL=\$#      - FRAUD_CHECK_URL=${FRAUD_URL}#; s/APP_VERSION=1.0.0/APP_VERSION=1.1.0/" "$OVERLAY"
    grep -nE 'FRAUD_CHECK_URL=|APP_VERSION=' "$OVERLAY"
    commit "feat(prod): enable fraud scoring before checkout (v1.1.0)"
    echo "expect: latency 3s within 1 min, errors stay 0%, alarm 'checkout-api-latency-p99' in ~3 min."
    echo "next:   scripts/demo.sh watch"
    ;;

  force-alarm)
    case "${2:-restarts}" in
      restarts) A=checkout-api-container-restarts ;;
      latency)  A=checkout-api-latency-p99 ;;
      errors)   A=checkout-api-error-rate ;;
      *) echo "unknown alarm: $2"; exit 1 ;;
    esac
    aws cloudwatch set-alarm-state --region "$REGION" --alarm-name "$A" --state-value ALARM --state-reason "demo: forced"
    echo "forced $A -> ALARM. EventBridge -> Lambda -> investigation should appear in ~10s:"
    sleep 12; backlog
    ;;

  watch)
    while true; do
      clear; date -u +"%H:%M:%S UTC"
      echo "== alarms"; alarms
      echo "== pods"; pods
      echo "== loadgen (last 30s)"; loadgen
      echo "== investigations"; backlog
      sleep 10
    done
    ;;

  tail)
    TASK=$(aws devops-agent list-backlog-tasks --agent-space-id "$AS_ID" --region "$REGION" --output json \
      | python3 -c 'import json,sys; ts=[t for t in json.load(sys.stdin)["tasks"] if t.get("taskType")=="INVESTIGATION"]; ts.sort(key=lambda t:t.get("createdAt",""),reverse=True); print(ts[0]["taskId"])')
    EXE=$(aws devops-agent get-backlog-task --agent-space-id "$AS_ID" --task-id "$TASK" --region "$REGION" --output json \
      | python3 -c 'import json,sys; t=json.load(sys.stdin); t=t.get("task",t); print(t.get("executionId") or (t.get("executions") or [{}])[-1].get("executionId",""))')
    [ -n "$EXE" ] || { echo "investigation $TASK has no execution yet (PENDING_START)"; exit 0; }
    python3 scripts/investigation-tail.py "$EXE" --n "${2:-8}"
    ;;

  fix-pr)
    sed -i '' 's/            memory: 48Mi/            memory: 64Mi/; s/            memory: 56Mi/            memory: 128Mi/' "$OVERLAY"
    python3 scripts/gh-commit.py "$REPO" fix/memory-limit \
      "fix(prod): revert checkout-api right-sizing (OOMKilled under load)" "$OVERLAY" | tail -1
    gh pr create --repo "$REPO" --head fix/memory-limit --base main \
      --title "fix(prod): revert checkout-api right-sizing" \
      --body "Mitigação da investigação [ALARM] checkout-api-container-restarts: limite de 56Mi abaixo do uso real (~48 MB anon + cache de idempotência). Restaura 64Mi/128Mi."
    ;;

  merge)
    PR=$(gh pr list --repo "$REPO" --head fix/memory-limit --state open --json number --jq '.[0].number')
    [ -n "$PR" ] || { echo "no open PR from fix/memory-limit"; exit 1; }
    gh pr merge "$PR" --repo "$REPO" --squash --delete-branch
    kubectl --context "$CTX" annotate application checkout-api -n argocd argocd.argoproj.io/refresh=hard --overwrite >/dev/null
    echo "merged PR #$PR; ArgoCD refresh requested. Restarts alarm returns to OK within 5 min."
    ;;

  reset)
    sed -i '' "s/            memory: 48Mi/            memory: 64Mi/; s/            memory: 56Mi/            memory: 128Mi/; s#FRAUD_CHECK_URL=${FRAUD_URL}#FRAUD_CHECK_URL=#; s/APP_VERSION=1.1.0/APP_VERSION=1.0.0/" "$OVERLAY"
    # keep the scenario comment intact (the FRAUD_URL substitution above also hits it)
    sed -i '' "s|^#   B) FRAUD_CHECK_URL=  slow upstream|#   B) FRAUD_CHECK_URL=${FRAUD_URL}  slow upstream|" "$OVERLAY"
    grep -nE 'memory:|FRAUD_CHECK_URL=|APP_VERSION=' "$OVERLAY"
    commit "chore(prod): restore baseline after demo"
    # re-enable alarm actions in case 5.0 (Agent Actions) left them disabled
    aws cloudwatch enable-alarm-actions --region "$REGION" --alarm-names checkout-api-container-restarts checkout-api-latency-p99 checkout-api-error-rate
    echo "baseline restored; alarms actions enabled."
    ;;

  *)
    sed -n '2,13p' "$0"; exit 1 ;;
esac
