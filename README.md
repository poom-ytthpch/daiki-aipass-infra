# daiki-ai-passport-infra

Kubernetes/GitOps infrastructure for Daiki AI Passport on `match-infra`.

## Target flow

```text
Vercel Frontend
  -> public HTTPS API / Cloudflare path
  -> ingress-nginx
  -> APISIX
  -> daiki-ai-passport-backend (Go + Chi)
       -> Keycloak
       -> Redis
       -> PostgreSQL
       -> LiteLLM
            -> existing external WireGuard/Tailscale service
            -> Local OpenAI-compatible runtime
            -> Qwen3.8-27B
```

The Local LLM is intentionally optional until the dedicated GPU host is connected. Web/Auth/Admin/Gateway/Data/Observability must remain healthy without it.

## Components
- `backend`: Daiki Go API, 1 replica, 32Mi request / 128Mi limit.
- `APISIX`: standalone YAML-configured gateway, no etcd.
- `Keycloak`: OIDC/RBAC (`ai-user`, `ai-admin`).
- `LiteLLM`: model gateway with alias `qwen-local`.
- Dedicated PostgreSQL databases: `keycloak`, `litellm`, `backend`.
- Dedicated small Redis for session-independent counters/cache.
- cert-manager certificate for `api.ai.infra.local` and `auth.ai.infra.local`.
- PodMonitors, PrometheusRule and Grafana dashboard using the existing match-infra observability stack.

## VPN
WireGuard and Tailscale are **not installed or owned by this repository**. They already run in an external container/service. Daiki consumes only the stable private route configured in `daiki-ai-passport-runtime`.

## Frontend
`daiki-ai-passport-frontend` is deployed to Vercel and is not a Kubernetes workload in this repository.

## Validation

```bash
kubectl kustomize k8s/overlays/match-infra > /tmp/daiki.yaml
kubeconform -strict -summary -ignore-missing-schemas /tmp/daiki.yaml
```

## Bootstrap order
1. Create namespace and generated Kubernetes Secret (never commit real credentials).
2. Start PostgreSQL/Redis.
3. Start Keycloak and run realm bootstrap.
4. Start LiteLLM.
5. Deploy backend image from GHCR.
6. Start APISIX and ingress.
7. Verify monitoring/TLS.
8. Later, connect `local-llm-base-url` to the private Local LLM endpoint.
