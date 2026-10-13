# checkout-api deployment conventions

- Deployments of `checkout-api` (namespace `checkout`) happen exclusively through ArgoCD
  Application `checkout-api` (namespace `argocd`), which has auto-sync, self-heal and prune
  enabled. Source: https://github.com/hellenavilarosa/checkout-devops-demo, path `overlays/prod`.
- Manual changes in the cluster (`kubectl edit/apply/rollout undo`) are reverted automatically
  by self-heal within seconds. The correct mitigation is always a commit on that repository.
- `checkout-api` depends on Service `redis` in namespace `checkout`; the hostname is set by the
  `REDIS_HOST` key of the `checkout-api-config` ConfigMap in `overlays/prod/kustomization.yaml`.
- Readiness and liveness probes use `/ready` (process-level). `/health` additionally checks Redis.
