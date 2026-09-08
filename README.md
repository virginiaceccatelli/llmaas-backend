# LLMaaS — Gateway / Broker VM

The private backend tier of the LLMaaS platform: API keys, authentication,
rate limiting, model routing and usage metering. See
[architecture.md](architecture.md) for the overall design.

## Repository layout: two repos, not three

**Two repositories.** This one (gateway + serving config + infra),
and a separate `llmaas-frontend`.

---

## Layout

```
.
├── broker/                  The FastAPI service. All the code.
│   ├── app/
│   │   ├── main.py            app wiring + lifespan
│   │   ├── config.py          env-driven settings
│   │   ├── db.py / cache.py   Postgres pool, Redis client
│   │   ├── security.py        key hashing + API-key auth + user auth (JWT)
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
├── serving/                 GPU tier: vLLM compose + the model registries
│   ├── models.mock.yaml       offline mock   (free, no network)
│   ├── models.dev.yaml        Qwen via Hugging Face  (needs HF credits)
│   ├── models.prod.yaml       your own vLLM on the GPU VM
│   └── mock/server.py         the offline stand-in upstream
├── tests/                   unit tests (30, all passing)
├── gateway/                 Envoy AI Gateway — empty until you need it
├── docs/WORKFLOW.md         daily workflow, local setup, what NOT to install
├── docs/AUTH.md             what auth exists, what you must build
├── docs/THIRD_PARTY.md      accounts you need to create
├── scripts/smoke.py         quick end-to-end test, stdlib only
├── scripts/verify.py        deep verification: streaming, metering, isolation
├── scripts/local_postgres.ps1   portable Postgres for Windows, no admin needed
└── docker-compose.yml       the gateway VM stack
```

The `routers/` split is not decoration: it marks the **control plane**
(`keys`, `usage` — stays forever) apart from the **data plane** (`chat` — gets
deleted when Envoy AI Gateway takes over). See [gateway/README.md](gateway/README.md).

---

## Day-to-day

See **[docs/WORKFLOW.md](docs/WORKFLOW.md)** for the full workflow: venv, local
Postgres without admin rights, troubleshooting, and why you should *not*
install Envoy or Vault yet.

## Quick start A — no Docker, no GPU, no network (works today)

Runs the broker directly against portable Postgres and the offline mock
upstream. The whole auth -> rate limit -> route -> stream -> meter path is
real; only the model output is canned.

```powershell
py -3.12 -m venv .venv                       # once
.\.venv\Scripts\Activate.ps1
python -m pip install -r broker\requirements-dev.txt
Copy-Item .env.example .env                  # then set MODELS_FILE + AUTH_MODE
.\scripts\local_postgres.ps1 setup           # once; no admin needed

# terminal 1
uvicorn server:app --app-dir serving\mock --port 8000
# terminal 2
uvicorn app.main:app --reload --app-dir broker --port 8080
# terminal 3
python scripts\smoke.py     # quick end-to-end
python scripts\verify.py    # 29 deep checks
pytest -q                   # 30 unit tests
```

## Quick start B — Docker (needs a working engine)

The dev registry routes to **Qwen via Hugging Face's OpenAI-compatible
router**, so you get real model output with no GPU.

```powershell
Copy-Item .env.example .env
# edit .env: set HF_TOKEN (see docs/THIRD_PARTY.md)
docker compose up --build
python scripts\smoke.py
```

> Docker does not currently work on the dev laptop (WSL2 backend missing, needs
> admin). See [docs/WORKFLOW.md](docs/WORKFLOW.md#docker-on-this-laptop).

## By hand

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
| **A real `AUTH_MODE`** (login is implemented; the default is still `dev`) | `.env` — see [docs/AUTH.md](docs/AUTH.md) | **blocking before exposure** |
| TLS between tiers | reverse proxy / vLLM flags | high |
| Vault instead of `.env` | `broker/app/config.py`, commented service in compose | high |
| Token-based rate limits | `broker/app/ratelimit.py` | high |
| Key expiry, per-key quotas | `db/init.sql`, `routers/keys.py` | medium |
| DB migrations (Alembic) | replaces `db/init.sql` | medium |
| Per-model access control | `broker/app/registry.py` | medium |
| Prometheus metrics, tracing | `routers/health.py`, `main.py` | medium |
| `/v1/embeddings`, `/v1/completions` | `broker/app/routers/chat.py` | as needed |

## Security properties already in place

- Control-plane login verifies a signed JWT (`hs256` or `oidc`), with the
  algorithm pinned, `exp` required, and `iss`/`aud` checked when configured.
  Covered by [tests/test_auth.py](tests/test_auth.py).
- API keys stored only as SHA-256; plaintext returned once, never persisted.
- Customer keys are never forwarded upstream — the broker presents its own.
- Revocation checked on every request; key deletion cascades to usage rows.
- Per-tenant `cache_salt` sent to vLLM, so the prefix/KV cache cannot be
  shared across tenants (blocks cache-timing leakage between customers).
- vLLM runs with `--disable-log-requests`; no prompt or completion text is
  stored anywhere in this repo's schema.
- Broker has no CORS middleware, by design — it must never be browser-reachable.
- Container runs as a non-root user.
