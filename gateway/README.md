# gateway/ — Envoy AI Gateway (not yet wired up)

Intentionally empty for the PoC.

Right now the **broker** (`broker/`) does auth, routing and rate limiting in
Python. That is fine for a proof of concept and it keeps the number of moving
parts low while you are still proving the end-to-end flow.

## When to introduce Envoy AI Gateway

Adopt it when you hit any of these:

- Python request-proxying becomes a latency or throughput bottleneck.
- You need **token-based** rate limiting (tokens/min, not requests/min) —
  Envoy AI Gateway does this natively; the broker would need real work.
- You move to Kubernetes, where Envoy AI Gateway is the native fit
  (it is a Gateway API implementation).

## What the migration looks like

The router split in `broker/app/routers/` was designed for this:

| Today | After Envoy |
|---|---|
| `routers/chat.py` (data plane) | **deleted** — Envoy proxies `/v1/*` directly to vLLM |
| `routers/keys.py`, `routers/usage.py` (control plane) | **stays** — the broker becomes a pure control plane |
| `serving/models.prod.yaml` | becomes `AIServiceBackend` + `AIGatewayRoute` resources |
| `app/security.py: require_key` | becomes an Envoy `BackendSecurityPolicy` / ext_authz call into the broker |
| `app/ratelimit.py` | becomes Envoy's token rate limit, still backed by Redis |

Drop the generated Envoy config (`AIGatewayRoute`, `AIServiceBackend`,
`BackendTrafficPolicy`) into this directory when you get there.

Reference: https://aigateway.envoyproxy.io/
