# Integration plan — vLLM, Envoy, OpenStack, PostgreSQL, Vault, Redis

What has to change, in what order, and in which file, to get from the current
proof of concept to the architecture in [architecture.md](../architecture.md).

This plan spans **two repositories**:

| | Repo | Role |
|---|---|---|
| backend | `virginiaceccatelli/llmaas-backend` (this one) | broker + serving config + infra |
| frontend | `virginiaceccatelli/llmaas-frontend` | pages + BFF, the only public tier |

Every phase below has a **Frontend impact** section, because in practice every
change to the broker is a change to its only client.

---

## 0. Where we actually are

Verified by reading both repos, not by reading their READMEs.

| Layer | Planned | Reality today |
|---|---|---|
| Frontend UI + BFF | React/HTML + FastAPI | **done** — HTML/JS + FastAPI BFF, sessions, CSRF, login |
| Broker control plane | keys, usage | **done** — `routers/keys.py`, `routers/usage.py` |
| Broker data plane | `/v1/*` | **done in Python** — `routers/chat.py`, meant to be replaced by Envoy |
| API keys | hashed, shown once | **done** |
| Metering | one row per request | **done** — `metering.py` |
| Rate limiting | per key | **done, requests/min only** — not tokens/min |
| Model routing | config not code | **done** — `serving/models.*.yaml` |
| Tenant cache isolation | per-tenant `cache_salt` | **done in the broker** — `upstream.build_payload` |
| PostgreSQL | keys + usage | **running, not production-shaped** — no migrations, no TLS, one role, no backups |
| Redis | rate-limit counters | **running, unauthenticated**, single purpose |
| vLLM | one process per model | **not deployed** — dev routes to HF's router or an offline mock |
| Envoy AI Gateway | owns `/v1/*` | **not started** — `gateway/` holds a README and nothing else |
| OpenStack | three VM tiers | **not started** — `infra/cloud-init/` exists and is empty |
| Vault | all secrets | **not started** — everything is in `.env` |
| TLS | everywhere | **not started** |

The request path is real end to end. What is missing is the production
substrate. That is the good news: most of what follows is deployment and
configuration, and the genuine code changes are concentrated in two places —
the Envoy migration and the secrets migration.

---

## 1. The frontend question — answer this first, because everything else assumes it

### Are the repos linked?

**Yes, as of commit `5041820`** — `frontend/` is a git submodule pinned to the
frontend's `main`. `docker-compose.full.yml`, `contracts/` and the CI workflow
landed alongside it. What remains is listed in the Phase 0 checklist at the
bottom of this document; the frontend-side items need commits **in the frontend
repo**, followed by a submodule bump here.

The rest of this section is the reasoning, kept because it is the argument for
the shape rather than a record of a decision already made.

---

Before that commit, they were not linked in any mechanical sense:

- `git remote -v` here is `llmaas-backend`. There was no submodule, no
  `.gitmodules`, no subtree, no shared package, and no CI that ran one against
  the other.
- The only links were prose, and two of them are still broken:
  - the frontend's `CONFIG.md` points at `LLMaaS/docs/DEPLOY_OPENSTACK.md` —
    **that file does not exist in this repo**;
  - the frontend's `README.md` points at `../LLMaaS/architecture.md` — a
    relative path that resolves only if someone happens to have checked this
    repo out as a sibling directory *named `LLMaaS`*, which is not even the
    repo's name on GitHub (`llmaas-backend`).
- The two tiers share a hard contract that nothing enforces:

  | Shared thing | Backend | Frontend | Enforced by |
  |---|---|---|---|
  | `AUTH_MODE` / `BROKER_AUTH_MODE` | `broker/app/config.py` | `app/config.py` | a table in a doc |
  | `AUTH_JWT_SECRET` / `ISSUER` / `AUDIENCE` | `.env` | `.env` | copy-paste |
  | JWT claim shape (`sub`, `exp`, `iss`, `aud`) | `security._decode_kwargs` | `broker.mint_control_token` | nothing |
  | `/keys` + `/usage` JSON shape | `routers/keys.py` | `routers/keys.py` | nothing |
  | SSE / OpenAI chunk format | `upstream.stream_sse` | `broker.stream_chat` | nothing |
  | public model names | `serving/models.*.yaml` | the chat page's model picker | nothing |
  | pinned dependency versions | `broker/requirements.txt` | `requirements.txt` | **a comment** reading "so the two tiers cannot drift" |

  That comment is a wish, not a mechanism. It is true today because one person
  wrote both files in one sitting. It stops being true the first time either
  side is edited alone.

### How to link them

**Recommendation: add the frontend to this repo as a git submodule at
`frontend/`.** Keep two GitHub repos, two Docker images, two deploy artifacts —
but make a backend commit record *exactly which frontend commit it was verified
against*.

Why this over the alternatives:

| Option | Verdict |
|---|---|
| **Submodule** (recommended) | Pins a commit pair, so "which frontend went with this broker?" has an answer at rollback time. One `docker compose up` for the whole stack. Cross-repo doc links resolve. The public tier keeps its own repo, history, blast radius and release cadence. Cost: everyone must remember `--recurse-submodules`. |

Do this before any phase below:

```bash
# in this repo
git submodule add https://github.com/virginiaceccatelli/llmaas-frontend.git frontend
git commit -m "link the frontend repo as a submodule"

# cloning from now on
(git clone --recurse-submodules https://github.com/virginiaceccatelli/llmaas-backend.git)
git submodule update --init --recursive

# bumping the frontend to its latest main
cd frontend && git pull origin main && cd ..
git add frontend && git commit -m "bump frontend"
```

### Changes that come with the linkage

**New — `docker-compose.full.yml`** at the repo root. The whole stack on one
host: postgres, redis, broker, and `build: ./frontend`. This is what you run
locally and what proves the two tiers agree before either is deployed. Keep the
two existing compose files exactly as they are — they are the per-VM deployment
units and should stay that way.

**New — `contracts/`** at the repo root. The single source of truth for what
crosses the private network, so it stops living in two `.py` files and a table:

- `contracts/control_token.md` — the exact JWT claim set the broker accepts.
- `contracts/openapi.broker.json` — regenerate in CI from
  `app.main.app.openapi()` and fail the build when it changes without a version
  bump. That is what catches a `/keys` response-shape change breaking the BFF.
- `contracts/requirements.shared.txt` — the versions both tiers pin. Have both
  `requirements.txt` files `-r` it, or add a CI check that diffs them.

**New — `.github/workflows/contract.yml`.** On every push to either repo: check
out both (the submodule makes this one line), bring up
`docker-compose.full.yml`, then run the broker's `scripts/verify.py` **and** the
frontend's `scripts/smoke.py`. Both already exist and are stdlib-only, so this
is nearly free, and it is the mechanism that comment is standing in for.

**Fix — the broken doc links.** Once `frontend/` exists here, the frontend's
`../LLMaaS/architecture.md` becomes `../architecture.md`, and
`docs/DEPLOY_OPENSTACK.md` needs to actually be written (Phase 1).

**Fix — `.gitignore`.** Add `frontend/users.json` and `frontend/.env`. The
frontend's own `.gitignore` covers them inside the submodule, but be explicit —
`users.json` holds password hashes.

---

## 2. Ordering, and why

```
Phase 0  Link the repos, pin the contract           <- do first; it is a day
   |
Phase 1  OpenStack: VMs, networks, security groups  <- the substrate. Nothing
   |                                                  else has anywhere to live
Phase 2  PostgreSQL: migrations, roles, TLS, schema <- before you have data you
   |                                                  care about, and before
   |                                                  Envoy needs the new columns
Phase 3  vLLM on the GPU VM
   |
   +--------------- MILESTONE A: a real, shippable service ---------------
   |                (real GPUs, real DB, TLS; no Envoy, no Vault)
   |
Phase 4  Redis: auth, TLS, sessions, key cache      <- Envoy's rate limiter
   |                                                  needs a Redis it can trust
Phase 5  Envoy AI Gateway                           <- the only large code change
   |
Phase 6  Vault (optional)                           <- any time after Phase 1
```

Two orderings are not negotiable:

- **Postgres schema before Envoy.** Envoy's per-tenant token limits are fed by
  an external-auth call that reads per-key quota columns. Those columns do not
  exist yet. Building Envoy first means building it twice.
- **OpenStack before Envoy.** Envoy's configuration is a description of your
  network. Writing it against a laptop's `127.0.0.1` and then rewriting it for
  the VMs is wasted work.

**Vault and Redis are both marked optional in the brief; they are not equally
optional.** Redis is already load-bearing — it holds the rate-limit counters and
it is the only correct home for the frontend's sessions. Vault genuinely can
wait: `.env` with tight file permissions and a small operator count is a
defensible interim position, and moving off it is a change to one function in
`config.py`.

---

## Phase 1 — OpenStack

The substrate. Three tiers, three security groups, one public IP.

### What has to happen first

1. **Get an OpenStack project with GPU flavors and PCI passthrough enabled.**
   Confirm with your operator: which flavor exposes a GPU, which image has the
   NVIDIA driver (or whether you install it via cloud-init), and whether nested
   virtualisation or hugepages are needed. Everything else waits on this.
2. **Decide the network layout.** One private network (`llmaas-internal`,
   e.g. `10.0.0.0/24`), one router with an external gateway for egress, one
   floating IP — attached to the frontend VM only.
3. **Write the security groups before the VMs.** They are the enforcement of
   "only the frontend tier is public", which today is described in
   `architecture.md` as a property and exists nowhere as a rule.

| VM | Flavor | Ingress allowed | From |
|---|---|---|---|
| `llmaas-frontend` | 2 vCPU / 4 GB | 443, 80 (redirect only) | `0.0.0.0/0` |
| | | 22 | bastion / your VPN only |
| `llmaas-gateway` | 4 vCPU / 8 GB, 50 GB volume | 8080 (broker) | `sg-frontend` **only** |
| | | 22 | bastion only |
| `llmaas-gpu-1` | GPU flavor, 200 GB volume | 8000-8010 (vLLM) | `sg-gateway` **only** |
| | | 22 | bastion only |

Note the security groups reference *each other*, not CIDRs. That survives a
re-IP; a hardcoded `10.0.0.0/24` does not.

### Code and config changes

**`infra/cloud-init/` — currently empty. Create three files.**

- `frontend.yaml` — Docker + compose plugin, Caddy or nginx for TLS, a
  `deploy` user, clone the frontend repo, `docker compose up -d`.
- `gateway.yaml` — Docker + compose plugin, clone this repo, place `.env` from
  the instance metadata (or Vault, after Phase 6), `docker compose up -d`.
- `gpu.yaml` — NVIDIA driver, NVIDIA Container Toolkit, Docker, then
  `docker compose -f serving/docker-compose.vllm.yml up -d`.

Do **not** bake secrets into cloud-init user data; it is readable from inside
the instance at `169.254.169.254` by any process, including a compromised
container. Pass a Vault AppRole `role_id` at most (Phase 6), or drop `.env`
over SSH on first boot.

**New — `infra/openstack/` .** Either a `terraform`/`opentofu` module or a set
of `openstack` CLI scripts creating: network, subnet, router, three security
groups, three servers, one floating IP, and the volumes. Committed, not typed
by hand — the security groups are a security control and need review and
history like any other code.

**New — `docs/DEPLOY_OPENSTACK.md`.** VM sizing, the security-group table
above, the boot order (gateway before frontend, GPU before gateway is useful
but not required — the broker starts fine with an unreachable upstream), and how
to roll a deploy. The frontend's `CONFIG.md` already links to this file; write
it and the link stops being a lie.

**Change — `docker-compose.yml`, the `broker` service.** This is a real
exposure bug for a VM deployment:

```yaml
    ports:
      - "8080:8080"          # <-- binds 0.0.0.0 on every interface
```

On a VM this publishes the broker on **every** interface. If that VM ever gets a
floating IP — even temporarily, to debug — the control plane and the data plane
are on the public internet. Worse, Docker writes its own `DOCKER` iptables
chain ahead of the `INPUT` chain, so a host firewall (ufw/firewalld) does *not*
block a published port; only the OpenStack security group does, and relying on
one layer is exactly what `architecture.md` says not to do. Postgres and Redis
in the same file are correctly bound to `127.0.0.1`; the broker was missed.

```yaml
    ports:
      - "${GATEWAY_PRIVATE_IP:-127.0.0.1}:8080:8080"
```

and add `GATEWAY_PRIVATE_IP=10.0.0.11` to `.env.example`.

**Change — `.env.example`.** Add `GATEWAY_PRIVATE_IP`, `FRONTEND_PUBLIC_FQDN`,
and make `VLLM_QWEN_HOST` / `PRIVATE_IP` non-optional in the production
section with a comment that they must be private addresses.

**Change — `serving/docker-compose.vllm.yml`.** The port binding
`"${PRIVATE_IP:-127.0.0.1}:8000:8000"` fails confusingly when `PRIVATE_IP` is
unset on the GPU VM: vLLM comes up, the logs look perfect, and the broker gets
connection-refused from another host. Make it required — `${PRIVATE_IP:?set
PRIVATE_IP to this VM's private address}` — so it fails at `up` time with a
message instead of at request time without one.

### TLS (do it in this phase, not "later")

- **User to frontend**: Caddy or nginx on the frontend VM with a real
  certificate. Caddy is two lines of config and handles ACME renewal itself.
- **Frontend to broker, broker to vLLM**: "private" is not "encrypted", and
  prompts are the most sensitive data in the system. Either terminate TLS at
  each service with an internal CA, or run the internal hops over WireGuard
  between the three VMs. WireGuard is less work than an internal PKI and gives
  you the same property; revisit when you move to Kubernetes and get mTLS from a
  service mesh.
- The moment TLS is in front of the frontend, set `COOKIE_SECURE=true` — the
  session cookie is a bearer credential and it is currently sent in the clear by
  default.

### Frontend impact

- `BROKER_URL` becomes `https://10.0.0.11:8080` (the gateway's private
  address), set in the frontend VM's `.env`. It is never sent to the browser,
  which the BFF already gets right.
- `COOKIE_SECURE=true`, `ALLOW_PASSWORDLESS=false`, and a real `SESSION_SECRET`
  — all three currently default to the laptop-friendly value. The frontend's
  compose file already forces `ALLOW_PASSWORDLESS=false` and requires
  `SESSION_SECRET`; the bare `uvicorn` path does not, so deploy via compose.
- The frontend VM needs **egress to the gateway VM's private address only**. It
  must not be able to reach the GPU VMs at all. That is a security-group rule,
  and it is worth asserting in a test.

---

## Phase 2 — PostgreSQL, from "running" to "production"

Postgres is already integrated. This phase is about the things that are
irreversible once you have real customer data.

### What has to happen first

**Replace `db/init.sql` with migrations.** `db/init.sql` runs *only on the
first container start* — the `docker-entrypoint-initdb.d` convention. Every
schema change below is unreachable through that file once a volume exists, and
`db.py` already carries the `EXTEND` comment saying so. Adopt Alembic (you are
already a Python shop) or numbered `.sql` files applied by a one-shot job.

Concretely:
- add `alembic` to `broker/requirements.txt`;
- `alembic init db/migrations`, with `sqlalchemy.url` read from `DATABASE_URL`;
- generate `0001_initial` from the current `init.sql` verbatim so existing
  volumes stamp cleanly with `alembic stamp 0001`;
- add a `migrate` service to `docker-compose.yml` that runs `alembic upgrade
  head` and exits, with the broker depending on its completion;
- delete `db/init.sql` in the same commit, or it will drift.

Do this before Phase 3, because Phase 3 puts real usage rows in the table.

### Schema changes needed

Everything below is already flagged with an `EXTEND:` comment where it belongs.
Grouped by what forces them:

**Required by Envoy (Phase 5) — do these now:**

```sql
ALTER TABLE api_keys ADD COLUMN expires_at        TIMESTAMPTZ;
ALTER TABLE api_keys ADD COLUMN last_used_at      TIMESTAMPTZ;
ALTER TABLE api_keys ADD COLUMN rate_limit_per_min INT;      -- NULL = use global
ALTER TABLE api_keys ADD COLUMN token_quota_per_h  BIGINT;   -- NULL = unlimited
ALTER TABLE api_keys ADD COLUMN allowed_models    TEXT[];    -- NULL = all
```

`require_key` must then check `expires_at`, and `ratelimit.check` must read the
per-key column with the global setting as fallback. The `allowed_models` column
is what `registry.py`'s `EXTEND` comment describes as `allowed_plans`; a column
is better than a YAML field because it is per-customer, not per-tier.

**Required for billing:**

```sql
ALTER TABLE usage ADD COLUMN cached_tokens INT NOT NULL DEFAULT 0;
ALTER TABLE usage ADD COLUMN request_id    TEXT;             -- for tracing
CREATE TABLE usage_daily (                                   -- nightly rollup
    day DATE, user_id TEXT, model TEXT,
    requests BIGINT, prompt_tokens BIGINT, completion_tokens BIGINT,
    PRIMARY KEY (day, user_id, model)
);
CREATE TABLE model_pricing (
    model TEXT PRIMARY KEY,
    price_per_1k_prompt NUMERIC(12,6), price_per_1k_completion NUMERIC(12,6),
    valid_from TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

`routers/usage.py` currently scans the whole `usage` table per request with a
`GROUP BY`. That is fine at PoC volume and will not be at a million rows a day —
point it at `usage_daily` for anything older than today, and add the
`?since=`/`?until=` filters the `EXTEND` comment asks for. **The frontend's
usage page is waiting on exactly this**; its own doc lists "usage date filters"
as blocked on the broker.

**Required for real accounts:**

```sql
ALTER TABLE users ADD COLUMN idp_issuer TEXT;
ALTER TABLE users ADD COLUMN idp_subject TEXT;
ALTER TABLE users ADD COLUMN disabled BOOLEAN NOT NULL DEFAULT false;
CREATE TABLE audit_log (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    actor TEXT NOT NULL, action TEXT NOT NULL, target TEXT,
    detail JSONB, created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
DELETE FROM users WHERE id = 'demo-user';   -- seeded by init.sql
```

And **remove the lazy user creation** in `routers/keys.py`:

```python
await conn.execute("INSERT INTO users (id) VALUES ($1) ON CONFLICT DO NOTHING", user_id)
```

Its own comment says to delete it once real registration exists. Under
`AUTH_MODE=oidc` this line means *any* subject in any token the IdP will sign
silently becomes a billable user with no email, no plan and no record of how
they got there. Replace it with a provisioning path that fails closed:
`404 unknown user` if the `sub` has no row.

### Operational changes

- **Least-privilege role.** The broker currently connects as the database
  owner. Create `llmaas_app` with `SELECT/INSERT/UPDATE` on the three tables
  and nothing else; keep DDL to the migration job's role. A SQL-injection bug
  should not be able to `DROP TABLE`.
- **TLS to the database.** `DATABASE_URL=...?sslmode=verify-full` plus a CA
  cert mounted into the broker. `asyncpg` supports this through the DSN; no
  code change beyond the URL.
- **Redis-style secret handling for the password.** `POSTGRES_PASSWORD` is
  `change_me_before_production` in `.env.example`; the compose default
  (`:-llmaas`) means an unset variable silently produces a trivial password
  rather than an error. Change both compose files to `${POSTGRES_PASSWORD:?...}`.
- **Backups.** `pg_dump` to an OpenStack Swift/S3 container nightly, plus WAL
  archiving if you want PITR. Untested backups are not backups — put a restore
  drill in the runbook.
- **Pool sizing.** `db.connect` uses `min_size=1, max_size=10`. That is one
  process; with `--workers 4` you have 40 connections against a default
  `max_connections=100`. Either set the pool from an env var or put PgBouncer
  in front before you scale workers.
- **Drop the port publish.** `"127.0.0.1:5432:5432"` is fine on a VM but the
  comment already says "drop in production" — do it, and use
  `docker compose exec` when you need psql.

### Frontend impact

- **Sessions must leave the process.** `app/session.py` keeps sessions in a
  module-level dict. Two consequences on a VM: any deploy signs every user out,
  and running more than one uvicorn worker means a user's requests land on a
  worker that has never heard of their session. Move the store to Redis
  (Phase 4) or to a Postgres `sessions` table. The file's own `EXTEND` comment
  says Redis, and that is right.
- **There is a key leak hiding in here.** `apikeys.ensure` mints a real API key
  per browser session and `apikeys.release` revokes it at logout. But sessions
  are in memory, so **any restart, crash or deploy orphans every live session's
  key**: it stays valid in `api_keys` forever, labelled `web-ui session`, and
  nothing will ever revoke it. In a PoC that is a few rows. In production it is
  an unbounded set of long-lived credentials nobody knows about. The
  `expires_at` column above is the fix — set a short expiry (session TTL plus a
  margin) on auto-minted keys, and add a reaper. Do this in the same phase as
  the column.
- Once `usage_daily` and `?since=` exist, the frontend's usage page can get its
  date filters and per-key breakdown.

---

## Phase 3 — vLLM on the GPU VM

The least code and the most waiting. `serving/` is already structured for this;
the broker needs no changes at all, which is the point of the registry
indirection.

### What has to happen first

1. GPU VM built with PCI passthrough (Phase 1), NVIDIA driver and NVIDIA
   Container Toolkit installed, `nvidia-smi` working inside a container.
2. Pick and pin the model. `Qwen/Qwen2.5-7B-Instruct` at ~15 GB of weights
   needs a 24 GB card comfortably; below 16 GB VRAM drop to
   `Qwen/Qwen2.5-1.5B-Instruct` as `serving/README.md` already notes.
3. Pre-pull the weights into the `hf-cache` volume before the first real
   traffic. The first start downloads ~15 GB and looks like a hang.

### Changes to `serving/docker-compose.vllm.yml`

**Pin the image.** `image: vllm/vllm-openai:latest` is a production incident
waiting for a `docker compose pull`. Pin an exact tag.

**`--disable-log-requests` no longer exists in current vLLM.** The flag was
inverted — logging is now off by default and opt-in via `--enable-log-requests`.
This matters twice over: with a recent image the container **fails to start** on
an unrecognised argument, and the "no prompt logging" property that `README.md`,
`architecture.md` and `docs/AUTH.md` all claim is being asserted by a flag that
may not be doing anything. Against the pinned version:

- confirm which spelling that version takes (`vllm serve --help`);
- if it is the new one, simply *omit* `--enable-log-requests` and add a comment
  saying the default is off — an absent opt-in is a stronger guarantee than a
  present opt-out;
- add a check to `scripts/verify.py` that greps the vLLM container logs after a
  request and asserts no prompt text appears. The property is claimed in three
  documents; make something test it.

**Pin the prefix-cache hash algorithm.** `cache_salt` — the per-tenant KV-cache
isolation that `upstream.build_payload` sends and that `architecture.md` calls
out as the special focus — is folded into the first block's hash. `sha256` is
the current default and the right choice here; pin it explicitly with
`--prefix-caching-hash-algo=sha256` so a future default change cannot quietly
weaken it.

**Add the second model.** Both `models.dev.yaml` and `models.mock.yaml` expose
`qwen-instruct` and `qwen-coder`, so the frontend's model picker offers two.
`models.prod.yaml` defines only `qwen-instruct`, with the second commented out.
If you cut over without adding it, the chat page offers a model that returns
404. Either start the second vLLM container on port 8001 with its own GPU
(`device_ids: ["1"]`) and uncomment the registry entry, or accept a single model
and confirm the frontend degrades cleanly to one option.

**Per-model resources.** `--gpu-memory-utilization=0.90` and
`--max-model-len=8192` are per process. Two vLLM processes on *one* GPU at 0.90
each will not fit. One process per GPU, or drop each to ~0.45.

### Changes to the broker side

None to the code. The cutover is:

1. `docker-compose.yml`: mount `./serving/models.prod.yaml` instead of
   `./serving/models.dev.yaml`.
2. `.env`: set `VLLM_QWEN_HOST` and `VLLM_API_KEY`.
3. `docker compose up -d --build broker`.

Two things worth adding while you are here:

- **`upstream.build_payload` should clamp `max_tokens`** to a per-plan ceiling
  before it reaches a real GPU, as its `EXTEND` comment says. On the HF router a
  runaway request costs credits; on your own GPU it costs *capacity for every
  other tenant*. This is the moment that comment becomes load-bearing.
- **Streaming leaks the upstream model id.** `chat_completions` rewrites
  `data["model"] = route.name` on the non-streaming path only; `stream_sse`
  relays chunks byte for byte, so every streamed chunk carries
  `"model":"Qwen/Qwen2.5-7B-Instruct"` instead of `"qwen-instruct"`. Customers
  see two different names for the same model depending on `stream`, and it
  discloses your backend choice. Rewrite it in the SSE path too, or accept it
  deliberately and say so in the docs.

### Frontend impact

- The model names in the picker come from `GET /api/models`, which proxies the
  broker's registry, so the cutover is transparent **as long as the public names
  in `models.prod.yaml` match those in `models.dev.yaml`**. That is the whole
  contract between the tiers here, and it is currently unenforced — a good
  candidate for a contract test.
- Real GPU latency is much higher than the mock's. The frontend's
  `BROKER_TIMEOUT_S=120` and the broker's `UPSTREAM_TIMEOUT_S=120` should be
  reviewed together against a real cold-start generation; they are two separate
  timeouts on the same request and the outer one must be the larger.
- `qwen-thinking` (in `models.dev.yaml`) emits `reasoning_content` and can burn
  150+ tokens before any visible output. If a reasoning model reaches
  production, the chat page needs to render or explicitly discard that field, or
  users see an empty reply.

---

## MILESTONE A

At this point you have a real service: three OpenStack VMs, TLS to the user, a
migrated Postgres with backups, real GPUs serving real tokens, per-key auth,
rate limits, metering and a working UI. No Envoy, no Vault.

This is a legitimate place to stop and run a pilot. Everything after this is
about scale, operability and secret hygiene — not about whether the product
works.

---

## Phase 4 — Redis

Already deployed, currently doing one job insecurely.

### Changes

**Authenticate it.** The compose service runs with no `requirepass` and
`redis_url` in `config.py` has no password. Anything that can reach port 6379 on
the gateway VM can read every rate-limit counter and, after the session move
below, hijack any browser session.

```yaml
  redis:
    command: ["redis-server", "--requirepass", "${REDIS_PASSWORD:?set REDIS_PASSWORD}",
              "--maxmemory", "512mb", "--maxmemory-policy", "noeviction",
              "--appendonly", "yes"]
```

and `REDIS_URL=redis://:${REDIS_PASSWORD}@redis:6379/0`.

**`maxmemory-policy` is a correctness decision, not a tuning knob.** The
default `noeviction` is what you want. `allkeys-lru` — the instinctive choice
for "it's just a cache" — would silently evict rate-limit counters under
pressure, which converts a memory problem into a *billing and capacity* problem:
every evicted counter resets a customer's limit to zero. After sessions move
here it would also sign users out at random. Set it explicitly so nobody
"optimises" it later.

**Separate the databases.** `/0` rate limits, `/1` frontend sessions, `/2`
Envoy's rate-limit backend, `/3` the key-lookup cache. One `FLUSHDB` should not
take out four subsystems.

**TLS**, or keep Redis on the WireGuard interface only.

### New uses for Redis

1. **Frontend sessions** (Phase 2's finding). Replace the `_sessions` dict in
   `app/session.py` with `SETEX llmaas:sess:<sid>` holding a JSON blob. The
   dataclass already carries everything that needs to move; the file's `EXTEND`
   comment calls it "a drop-in replacement", and it very nearly is.
   *Note:* the session holds a plaintext API key. Putting it in Redis means that
   key is now at rest in a second system — argue for encrypting the blob with
   `SESSION_SECRET`, or better, take the `expires_at` fix from Phase 2 so the
   blast radius is minutes rather than forever.
2. **API-key lookup cache.** `require_key` does a Postgres round trip on
   *every* inference request. Under Envoy that becomes the ext_authz hot path
   for every single token. Cache `key_hash -> (key_id, user_id, limits)` with a
   30-60 s TTL, and delete the entry in `revoke_key` so revocation stays
   immediate. Revocation latency is the thing to get right: a TTL alone means a
   revoked key keeps working for up to a minute.
3. **Token-bucket state**, if you implement token-based rate limiting in the
   broker rather than waiting for Envoy (see Phase 5).
4. **Envoy's global rate-limit backend** — the `ratelimit` service is
   Redis-backed.

### Frontend impact

The frontend gains a Redis dependency it does not have today: a new
`REDIS_URL` setting, `redis` in `requirements.txt`, connect/disconnect in the
lifespan, and a `/api/health` check for it. It should reach the **gateway VM's**
Redis over the private network — which means opening 6379 from `sg-frontend` to
`sg-gateway`, a widening of the trust boundary. If that is unwelcome, run a
second small Redis on the frontend VM for sessions only; sessions and rate
limits share nothing.

---

## Phase 5 — Envoy AI Gateway

The largest change, and the one to think hardest about before starting.

### Read this before committing to it

Envoy AI Gateway reached 1.0 GA in June 2026 and has since been rebranded
**Agent Router** — same code, same maintainers, unchanged CRD names and API
group, but the docs moved from `aigateway.envoyproxy.io` to
`theagentrouter.ai`. The link in [gateway/README.md](../gateway/README.md) now
redirects; update it.

The important constraint for *this* architecture: **the features you are
adopting Envoy for are the Kubernetes ones.** Token-based rate limiting works
through Envoy Gateway's `BackendTrafficPolicy` and its Redis-backed global
rate-limit service; external auth works through Envoy Gateway's
`SecurityPolicy`. Both are CRDs reconciled by a controller in a cluster. The
standalone `aigw run` CLI takes the same YAML and needs neither Kubernetes nor
Docker, but it is documented as a local-testing path.

So there are three honest options:

| Option | What you get | Cost |
|---|---|---|
| **Defer Envoy to the Kubernetes migration** (recommended) | Keep `routers/chat.py`. Add token-based rate limiting to `ratelimit.py` — roughly a day: charge estimated prompt tokens up front, reconcile against real usage after. | You keep a Python hop on the data plane. At PoC and pilot volume that is not your bottleneck. |
| **k3s on the gateway VM, then Envoy AI Gateway properly** | The real thing: token limits, ext_authz, provider failover, the Kubernetes path you want eventually. | A whole new operational surface — a cluster, a CNI, cert-manager, ingress — on a VM you were running with Docker Compose. |
| **`aigw run` standalone** | Envoy on the data plane with no cluster. | Sheds most of the reason you wanted it. Useful as a spike, not as the destination. |

The recommendation is **defer**, for one specific reason: the two hardest
integration problems below have no obvious answer yet, and both are properties
`architecture.md` names as the security focus. Spike them for a day with
`aigw run` before you commit a quarter to k3s.

### Problem 1 — `cache_salt` has nowhere to live

`upstream.build_payload` injects a per-tenant `cache_salt` into the request
body. That is what stops one customer's KV/prefix cache being reused by another,
and it is the mitigation for cache-timing leakage between tenants that
`architecture.md` calls out by name.

**When Envoy owns `/v1/*`, the broker never sees the body.** The salt is a
per-tenant mutation of a JSON field, which Envoy cannot do from routing rules
alone. The options:

- ext_authz returns `x-llmaas-cache-salt: <hash>` and an **ext_proc** filter
  (or a small Lua filter) injects it into the JSON body. Adds a hop and needs
  writing.
- Give each tenant its own vLLM process. Perfect isolation, does not scale past
  a handful of customers.
- Drop prefix caching entirely (`--no-enable-prefix-caching`). Costs real
  throughput but the property becomes structural rather than configured.
- Keep the broker in the body path for this one mutation, which is most of the
  reason you were removing it.

**Spike this first.** If it has no clean answer, that is a legitimate reason not
to adopt Envoy yet, and a much better one than "we ran out of time".

### Problem 2 — metering moves out of your control

`metering.record` writes one `usage` row per request from `routers/chat.py`,
including the `finally` block that bills partial generations when a client
disconnects mid-stream. That is careful code and it disappears with the file.

Envoy AI Gateway extracts token usage from OpenAI-schema responses into dynamic
metadata (that is how its own token rate limiting works) and can emit it via
OpenTelemetry and access logs. So the replacement is:

- Envoy access log or OTel exporter, with `llmRequestCosts` configured for
  input/output/total tokens;
- an **OTel collector** on the gateway VM;
- a new broker endpoint — `POST /internal/usage`, reachable only from
  `sg-gateway` — that batch-inserts rows, **or** a collector exporter writing
  straight to Postgres.

Verify the disconnect case explicitly. Billing that silently stops counting
abandoned streams is a revenue bug that no test will catch on its own.

### Code changes, when you do it

**Deleted:**
- `broker/app/routers/chat.py` — Envoy proxies `/v1/*` directly to vLLM.
- `broker/app/upstream.py` — with one exception: `build_payload`'s clamping and
  salting logic has to be re-homed (Problem 1) before this can go.
- `broker/app/ratelimit.py` — becomes Envoy's `BackendTrafficPolicy`, still
  Redis-backed.

**New:**
- `broker/app/routers/authz.py` — the ext_authz endpoint. Takes the
  `Authorization` header, does what `require_key` does today (hash, look up,
  check `revoked` and `expires_at`), and returns `200` with headers Envoy uses
  downstream: `x-llmaas-user`, `x-llmaas-key-id`, `x-aigw-limit-input` and
  `x-aigw-limit-output` as `count/unit` (this is exactly the pattern AI
  Gateway's own tenant-quota examples use), plus `x-llmaas-cache-salt` if
  Problem 1 lands that way. Must be fast: Redis-cached (Phase 4), never a cold
  Postgres query.
- `broker/app/routers/usage_ingest.py` — `POST /internal/usage` (Problem 2).
- `gateway/` fills up: `AIGatewayRoute`, one `AIServiceBackend` per vLLM
  process, `BackendSecurityPolicy` for the `--api-key` credential,
  `BackendTrafficPolicy` for token limits, `SecurityPolicy` for ext_authz.

**Changed:**
- `broker/app/main.py` — drop the `chat` router, add `authz` and
  `usage_ingest`; `upstream.connect/disconnect` goes.
- `serving/models.prod.yaml` — becomes the source for generating the
  `AIServiceBackend` resources rather than something the broker reads. Keep one
  file as the source of truth and generate the CRDs from it, or the "adding a
  model is a config change" property in `serving/README.md` quietly becomes "a
  config change in two places that must agree".
- `tests/` — `test_registry.py` still applies; you need new tests for the
  ext_authz endpoint, which is now the single point where every inference
  request is authorised.
- `scripts/verify.py` — its streaming, metering and isolation checks currently
  go through the broker. Repoint at the gateway.

### Frontend impact — this is the part that is easy to miss

**The frontend's single `BROKER_URL` splits in two.** `app/broker.py` uses one
`httpx.AsyncClient` with one `base_url` for both planes:

- `control()` -> `/keys`, `/usage` — still the **broker**
- `data()` and `stream_chat()` -> `/v1/*` — now the **gateway**

Required changes in the frontend repo:

- `app/config.py`: add `gateway_url` alongside `broker_url`.
- `app/broker.py`: a second client, `_gateway`, used by `data()` and
  `stream_chat()`. Consider splitting the module into `control.py` and
  `data.py` — the file's docstring already says the classic way to get this
  wrong is confusing the two credentials, and two base URLs make that concrete.
- `app/routers/chat.py`: unchanged, if the split lands cleanly in `broker.py`.
  That is the payoff for the BFF's existing structure.
- `.env.example` / `docker-compose.yml`: `GATEWAY_URL`.
- Security group: the frontend VM now needs egress to the gateway's Envoy
  listener as well as the broker's port.
- `scripts/smoke.py`: its `/api/models` and `/api/chat` checks now traverse
  Envoy. This is the test that tells you the migration worked.

**Error shapes will change.** `_raise_for_status` reads
`resp.json()["detail"]` — a FastAPI convention. Envoy returns its own error
bodies (OpenAI-shaped, or a plain Envoy 403 from ext_authz), so the BFF's error
handling and the chat page's error rendering both need revisiting. A 429 from
Envoy's rate limiter will not look like the broker's
`"rate limit exceeded (60 requests/min)"` detail string that the UI shows today.

---

## Phase 6 — Vault (optional)

### Decide first: Vault or OpenBao

HashiCorp Vault's Community Edition moved to the BSL in 2023 and HashiCorp is
now part of IBM. **OpenBao** is the Linux Foundation fork, API-compatible, and
is what most new self-hosted deployments pick. For a company shipping this as a
product the licence question is worth five minutes with whoever handles that at
WIIT — the integration work below is identical either way, and the client
library call is the same.

### What has to happen first

**A production Vault is not the commented-out service in
`docker-compose.yml`.** That block runs `server -dev`, which is in-memory,
unsealed, and has a root token printed in the logs. It is a demo. Production
needs: a storage backend (Raft on its own volume, or Postgres), a real TLS cert,
an unseal strategy (auto-unseal against an OpenStack Barbican/KMS if available,
otherwise Shamir keys held by more than one person), and a backup of the storage
that is *separate* from the unseal keys.

That is a real project. It is also why this phase is last: a badly-run Vault is
worse than `.env`, because it adds a single point of failure in front of every
service start while providing the same secrets.

### Secrets to migrate

| Secret | Used by | Today |
|---|---|---|
| `POSTGRES_PASSWORD` | broker, postgres | `.env` |
| `REDIS_PASSWORD` | broker, frontend | does not exist yet (Phase 4) |
| `VLLM_API_KEY` | broker, GPU VM | `.env` on two VMs |
| `HF_TOKEN` | GPU VM (gated repos only) | `.env` |
| `AUTH_JWT_SECRET` | broker **and** frontend | `.env` on two VMs, must match byte for byte |
| `SESSION_SECRET` | frontend | `.env` |
| OIDC client secret | frontend | not yet |

`AUTH_JWT_SECRET` is the one that most justifies Vault: it is a shared secret
that must be identical across two VMs, and rotating it by hand means editing two
`.env` files and restarting both tiers in the right order or every control-plane
call 401s. From Vault it becomes one write and two restarts.

### Code changes

**`broker/app/config.py`.** Add a `load_from_vault()` alongside
`load_env_file()`, called first in the lifespan, populating `os.environ` before
`Settings()` is constructed. Keep the precedence rule the existing helper
already gets right — real environment variables win — so Kubernetes and local
development still work with Vault absent.

```python
# broker/app/config.py
def load_from_vault() -> list[str]:
    """Fetch secrets from Vault into os.environ. No-op if VAULT_ADDR is unset."""
```

Authenticate with **AppRole**: the `role_id` can sit in cloud-init user data
(it is not a secret on its own), and the `secret_id` is delivered by a
response-wrapped token at boot. Add `hvac` to `broker/requirements.txt`.

Note the ordering constraint: `registry.py` expands `${VAR}` in the models YAML
from `os.environ`, so Vault must populate the environment **before**
`registry.load()` runs in the lifespan. `load_env_file` already has that
ordering; keep it.

**`docker-compose.yml`.** Replace the commented `-dev` block with a real
configuration, or — better — do not run Vault on the gateway VM at all. Give it
its own small VM. A secret store that shares a host with the service consuming
its secrets provides less than it appears to.

**Rotation.** The point of Vault is not storage, it is rotation. Write down how
each secret rotates and what breaks during the window. `VLLM_API_KEY` needs vLLM
to accept two keys briefly, or a short maintenance window; `AUTH_JWT_SECRET`
needs the broker to accept the old and new secret simultaneously for one token
TTL (5 minutes), which is a small change to `_verify_hs256` — try both keys —
and worth writing when you write the rotation runbook.

### Frontend impact

The frontend needs `AUTH_JWT_SECRET` and `SESSION_SECRET` from the same Vault,
so it needs `hvac`, its own AppRole (scoped to only those two paths — it must
not be able to read `VLLM_API_KEY` or the database password), and the same
`load_from_vault()` helper. **Copy it deliberately, or extract it.** The two
`load_env_file` functions are already byte-identical copies in two repos; adding
a second duplicated function makes the case for `contracts/` from Phase 0 into a
small shared package instead.

---

## Cross-cutting: observability

Not in the brief, but Phase 5 is very hard to debug without it, and both repos
already have `EXTEND` comments asking for it.

- **Request IDs.** Generate one at the frontend BFF, forward it as
  `X-Request-Id` to the broker, and log it at every hop. `app/main.py` in the
  frontend and `broker/app/main.py` both flag this. It is an hour's work and it
  is the difference between debugging Envoy and guessing.
- **Prometheus.** `prometheus-fastapi-instrumentator` on both services;
  `/metrics` on vLLM is already exposed; Envoy and `aigw` both expose
  `/metrics`.
- **OpenTelemetry tracing** across frontend -> broker -> vLLM. Becomes close to
  mandatory once Envoy is in the path, and the collector is the same one Phase 5
  needs for usage.

---

## Appendix: defects and gaps found while writing this

Ordered by how much they will hurt. Each is actionable now, independent of the
phases above.

| # | Where | What |
|---|---|---|
| 1 | `docker-compose.yml`, broker service | `"8080:8080"` binds every interface. Docker's iptables chain bypasses host firewalls, so only the OpenStack security group stands between the control plane and the internet. Bind to the private IP. |
| 2 | `frontend: app/apikeys.py` + `app/session.py` | Auto-minted session keys are orphaned by any restart — permanently valid credentials accumulating in `api_keys` with nothing tracking them. Needs `expires_at` plus a reaper. |
| 3 | `serving/docker-compose.vllm.yml` | `image: ...:latest` with `--disable-log-requests`, a flag removed from current vLLM. Either the container fails to start or the no-prompt-logging property claimed in three documents is not being enforced by what you think enforces it. Pin the image; verify the flag; test the property. |
| 4 | `broker/app/routers/keys.py` | Lazy `INSERT INTO users ... ON CONFLICT DO NOTHING` means that under `AUTH_MODE=oidc`, any subject the IdP will sign for becomes a billable user. Its own comment says to delete it. |
| 5 | `db/init.sql` | Runs on first container start only. Every schema change in this plan is unreachable through it. Migrations before real data. |
| 6 | `docker-compose.yml`, redis service | No `requirepass`, no `maxmemory-policy`. After sessions move to Redis this is session hijacking, not just leaked counters. |
| 7 | `broker/app/routers/chat.py` | Streaming responses leak the upstream model id (`Qwen/Qwen2.5-7B-Instruct`); the non-streaming path rewrites it to the public name. Same request, two different answers depending on `stream`. |
| 8 | `serving/models.prod.yaml` vs `models.dev.yaml` | prod defines one model, dev defines three. Cutting over silently removes models the frontend's picker offers. |
| 9 | `frontend: app/session.py` | In-memory session store: one worker only, and every deploy signs everyone out. |
| 10 | both `requirements.txt` | "Pinned to the same versions so the two tiers cannot drift" is a comment, not a mechanism. |
| 11 | `frontend: CONFIG.md`, `README.md` | Link to `docs/DEPLOY_OPENSTACK.md` (does not exist) and `../LLMaaS/architecture.md` (wrong path, wrong repo name). |
| 12 | `broker/app/security.py:require_key` | The `hmac.compare_digest` after `WHERE key_hash = $1` compares a value to itself — harmless, but it reads as a timing-safety measure that is not doing anything. |
| 13 | `.env.example`, compose defaults | `POSTGRES_PASSWORD` falls back to `llmaas` when unset rather than failing. Use `${VAR:?message}` for anything that is a credential. |
| 14 | `gateway/README.md` | Links to `aigateway.envoyproxy.io`, which now redirects to `theagentrouter.ai` after the Agent Router rebrand. |

---

## Appendix: phase checklists

**Phase 0 — linkage**
- [x] `git submodule add` the frontend at `frontend/` — pinned at `11f502a`
- [x] `docker-compose.full.yml` brings up all four services, plus the mock
      upstream under the `ci` profile
- [x] `contracts/` — `control_token.md`, `check_drift.py`, `dump_openapi.py`,
      `openapi.broker.json`
- [x] `.github/workflows/contract.yml` runs `verify.py` **and** the frontend's
      `smoke.py` against the full stack
- [x] `serving/models.ci.yaml` so CI needs no HF token
- [ ] **push** — the submodule commit is still local; GitHub does not know
      about the link yet
- [ ] delete the stray `llmaas-backend/` clone at the repo root
- [ ] run the full stack once end to end (needs a working Docker engine —
      see `docs/WORKFLOW.md#docker-on-this-laptop`)
- [ ] **in the frontend repo:** fix `../LLMaaS/architecture.md` ->
      `../architecture.md`, and the `docs/DEPLOY_OPENSTACK.md` link; then bump
      the submodule pointer here
- [ ] `frontend/.env` for the local non-Docker path, with `AUTH_JWT_SECRET`
      matching the backend's and `BROKER_AUTH_MODE=hs256`

**Phase 1 — OpenStack**
- [ ] network, subnet, router, floating IP (frontend only)
- [ ] three security groups, referencing each other, committed as code
- [ ] `infra/cloud-init/{frontend,gateway,gpu}.yaml`
- [ ] `infra/openstack/` terraform or CLI scripts
- [ ] `docs/DEPLOY_OPENSTACK.md`
- [ ] broker port bound to the private IP (defect 1)
- [ ] TLS at the frontend; `COOKIE_SECURE=true`; internal TLS or WireGuard
- [ ] verify from a laptop that ports 8080 and 8000 are unreachable

**Phase 2 — PostgreSQL**
- [ ] Alembic in, `init.sql` out, `0001_initial` stamped
- [ ] key columns: `expires_at`, `last_used_at`, per-key limits, `allowed_models`
- [ ] billing: `usage_daily`, `model_pricing`, `?since=`/`?until=`
- [ ] accounts: `audit_log`, IdP columns, `demo-user` deleted, lazy user creation removed
- [ ] least-privilege role, `sslmode=verify-full`, nightly backup **plus a restore drill**
- [ ] session-key expiry + reaper (defect 2)

**Phase 3 — vLLM**
- [ ] GPU VM: driver, container toolkit, `nvidia-smi` inside a container
- [ ] pinned image tag; log-request flag verified against that tag
- [ ] `--prefix-caching-hash-algo=sha256` pinned
- [ ] weights pre-pulled into the cache volume
- [ ] `models.prod.yaml` matches the public names the frontend expects
- [ ] `max_tokens` ceiling in `build_payload`
- [ ] `verify.py` asserts no prompt text in the vLLM logs

**Phase 4 — Redis**
- [ ] `requirepass`, `maxmemory-policy noeviction`, separate DBs, TLS
- [ ] frontend sessions moved off the in-process dict
- [ ] key-lookup cache with immediate revocation invalidation

**Phase 5 — Envoy**
- [ ] spike: `cache_salt` injection — decide before committing
- [ ] spike: usage metering out of Envoy, including the client-disconnect case
- [ ] decide k3s vs defer
- [ ] ext_authz endpoint + tests
- [ ] `gateway/` CRDs generated from `models.prod.yaml`
- [ ] frontend: `BROKER_URL` / `GATEWAY_URL` split, error-shape handling
- [ ] `verify.py` and `smoke.py` repointed at the gateway

**Phase 6 — Vault**
- [ ] Vault or OpenBao, decided
- [ ] real storage, TLS, unseal strategy, backups separate from unseal keys
- [ ] `load_from_vault()` in both repos, ordered before `registry.load()`
- [ ] AppRole per service, frontend scoped to two paths only
- [ ] rotation runbook, including dual-secret JWT verification
