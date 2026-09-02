# Existing VPN Integration

Daiki AI Passport does **not** install WireGuard or Tailscale on `match-infra`.
Both VPN technologies are already provided by an existing external container/service.
This project only consumes the private connectivity exposed by that service.

## WireGuard — primary inference path

Current intended Local LLM endpoint:

- `daiki-llm-01`: `10.90.0.11:8000`
- OpenAI-compatible API base: `http://10.90.0.11:8000/v1`

The existing VPN container/service owns WireGuard peer keys, interfaces, UDP listeners, NAT/forwarding, keepalive, route persistence and firewall policy. None of those are created or managed by this repository.

Required network contract:

```text
daiki-ai-passport namespace / LiteLLM
        |
        | private route provided by existing VPN container/service
        v
10.90.0.11:8000 (or configured stable VPN address/DNS)
        |
        v
Local LLM OpenAI-compatible API
```

Only approved private server networks should reach the inference port. Port `8000` must never be exposed directly to the public Internet.

## Tailscale — management and fallback

The existing VPN container/service also owns Tailscale enrollment, MagicDNS and ACLs. Daiki AI Passport may use a stable Tailscale IP or MagicDNS name as a fallback upstream if required.

Recommended policy:

- Server network -> inference API: allowed.
- Administrator devices -> SSH/management: allowed by ACL.
- Public Internet -> inference API: denied.
- Do not advertise conflicting routes through both WireGuard and Tailscale.

WireGuard remains the preferred inference data plane; Tailscale is management/fallback.

## Daiki configuration contract

Do not encode VPN implementation details in the frontend. Only provide a stable upstream URL to LiteLLM through Kubernetes configuration:

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: daiki-ai-passport-runtime
  namespace: daiki-ai-passport
data:
  local-llm-base-url: http://10.90.0.11:8000/v1
```

If the existing VPN container exposes another IP or DNS name, patch `local-llm-base-url` in the `match-infra` overlay. LiteLLM and the frontend do not need to be rebuilt.

## Verification gate

Before deploying LiteLLM:

1. Verify `match-infra` network can route through the existing VPN service to the Local LLM endpoint.
2. Verify `GET /v1/models` or the runtime health endpoint returns successfully over that private path.
3. Verify the inference port is unreachable from public interfaces.
4. Verify the VPN container survives restart/reboot and preserves its route.
5. Record the final stable WireGuard endpoint and optional Tailscale fallback endpoint in the environment overlay.
