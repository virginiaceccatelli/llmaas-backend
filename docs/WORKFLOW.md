# Daily workflow & local setup

## Where things stand

| | Status |
|---|---|
| `.venv` (Python 3.12) | created, dependencies installed |
| Portable PostgreSQL | installed at `%USERPROFILE%\pgsql-llmaas`, schema loaded |
| Broker end-to-end | verified against real Postgres, in `dev` and `hs256` auth modes |
| Tests | 30 unit + 9 smoke + 29 deep checks, all passing |
| Hugging Face upstream | working (credits reset); offline mock also available |
| Docker | engine broken, needs admin — see [below](#docker-on-this-laptop) |

The one-time setup below is **already done on this machine**. It is written
down for a fresh machine, and for whoever joins the project next.

---

## TL;DR — what you actually need to install

| Component | Install it? | Why |
|---|---|---|
| **Python venv** | once | Editor autocomplete, linting, running the broker directly |
| **PostgreSQL** | yes | The one hard dependency. `scripts\local_postgres.ps1` needs no admin |
| **Redis** | not on Windows | Set `RATE_LIMIT_BACKEND=memory`. Real Redis comes with Docker/the VM |
| **Envoy AI Gateway** | **not yet** | Phase 2, and Kubernetes-native. See below |
| **Vault** | **not yet** | Phase 2. `.env` is the PoC secret store |
| **Docker** | blocked here | See [Docker on this laptop](#docker-on-this-laptop) |

**You do not install Postgres/Redis/Vault/Envoy one by one.** That is the whole
point of `docker-compose.yml` — it pulls those as images. The list above is only
about the *fallback* path for a laptop where Docker won't run.

> **A Python venv holds Python packages only.** PostgreSQL, Redis, Vault and
> Envoy are native servers written in C/C++/Go — they cannot go in a venv, and
> `pip install psycopg2` gives you a *client driver*, not a database. That is
> why Postgres gets its own portable install below.

---

## One-time setup

```powershell
cd C:\Users\virginia.ceccatelli\Documents\LLMaaS

# 1. venv — Python 3.12, NOT your default 3.14 (asyncpg has no 3.14 wheels yet)
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r broker\requirements-dev.txt

# 2. secrets
Copy-Item .env.example .env
#    then edit .env: set HF_TOKEN (see docs/THIRD_PARTY.md)

# 3. PostgreSQL — portable, no admin, no installer, no service
.\scripts\local_postgres.ps1 setup
```

If `Activate.ps1` is blocked by execution policy (this does **not** need admin):

```powershell
Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned
```

### `.env` settings for running outside Docker

The defaults in `config.py` point at Docker service hostnames (`postgres`,
`redis`) that only resolve inside the compose network, so running the broker
directly needs these four:

```ini
DATABASE_URL=postgresql://llmaas:llmaas@127.0.0.1:5432/llmaas
MODELS_FILE=./serving/models.mock.yaml
RATE_LIMIT_BACKEND=memory
AUTH_MODE=dev
```

`docker-compose.yml` sets its own values for all of these, so leaving them in
`.env` does not affect `docker compose up`.

> `DEV_AUTH_ENABLED` was replaced by `AUTH_MODE`. If your `.env` still has that
> line it is silently ignored — delete it and set `AUTH_MODE` instead.

---

## Every day

```powershell
cd C:\Users\virginia.ceccatelli\Documents\LLMaaS
.\.venv\Scripts\Activate.ps1          # prompt becomes (.venv) PS>
.\scripts\local_postgres.ps1 start    # no-op if already running
```

Then one terminal each:

```powershell
# terminal 1 — the upstream (only for MODELS_FILE=models.mock.yaml)
uvicorn server:app --app-dir serving\mock --port 8000

# terminal 2 — the broker, reloads on every save
uvicorn app.main:app --reload --app-dir broker --port 8080

# terminal 3 — exercise it
python scripts\smoke.py     # 9 quick end-to-end checks
python scripts\verify.py    # 29 deep checks (see below)
pytest -q                   # 30 unit tests
```

### The three test layers

| Command | What it proves | Needs a running broker? |
|---|---|---|
| `pytest -q` | Auth logic, registry validation, upstream error handling | no |
| `scripts\smoke.py` | The happy path works end to end | yes |
| `scripts\verify.py` | Streaming, metering accuracy, multi-model routing, rate limiting, tenant isolation, error paths | yes |

`verify.py` prints PASS / FAIL / SKIP per check and exits non-zero on failure.
Run it against whichever broker you want to test:

```powershell
python scripts\verify.py http://127.0.0.1:8080
```

Some checks self-skip when they cannot apply — multi-tenant checks need
`AUTH_MODE=dev`, and the `cache_salt` check needs the broker routed to the mock
upstream (it verifies the mock actually received the request, so a stale mock
on port 8000 cannot fake a pass).

Interactive API docs: <http://localhost:8080/docs>

At the end of the day, `deactivate`, and optionally
`.\scripts\local_postgres.ps1 stop`. Leaving Postgres running is harmless.

### Which upstream to point at

`MODELS_FILE` in `.env` selects one of three registries. Nothing else changes —
that indirection is the whole point of the model registry.

| File | Upstream | Cost | Use when |
|---|---|---|---|
| `serving/models.mock.yaml` | local mock | free, offline | Offline, or to save HF credits |
| `serving/models.dev.yaml` | real Qwen via HF | HF credits | You want real model output |
| `serving/models.prod.yaml` | your own vLLM | your GPU | The GPU VM exists |

**The HF free tier is a small monthly credit.** When it runs out, every model
returns `HTTP 402 "You have depleted your monthly included credits"`. That is
billing state, not a bug — switch to `models.mock.yaml`, wait for the monthly
reset, or buy a couple of dollars of credit.

Model ids verified working on the HF router are listed in
[`serving/models.dev.yaml`](../serving/models.dev.yaml). Availability changes
over time, so re-check before assuming an id still works.

### Testing real authentication

`AUTH_MODE=dev` is the default and trusts an unverified `X-Dev-User` header.
To exercise real JWT auth locally, put this in `.env`:

```ini
AUTH_MODE=hs256
AUTH_JWT_SECRET=paste-a-random-32-byte-string-here
AUTH_JWT_ISSUER=llmaas-frontend
AUTH_JWT_AUDIENCE=llmaas-broker
```

Generate the secret with:
`python -c "import secrets; print(secrets.token_urlsafe(32))"`

Restart the broker, then:

```powershell
$env:LLMAAS_CONTROL_TOKEN = (python scripts\make_token.py --user alice)
python scripts\smoke.py
```

`scripts\make_token.py` stands in for the frontend BFF, which will mint these
tokens once it exists. Details in [AUTH.md](AUTH.md).

### Handy commands

```powershell
.\scripts\local_postgres.ps1 psql      # SQL shell:  \dt   select * from api_keys;
.\scripts\local_postgres.ps1 status
.\scripts\local_postgres.ps1 reset     # wipe data, reload db\init.sql
pytest -q                              # 30 unit tests
ruff check . ; ruff format .           # lint + format
python scripts\make_token.py --user alice
git switch -c feature/whatever         # branch per change
```

### When you change something

| Change | What to do |
|---|---|
| Python code in `broker/` | nothing — `--reload` picks it up |
| `serving/mock/server.py` | restart the mock (it runs without `--reload`) |
| `broker/requirements.txt` | `pip install -r broker\requirements-dev.txt` |
| `db/init.sql` | `.\scripts\local_postgres.ps1 reset` (destroys local data) |
| `serving/models.*.yaml` | restart the broker — the registry loads at startup |
| `.env` | restart the broker |
| Anything in `security.py` | run `pytest -q` before committing |

---

## Troubleshooting

Every row here is an error we actually hit while building this.

| Symptom | Cause and fix |
|---|---|
| `model registry not found at '/srv/models.yaml'` | `MODELS_FILE` unset. Add `MODELS_FILE=./serving/models.mock.yaml` to `.env` |
| `base_url is 'http://:8000/v1' — is an environment variable unset?` | `models.prod.yaml` needs `VLLM_QWEN_HOST` set in `.env` |
| `HTTP 402 ... depleted your monthly included credits` | HF free tier spent. Switch to `models.mock.yaml` |
| `HTTP 403` with an HTML body, one model only | That provider is not enabled for your HF account. Pick another model |
| `401` on `/keys` while sending `X-Dev-User` | `AUTH_MODE` is not `dev`. Use a token from `make_token.py` |
| `401` on `/v1/*` | Those endpoints want an **API key**, not a login token. Mint one with `POST /keys` |
| `AUTH_MODE=hs256 requires AUTH_JWT_SECRET` at startup | Working as intended — set the secret, or go back to `dev` |
| `Connection refused` on port 5432 | `.\scripts\local_postgres.ps1 start` |
| `error while attempting to bind ... 8080` | A broker is already running: `Get-NetTCPConnection -LocalPort 8080` |
| `asyncpg` fails to build during install | You used Python 3.14. Rebuild the venv with `py -3.12` |
| Empty `content` from `qwen-thinking` | It is a reasoning model — raise `max_tokens` to 512 or more |
| A streamed request bills 0 tokens | The client disconnected before the final usage frame. Consume the whole stream |

---

## Docker on this laptop

**Diagnosis:** Docker Desktop's engine is failing. Its API proxy logs
`dialing 192.168.65.7:2376: context canceled` — the Linux VM that runs the
actual daemon never becomes reachable. `wsl --status` returns nothing and
`wsl -l -v` prints usage text, so the WSL2 backend is not properly installed;
only the legacy inbox `wsl.exe` stub (10.0.22621) is present.

**Fixing that requires administrator rights.** `wsl --install`, enabling the
*Virtual Machine Platform* Windows feature, and adding your account to the
`docker-users` group are all elevation-gated.

### Path A — ask IT (do this in parallel, it costs you nothing)

Request, specifically:

1. Enable Windows features **Virtual Machine Platform** and **Windows
   Subsystem for Linux**, then run `wsl --install` / `wsl --update`.
2. Add your account to the local **`docker-users`** group.

Cite that it is needed to run the project's container stack.

### Path B — develop on an OpenStack VM (recommended regardless)

The better answer even once Docker works, because it is where the code
actually ships. Zero drift between your dev box and the gateway VM.

```bash
# on a fresh Ubuntu 22.04/24.04 VM
sudo apt update && sudo apt install -y docker.io docker-compose-v2 git
sudo usermod -aG docker $USER && newgrp docker

git clone <your-repo> && cd LLMaaS
cp .env.example .env && nano .env      # set HF_TOKEN
docker compose up -d --build
python3 scripts/smoke.py
```

Edit from Windows with **VS Code + the Remote-SSH extension** — you get your
normal editor, while the code and containers live on the VM. This also gets you
Redis and Vault for free, since they are just more compose services.

### Path C — what you are doing now

Broker in the venv + portable Postgres + `RATE_LIMIT_BACKEND=memory` + the mock
upstream. The full auth → rate limit → route → stream → meter path runs and is
verified end to end. What you *cannot* test this way: the container builds, real
Redis, Vault, and anything network-topology-related.

---

## Why not to install Envoy AI Gateway or Vault yet

**Envoy AI Gateway.** It is a Kubernetes Gateway API implementation — it wants
a cluster, not a laptop. Installing it now means kind/minikube, which needs…
Docker. And it would replace `broker/app/routers/chat.py`, code you are still
actively changing. The broker already does auth, routing and rate limiting.
Adopt Envoy when you hit one of the triggers in
[gateway/README.md](../gateway/README.md) — Python proxying becomes a
bottleneck, you need token-based limits, or you move to Kubernetes.

**Vault.** Same logic: `.env` is a fine PoC secret store, and `.env` is
gitignored. Vault matters when there are real credentials and more than one
person handling them. It is a commented-out service in `docker-compose.yml`,
ready when you are. The migration is in [AUTH.md](AUTH.md#4-secrets).

Neither is on the critical path to a working PoC.

---

## Suggested order of work

1. Backend skeleton — **done**
2. `require_user` implemented (`dev` / `hs256` / `oidc`) — **done**
3. End-to-end verified against Postgres + the mock upstream — **done**
4. Get containers working (Path B, or Path C + Path A in parallel)
5. Frontend repo: React + FastAPI BFF, login, chat view, key page.
   Have the BFF mint `hs256` tokens, and set `AUTH_MODE=hs256` on the broker
6. Real vLLM on a GPU VM; swap `models.dev.yaml` → `models.prod.yaml`
7. TLS everywhere, then Vault for secrets
8. Envoy AI Gateway, if and when the triggers hit
