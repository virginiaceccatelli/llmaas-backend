# Next steps — from "it works on a laptop" to an integrated PoC

The ordered runbook. [INTEGRATION_PLAN.md](INTEGRATION_PLAN.md) is the reference
for *what* each component needs; this is *what to do next, in order, and why
that order*.

---

## The gateway decision: Envoy AI Gateway, standalone. No Kubernetes.

**Envoy AI Gateway does not require Kubernetes.** The `aigw` CLI runs the same
Gateway API resources as a standalone proxy, no cluster and no Docker. So the
"swap to LiteLLM" condition does not trigger — keep Envoy.

The catch, stated honestly: **standalone mode does not enforce token-based rate
limits or per-tenant quotas.** Those go through Envoy Gateway's Redis-backed
rate-limit service and `SecurityPolicy` ext_authz, which are the Kubernetes
path. Standalone does give you routing, upstream credential injection, provider
failover, retries, OpenAI-schema normalisation, Prometheus metrics, and
`llmRequestCosts` token *counting*.

### The move that makes this work: put Envoy BELOW the broker

The original plan in `gateway/README.md` had Envoy replacing
`broker/app/routers/chat.py` and sitting in *front* of the broker. That is what
forces Kubernetes: once Envoy terminates the customer's request, per-customer
API-key auth has to become an ext_authz call, and the tenant `cache_salt` and
usage metering have to be rebuilt outside the broker. Three hard problems.

Invert it:

```
browser ──► frontend BFF ──► broker ──► Envoy AI Gateway ──► vLLM
            session          API keys    routing              the model
            CSRF             rate limit  upstream creds
                             metering    failover, retries
                             cache_salt  token counting, metrics
```

Everything the broker already does correctly, it keeps — because it still sees
the request body. Envoy does the things the broker does badly or not at all.
No Kubernetes, and **no broker code changes**: the model registry already
indirects through `base_url`, so pointing at Envoy instead of vLLM is a
config-only change. That indirection was built for exactly this.

Later, when Kubernetes arrives, Envoy moves in front and auth becomes ext_authz.
The broker's `routers/` split still supports that. Nothing here is wasted.

### Why not LiteLLM

Your rule was "swap if Envoy *necessarily* needs Kubernetes." It doesn't, so
no swap. Worth knowing what you would be trading anyway: LiteLLM's virtual
keys, budgets and TPM/RPM limits overlap almost exactly with the broker you
already built and verified. Adopting it means deleting working, tested code and
inheriting a much larger dependency. It stays the fallback if Envoy standalone
turns out not to fit — not the default.

---

## Phase A — Isolation. Start today, no infrastructure needed.

**This is the actual next step.** A multi-tenant LLM platform whose users are
not separated is not a PoC of the thing you are building, and no amount of
Envoy or vLLM fixes it.

What is *already* isolated, verified by `scripts/verify.py`: API keys, usage
rows, per-key rate limits, and the per-tenant `cache_salt`. One user cannot see
or revoke another's keys. That part is real.

What is not:

### A1. Anyone can sign in as anyone  ← the big one

`ALLOW_PASSWORDLESS=true` with no `users.json` means the login page accepts any
user id with no password. Every "user" is a costume, not an identity.

```powershell
cd frontend
python scripts\users.py add alice      # prompts for a password, scrypt-hashed
python scripts\users.py add bob
# then in frontend\.env:
ALLOW_PASSWORDLESS=false
```

That is the ten-minute fix and it is enough for a PoC demo. The real answer is
OIDC (Authentik on your OpenStack) — the broker already verifies OIDC tokens
and has tests for it; the *frontend* is the half that cannot yet produce them
(`app/broker.py`'s `AUTH_MODES` is `("dev", "hs256")`). Defer that to Phase D
unless you need SSO for the demo.

### A2. Chat history is per-browser, not per-user

`app/static/app.js` keeps conversations in `localStorage`. Two people using one
browser share a history; one person on two devices has two. That is why it
feels like "the same chat page".

**Decide this deliberately, because it contradicts a stated security property.**
`db/init.sql` says, and `architecture.md` repeats, that no prompt or completion
text is stored anywhere. Server-side history means storing exactly that. Your
options:

| | Consequence |
|---|---|
| **Keep localStorage** | The privacy claim holds. History stays per-browser. Honest for a PoC if you say so out loud. |
| **Store server-side** (recommended for a realistic PoC) | Real per-user history across devices. You must update the claim in `architecture.md` and `db/init.sql`, add a retention policy, and put it in a privacy notice. |

If you store it: new `conversations` and `messages` tables, a
`broker/app/routers/conversations.py` behind `require_user`, and the chat page
reads/writes through the BFF instead of `localStorage`. Do it in the same
migration as A3 — and do it **after** Alembic exists (Phase B2), not by editing
`db/init.sql`, which only runs on a fresh volume.

### A3. Sessions die on restart, and leak API keys

`app/session.py` holds sessions in a module-level dict. Every restart signs
everyone out, and more than one uvicorn worker breaks sign-in entirely.

Worse, and this is what your `13 key(s)` was: the BFF mints a real API key per
browser session and revokes it at logout, but a restart **orphans** every live
session's key. They stay valid in `api_keys` forever with nothing tracking
them. Two fixes, both needed:

- move the session store to Redis (the file's own `EXTEND` note says so);
- add `expires_at` to `api_keys`, set it on auto-minted keys, and reap.

**Deliverable for Phase A:** two real users, with passwords, who cannot see each
other's keys, usage, or conversations, surviving a restart.

---

## Phase B — OpenStack. Request the VMs now; the quota is the long pole.

### B0. Do this first, today, in parallel with Phase A

Ask your OpenStack operator for:

1. **A GPU flavor with PCI passthrough**, and confirmation of which image has
   the NVIDIA driver. **This is the one blocking unknown in the whole plan.**
2. A project quota for 3 instances, 1 floating IP, ~300 GB of volumes.
3. Whether you may open a floating IP to 0.0.0.0:443.

**If there is no GPU quota, say so early** — it changes Phase C but does not
block it. Fallbacks, in order of preference: run vLLM CPU-only with a tiny
model (`Qwen/Qwen2.5-0.5B-Instruct`) purely to prove the integration path; or
keep the Hugging Face router as the upstream behind Envoy. Either still
exercises every component. A slow real vLLM proves more than a fast mock.

### B1. Three VMs, one public interface

| VM | Runs | Public? |
|---|---|---|
| `llmaas-frontend` | BFF + Caddy (TLS) | **yes** — the only one |
| `llmaas-gateway` | broker, Postgres, Redis, **Envoy AI Gateway** | no |
| `llmaas-gpu-1` | vLLM | no |

Who may talk to whom — this is the "access" question, and it is enforced by
security groups referencing *each other*, not by CIDRs:

| From | To | Port | Why |
|---|---|---|---|
| world | `sg-frontend` | 443 | the only public entry |
| `sg-frontend` | `sg-gateway` | 8080 | BFF → broker control + data plane |
| `sg-gateway` | `sg-gpu` | 8000 | Envoy → vLLM |
| you / bastion | all | 22 | admin |
| **`sg-frontend`** | **`sg-gpu`** | **nothing** | the frontend must never reach a GPU |

Note what is *absent*: the browser never reaches the broker, and the frontend
never reaches vLLM. Envoy's listener (1975) is not in the table at all — it is
localhost-only on the gateway VM, reached only by the broker in the same host.

### B2. Before any real data exists

- **Alembic in, `db/init.sql` out.** Every schema change in Phase A and later
  is unreachable through `init.sql`, which runs only on a fresh volume.
- `DELETE FROM users WHERE id = 'demo-user'`, and remove the lazy
  `INSERT INTO users ... ON CONFLICT DO NOTHING` in `routers/keys.py` — under
  OIDC it turns any signable subject into a billable user.
- Redis gets a password (`--requirepass`) and `maxmemory-policy noeviction`.
  An LRU policy would silently evict rate-limit counters and, after A3,
  sessions.
- **Fix the broker's port binding.** `docker-compose.yml` publishes
  `"8080:8080"` — every interface. On a VM with a floating IP that is the
  control plane on the internet, and Docker's iptables chain sits ahead of any
  host firewall. Bind it to the private IP.
- TLS at the frontend (Caddy handles ACME itself), then `COOKIE_SECURE=true`.

---

## Phase C — vLLM, then Envoy

Order matters: bring vLLM up and prove the broker talks to it **directly**
first. Only then put Envoy in between. Debugging one new component at a time is
the difference between an afternoon and a week.

### C1. vLLM on the GPU VM

`serving/docker-compose.vllm.yml` is ready, with three fixes needed first:

- **Pin the image.** `vllm/vllm-openai:latest` is an incident waiting for a
  `docker compose pull`.
- **`--disable-log-requests` no longer exists** in current vLLM — logging is
  now opt-in via `--enable-log-requests`. With a recent image the container
  fails to start, and the "no prompt logging" property three of your documents
  claim is being asserted by a flag that may not exist. Check
  `vllm serve --help` against the tag you pin; if it is the new spelling, just
  omit the flag and note that the default is off.
- **Pin `--prefix-caching-hash-algo=sha256`** so a future default change cannot
  quietly weaken the `cache_salt` tenant isolation.

Then: `MODELS_FILE` → `models.prod.yaml`, set `VLLM_QWEN_HOST` and
`VLLM_API_KEY`, restart the broker. No code changes. Run `scripts/verify.py` —
all 29 checks should pass against real GPU output.

**Checkpoint: this is already a shippable service.** Real GPUs, real auth, real
isolation, TLS, metering. Everything after is about operability.

### C2. Envoy AI Gateway, standalone

Install on the gateway VM (Linux — `aigw run` does not support Windows, which
is the reason this cannot be tested on your laptop):

```bash
go install github.com/envoyproxy/ai-gateway/cmd/aigw@latest   # or a release binary
aigw run gateway/aigw.yaml
```

Write `gateway/aigw.yaml` modelled on the project's own standalone example
(`examples/aigw/ollama.yaml` — Ollama is a self-hosted OpenAI-compatible
backend, exactly like vLLM). You need five resources in one file:

| Resource | Holds |
|---|---|
| `GatewayClass` + `Gateway` | the listener, port **1975** |
| `Backend` | `hostname: <gpu-vm-private-ip>, port: 8000` |
| `AIServiceBackend` | `schema: OpenAI`, pointing at that Backend |
| `BackendSecurityPolicy` + `Secret` | the `VLLM_API_KEY` Envoy presents to vLLM |
| `AIGatewayRoute` | model-name → backend routing, plus `llmRequestCosts` for token counting |

Then the only change on the broker side, in `serving/models.prod.yaml`:

```yaml
models:
  qwen-instruct:
    kind: vllm
    base_url: http://127.0.0.1:1975/v1     # was the GPU VM directly
    upstream_model: Qwen/Qwen2.5-7B-Instruct
    api_key: ""                            # Envoy now injects the real one
```

**Spike this first, it is 20 minutes and it is the one real risk:** send a
request with `cache_salt` in the body through `aigw` and confirm it arrives at
vLLM. Envoy AI Gateway parses request bodies to count tokens and route by
model, and it may not preserve unknown fields. `serving/mock/server.py` already
records what it receives — point Envoy at the mock and check `/debug/last`.
If `cache_salt` is stripped, your options are a `Backend`-level header/body
mutation, or keeping vLLM's `base_url` direct for now and using Envoy only for
the models that do not need it. Either way, find out before you build the rest.

---

## Phase D — later, deliberately

- **OIDC** (Authentik on OpenStack) — replaces `users.py` wholesale and gives
  you MFA, password reset and lockout for free. The broker is already ready;
  the frontend needs `oidc` added to its `AUTH_MODES`.
- **Vault or OpenBao** — the commented-out compose service is `-dev` mode:
  in-memory, unsealed, root token in the logs. A real one needs storage, TLS,
  an unseal strategy and backups kept separately from the unseal keys. `.env`
  with tight permissions is defensible until then.
- **Kubernetes**, and only then Envoy in front with ext_authz and real token
  rate limiting.

Until Kubernetes, **token-based rate limiting belongs in the broker.** Do not
wait for Envoy to provide it. The current fixed-window limiter in
`ratelimit.py` lets a client timing its bursts around the minute boundary get
close to double its configured limit — measured: a limit of 60 first refused at
request 37, a limit of 15 at request 28. That is a billing and GPU-capacity
problem, and a sliding window plus a token budget is about a day's work in one
file.

---

## The short version

1. **Today:** request the GPU quota (long lead time), and add real passwords —
   `python scripts\users.py add alice`, `ALLOW_PASSWORDLESS=false`.
2. **This week:** finish isolation — Redis sessions, key expiry, and decide the
   chat-history question.
3. **When the VMs land:** Alembic, then the three tiers, security groups, TLS.
4. **Then:** vLLM, prove it directly, and only then slot Envoy underneath the
   broker.
5. **Not yet:** Kubernetes, Vault, OIDC.
