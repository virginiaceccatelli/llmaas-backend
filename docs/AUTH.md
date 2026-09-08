# Authentication — what exists, and exactly what you must add

There are **two independent authentication problems** in this system. Keeping
them separate is the single most important thing to get right.

| | Who authenticates | Against what | Status |
|---|---|---|---|
| **Data plane** | a program (SDK, curl, someone's script) | an **API key** | ✅ implemented |
| **Control plane** | a human in a browser | a **login token** | ✅ implemented (needs configuring) |

---

## 1. Data plane — API keys (done)

`POST /v1/chat/completions`, `GET /v1/models`.

Implemented in [`broker/app/security.py`](../broker/app/security.py) →
`require_key`. What it already does correctly:

- keys are 256 bits of `secrets.token_urlsafe` — unguessable;
- only `SHA-256(key)` is stored; the plaintext is returned once and never again;
- lookup is by hash, so a stolen database dump yields nothing usable;
- revocation is a boolean check on every request (no cached grants);
- the customer's key is **never forwarded upstream** — the broker presents its
  own credential to vLLM (`upstream.build_headers`).

Gaps worth closing before real customers, in priority order:

1. **Key expiry.** Add `expires_at` to `api_keys` and check it in `require_key`.
2. **Per-key limits.** Add `rate_limit_per_min` / `monthly_token_quota` columns
   and read them in `ratelimit.py` instead of the single global setting.
3. **`last_used_at`** so users can find dead keys. Write it asynchronously — do
   not add a DB write to the hot path.
4. **Audit log.** A table recording key create/revoke with actor and timestamp.

---

## 2. Control plane — user login

`POST /keys`, `GET /keys`, `DELETE /keys/{id}`, `GET /usage`.

Implemented in [`broker/app/security.py`](../broker/app/security.py) →
`require_user`, with three modes selected by `AUTH_MODE`:

| `AUTH_MODE` | What it does | Use when |
|---|---|---|
| `dev` *(default)* | Trusts an unverified `X-Dev-User` header | Your laptop, only |
| `hs256` | Verifies a JWT signed by the frontend BFF with a shared secret | You have a BFF but no IdP |
| `oidc` | Verifies a JWT against an identity provider's JWKS | **Production** |

Misconfiguration is caught **at startup**, not on the first request: `hs256`
without `AUTH_JWT_SECRET` and `oidc` without `AUTH_OIDC_JWKS_URL` both refuse
to boot. `dev` mode logs a loud warning on every start.

What the JWT modes check: the signature with the algorithm **pinned** (so
`alg:none` and algorithm-confusion attacks fail), `exp` (tokens *must* carry
one), `sub` (must be present), plus `iss` and `aud` when configured. All of
this is covered by [`tests/test_auth.py`](../tests/test_auth.py).

### Where the login actually belongs

You have a frontend VM running React + a FastAPI backend-for-frontend. **Put
the login there, not in the broker.** The BFF holds the session cookie, and
calls the broker server-to-server. The broker only needs to answer "which user
is this request for?".

```
browser --(session cookie, TLS)--> frontend BFF --(JWT, private net)--> broker
         ^^^ login happens here                   ^^^ verifies signature
```

Note the broker no longer *trusts* the BFF — it verifies a signature. Network
segmentation is one layer, not the whole answer.

### Option A — sessions in the BFF (`AUTH_MODE=hs256`)

No third party. The BFF issues an `HttpOnly`, `Secure`, `SameSite=Lax` session
cookie on login, then mints a short-lived JWT for each call to the broker:

```python
# in the frontend BFF
import jwt, datetime as dt
now = dt.datetime.now(dt.timezone.utc)
token = jwt.encode({
    "sub": user_id,                       # who the broker will bill
    "iss": "llmaas-frontend",
    "aud": "llmaas-broker",
    "iat": now,
    "exp": now + dt.timedelta(minutes=5), # keep it short
}, AUTH_JWT_SECRET, algorithm="HS256")
```

Set the same `AUTH_JWT_SECRET`, `AUTH_JWT_ISSUER` and `AUTH_JWT_AUDIENCE` in
the broker's `.env`. You can test this before the frontend exists:

```powershell
$env:LLMAAS_CONTROL_TOKEN = (python scripts\make_token.py --user alice)
python scripts\smoke.py
```

You still own password reset, email verification, MFA and lockout. That is
real work — do not underestimate it. Store password hashes with **argon2**
(here you *do* want a slow hash: passwords are low-entropy, unlike API keys).

### Option B — an identity provider (`AUTH_MODE=oidc`, recommended)

Use **Authentik** (self-hosted, no vendor, stays on your OpenStack) or
Keycloak/Auth.js. The frontend does the OIDC redirect dance, the BFF forwards
the resulting access token, and the broker verifies it against the IdP's
published keys:

```ini
AUTH_MODE=oidc
AUTH_OIDC_JWKS_URL=https://auth.example.org/application/o/llmaas/jwks/
AUTH_JWT_ISSUER=https://auth.example.org/application/o/llmaas/
AUTH_JWT_AUDIENCE=llmaas-broker
```

MFA, SSO, password reset and account lockout all come for free, which is why
this is the recommendation.

### Before going live

1. Set `AUTH_MODE` to `hs256` or `oidc` — **never leave it as `dev`**.
2. Set `AUTH_JWT_AUDIENCE` and `AUTH_JWT_ISSUER`. Without an audience check a
   token minted for a different service would be accepted here. The broker
   warns about this at startup.
3. Keep token lifetimes short (5–15 min). The broker rejects tokens that carry
   no `exp` at all.
4. Serve the broker over TLS. A bearer token on a plaintext link is a password
   in cleartext.

---

## 3. Machine-to-machine: broker → vLLM

Already handled: vLLM runs with `--api-key=$VLLM_API_KEY` and the broker sends
it. Two things to add for production:

- **TLS** on the private network too. "Private" is not "encrypted", and prompts
  are the most sensitive data in the system.
- Move `VLLM_API_KEY` out of `.env` into **Vault** (see below).

---

## 4. Secrets

Today every secret is in `.env`. That is acceptable for a PoC and not for
production. The migration:

1. Run Vault (there is a commented-out service in `docker-compose.yml`; the
   `-dev` flag makes it **in-memory and unsealed** — never use it for real).
2. Give the broker an AppRole identity.
3. Read `VLLM_API_KEY`, `HF_TOKEN` and the DB password from Vault at startup in
   `broker/app/config.py`, instead of from the environment.

Secrets that need to get there: Postgres password, `VLLM_API_KEY`, `HF_TOKEN`,
the session-signing key, and the OIDC client secret if you choose option B.
