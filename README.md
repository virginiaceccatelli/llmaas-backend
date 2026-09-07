# LLMaaS — Gateway / Broker VM

The private backend tier of the LLMaaS platform: API keys, authentication,
rate limiting, model routing and usage metering. See
[architecture.md](architecture.md) for the overall design.

```
       ┌─────────────┐        ┌──────────────────────────┐        ┌──────────────┐
User ──│ Frontend VM │────────│  Gateway / Broker VM     │────────│  GPU VM(s)   │
  TLS  │ React + BFF │private │  broker + Postgres+Redis │private │  vLLM / Qwen │
       └─────────────┘        └──────────────────────────┘        └──────────────┘
         other repo                    THIS REPO                  configured here,
                                                                  deployed there
```

---

## Repository layout: two repos, not three

**Decision: two repositories.** This one (gateway + serving config + infra),
and a separate `llmaas-frontend`.

**Why the GPU tier does *not* get its own repo.** It has no application code —
vLLM is an off-the-shelf image, and the GPU VM's entire contents are a compose
file and some flags. What it *does* have is a tight coupling to the gateway:
adding a model means starting a vLLM process **and** adding a route to the
broker's registry. Those two edits must land together or the platform is
broken between merges. Splitting them across repos turns every model change
into two coordinated PRs with no atomic rollback. So `serving/` lives here,
next to the registry it must stay in sync with.

**Why the frontend *does* get its own repo.** Different toolchain (Node/React
vs Python), different release cadence, different CI. More importantly it is the
only **publicly exposed** tier — keeping the private tier's code, config and
issue tracker out of the public-facing repo is a real boundary, cheaply bought.

**Why not one monorepo?** You could. The frontend/backend split is the useful
seam because it matches the trust boundary; a further split by VM does not
match any boundary that actually exists.

If the GPU tier ever grows real code (custom schedulers, model lifecycle
automation), `serving/` is a clean `git subtree split` away from becoming its
own repo. Nothing here forecloses that.

---

## Layout

```
.
├── broker/                  The FastAPI service. All the code.
│   ├── app/
│   │   ├── main.py            app wiring + lifespan
│   │   ├── config.py          env-driven settings
│   │   ├── db.py / cache.py   Postgres pool, Redis client
│   │   ├── security.py        key hashing, auth  ← THE AUTH GAP IS HERE
│   │   ├── ratelimit.py       per-key request limits (Redis)
│   │   ├── metering.py        usage rows for billing
│   │   ├── registry.py        public model name → upstream
│   │   ├── upstream.py        the vLLM/OpenAI client + body rewriting
│   │   └── routers/
│   │       ├── keys.py        control plane: issue / list / revoke keys
│   │       ├── usage.py       control plane: usage summary
│   │       ├── chat.py        data plane: /v1/* — Envoy replaces THIS file
│   │       └── health.py      /health, /ready
│   ├── Dockerfile
│   └── requirements.txt
├── db/init.sql              users, api_keys, usage
├── serving/                 GPU tier: vLLM compose + the model registry
├── gateway/                 Envoy AI Gateway — empty until you need it
├── docs/AUTH.md             what auth exists, what you must build
├── docs/THIRD_PARTY.md      accounts you need to create
├── scripts/smoke.py         end-to-end test, stdlib only
└── docker-compose.yml       the gateway VM stack
```

The `routers/` split is not decoration: it marks the **control plane**
(`keys`, `usage` — stays forever) apart from the **data plane** (`chat` — gets
deleted when Envoy AI Gateway takes over). See [gateway/README.md](gateway/README.md).

---

## Quick start (no GPU needed)

The dev config routes to **Qwen via Hugging Face's OpenAI-compatible router**,
so the whole auth → rate limit → route → meter path works on a laptop.

```powershell
Copy-Item .env.example .env
# edit .env: set HF_TOKEN (see docs/THIRD_PARTY.md)
docker compose up --build
```

Then:

```powershell
python scripts/smoke.py
```

Or by hand:

```bash
# 1. mint a key (plaintext is shown ONCE)
curl -s -X POST localhost:8080/keys -H 'Content-Type: application/json' -d '{"label":"my-key"}'

# 2. use it
curl -s localhost:8080/v1/chat/completions \
  -H "Authorization: Bearer wiit_..." -H 'Content-Type: application/json' \
  -d '{"model":"qwen-instruct","messages":[{"role":"user","content":"hi"}]}'

# 3. see what it cost
curl -s localhost:8080/usage
```

Interactive API docs: <http://localhost:8080/docs>

---

## Going to the real GPU

1. Bring up vLLM on the GPU VM — see [serving/README.md](serving/README.md).
2. In `docker-compose.yml`, change the broker's volume mount from
   `models.dev.yaml` to `models.prod.yaml`.
3. Set `VLLM_QWEN_HOST` and `VLLM_API_KEY` in `.env`.
4. `docker compose up -d --build broker`

No code changes. That indirection is the whole point of `serving/models.*.yaml`.

---

## What is deliberately missing

Every item below is marked with an `EXTEND:` comment at the exact place in the
code where it belongs.

| Missing | Where to add it | Priority |
|---|---|---|
| **User login on `/keys` and `/usage`** | `broker/app/security.py: require_user` | **blocking** — see [docs/AUTH.md](docs/AUTH.md) |
| TLS between tiers | reverse proxy / vLLM flags | high |
| Vault instead of `.env` | `broker/app/config.py`, commented service in compose | high |
| Token-based rate limits | `broker/app/ratelimit.py` | high |
| Key expiry, per-key quotas | `db/init.sql`, `routers/keys.py` | medium |
| DB migrations (Alembic) | replaces `db/init.sql` | medium |
| Per-model access control | `broker/app/registry.py` | medium |
| Prometheus metrics, tracing | `routers/health.py`, `main.py` | medium |
| `/v1/embeddings`, `/v1/completions` | `broker/app/routers/chat.py` | as needed |

## Security properties already in place

- API keys stored only as SHA-256; plaintext returned once, never persisted.
- Customer keys are never forwarded upstream — the broker presents its own.
- Revocation checked on every request; key deletion cascades to usage rows.
- Per-tenant `cache_salt` sent to vLLM, so the prefix/KV cache cannot be
  shared across tenants (blocks cache-timing leakage between customers).
- vLLM runs with `--disable-log-requests`; no prompt or completion text is
  stored anywhere in this repo's schema.
- Broker has no CORS middleware, by design — it must never be browser-reachable.
- Container runs as a non-root user.
