---
name: runbook-argocd-rollback
description: Runbook for mitigating incidents in applications managed by ArgoCD with
  auto-sync and self-heal enabled. Use this skill when an investigation on an EKS
  workload points to a recent configuration, image or probe change and the workload
  is an ArgoCD Application (for example checkout-api in namespace checkout). It explains
  why kubectl-based rollbacks do not work with self-heal and how to propose a GitOps revert.
---

# Runbook: rollback of an ArgoCD-managed application

## When to use
An incident in an ArgoCD Application that has `syncPolicy.automated` with `selfHeal: true`,
where the probable cause is a recent change to a ConfigMap, image tag, environment
variable or probe.

## Rules
1. NEVER propose `kubectl rollout undo`, `kubectl edit`, `kubectl set image` or a manual
   `kubectl apply`: self-heal reverts the change within seconds and hides the mitigation.
2. The mitigation is ALWAYS a commit in the GitOps repository referenced by
   `spec.source.repoURL`, under the path `spec.source.path` of the Application.
3. Prefer reverting the culprit commit over a hand-written fix, unless the revert would
   reintroduce another known problem.
4. If the Application has `prune: true`, warn that removing resources from Git deletes
   them from the cluster.

## Steps
1. Identify the Application: `kubectl get application -n argocd` and read
   `status.sync.revision` (current) and `status.history` (previous revisions with timestamps).
2. Correlate the start of the error with the sync timestamp in `status.history`.
3. Compare the current and previous revisions: ConfigMap values, image tags, env vars,
   readiness/liveness probes.
4. Check whether a second, adjacent change (for example a probe switched from a dependency
   check to a process-level check) allowed a bad rollout to receive traffic.
5. Propose: revert commit (or minimal patch) on the GitOps path, then a hard refresh
   (`argocd.argoproj.io/refresh=hard` annotation) so ArgoCD syncs immediately.
6. Record follow-ups: adjacent changes that masked the problem, and any RBAC gap that
   prevented reading Application fields.

## Verification
- `kubectl get application <name> -n argocd` shows `Synced` and `Healthy`.
- Pods of the new ReplicaSet are `Ready` and the 5xx rate returns to baseline for 5 minutes.
