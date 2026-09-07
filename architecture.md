# LLMaaS Proof of Concept Architecture Plan

## Architecture Overview

```
    User ─TLS─► 
    Frontend VM: WebUI + Key-Auth UI ─private network─► 
    Gateway/ Broker VM: Envoy AI + Redis + PostgresSQL + Vault (auth & rate limits) ─private network─► 
    GPU serving VM(s): vLLM (one instance per model)
```

---

## Components & Decisions

### Serving engine: **vLLM** 
- OpenAI-compatible API, PagedAttention + continuous batching for high throughput and low latency, and mature multi-GPU (tensor-parallelism) support.
- vLLM runs one model per process/port, giving a hard boundary between models, the opposite of Ollama, which shares state and can leak context between models/sessions. 
- Multiple models = multiple vLLM instances, each on its own port/GPU, routed by the gateway.

### Infrastructure: **OpenStack VMs → Kubernetes later**
- Launch VMs on OpenStack, with GPUs attached via PCI passthrough (isolation boundary)
- Separate VMs for frontend, gateway, and GPU serving, as required.
- Everything runs in Docker containers so the same images later drop into a Kubernetes cluster.

### Gateway / Broker: **Envoy AI Gateway**
Envoy AI Gateway:
- It's built on Envoy and is CNCF/ Kubernetes-native 
- It handles API-key auth, per-model routing, and token-based rate limiting.
- Unlike LiteLLM, which is more feature-rich but has had serious CVEs, Enovy AI has no CVEs and seems more secure. 
- Very low latency overhead (~2 ms).

### Usage & Auth: **API keys + Redis + PostgreSQL + Vault**
- Generate API keys per user; store only a hash (never plaintext), show the key once. Use a standard prefix (e.g. `wiit_…`) for identification/rotation.
- Request flow: key → gateway validates → checks limits → routes to the right vLLM model → logs usage.
- Redis holds fast per-key rate/token counters; PostgreSQL stores keys and usage records (for future billing); HashiCorp Vault holds secrets.

### Frontend: **React + Fast API**
- UI with React (or HTML/ JS): chat view plus a small account/ API kez management page
- Backend-for-frontend with FastAPI: login/ sessions and key management endpoints, communicates with PostgresSQL and proxies chat traffic to gateway
- Auth with Auth.js or Authentik (optional)

---

## Security & Isolation (the special focus)
- **Network segmentation**: only the frontend tier is public; gateway and GPU nodes are private.
- **Model isolation**: one vLLM process per model; no shared state between models.
- **Tenant / cache isolation**: run vLLM with `--disable-log-requests` (no prompt logging) and a **per-tenant `cache_salt`** so the KV / prefix cache is never shared across customers (prevents the known cache-timing leakage between tenants). 
- **Secrets & transport**: TLS everywhere; API keys hashed at rest; all secrets in Vault.

## Summary Table

| Layer | Tool | Why |
|---|---|---|
| Serving | **vLLM** | Standard, fast, OpenAI-compatible; one model/process = no leakage |
| Infra | **OpenStack VMs + Docker → Kubernetes** | GPU passthrough, tier separation, portable to Kubernetes in future |
| Gateway | **Envoy AI Gateway** | Secure, auth + routing + limits; low overhead |
| Auth / Usage | **API keys + Redis + PostgreSQL + Vault** | Hashed keys, fast limits, usage tracking, secret management |
| Frontend | **React + Fast API** | Simple, customizable |

