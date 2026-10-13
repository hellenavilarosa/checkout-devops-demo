# checkout-devops-demo

GitOps demo repository for **AWS DevOps Agent + ArgoCD on EKS**.

A small `checkout-api` (Python, stdlib only) with a Redis dependency and a
load generator, deployed with Kustomize and managed by ArgoCD. Faults are
injected **through Git**, never with `kubectl`: change a value in
`overlays/prod/kustomization.yaml`, commit, ArgoCD syncs, the alarm fires,
AWS DevOps Agent investigates, and the fix comes back as a pull request.

```
base/                 app + redis + loadgen, plus the ConfigMap with runtime knobs
  app/server.py       the service (/health, /checkout, /metrics)
overlays/prod/        namespace `checkout`; the ConfigMap values ArgoCD applies
argocd/application.yaml   ArgoCD Application (auto-sync, self-heal, prune)
```

## Runtime knobs (ConfigMap `checkout-api-config`)

| Key | Healthy | Fault example | Symptom |
|---|---|---|---|
| `REDIS_HOST` | `redis` | `redis-cache` | `/checkout` and `/health` return 503, pods go NotReady |
| `ERROR_RATE` | `0` | `0.6` | 60% of `/checkout` return 500 |
| `LATENCY_MS` | `20` | `1500` | p99 latency regression |

## Bootstrap (one time)

```bash
kubectl apply -f argocd/application.yaml
```

Everything else is driven by commits to `main`.
