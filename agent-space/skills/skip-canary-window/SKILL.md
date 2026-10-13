---
name: skip-canary-window
description: Skip incidents from the checkout-api-error-rate alarm that fire during a
  planned canary rollout of the checkout-api application. Use this skill during triage
  to avoid investigating expected error spikes right after a canary sync.
---

# Skip canary window

Skip the incident only if ALL of the following are true:

1. The triggering alarm is `checkout-api-error-rate`.
2. An ArgoCD sync of Application `checkout-api` (namespace `argocd`) happened in the last
   10 minutes and its revision message contains `canary=true`.
3. The error rate reported by the alarm is below 50%.

Do NOT skip if the error rate is above 50% or if the sync message does not mention
`canary` - a real outage must still be investigated. When skipping, state which sync
revision matched.
