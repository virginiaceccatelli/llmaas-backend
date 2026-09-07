# Authentication — what exists, and exactly what you must add

There are **two independent authentication problems** in this system. Keeping
them separate is the single most important thing to get right.

| | Who authenticates | Against what | Status |
|---|---|---|---|
| **Data plane** | a program (SDK, curl, someone's script) | an **API key** | ✅ implemented |
| **Control plane** | a human in a browser | a **login session** | ❌ **stub — you must build this** |

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

## 2. Control plane — user login (THIS IS THE GAP)

`POST /keys`, `GET /keys`, `DELETE /keys/{id}`, `GET /usage`.

**Right now these are unauthenticated.** `require_user` in
[`broker/app/security.py`](../broker/app/security.py) returns whatever is in
the `X-Dev-User` header, or `demo-user`. Anyone who can reach port 8080 can
mint an API key for any user id.

That is survivable *only* because the broker sits on a private network. The
broker logs a warning on every startup while `DEV_AUTH_ENABLED=true`.

### Where the login actually belongs

You have a frontend VM running React + a FastAPI backend-for-frontend. **Put
the login there, not in the broker.** The BFF holds the session cookie, and
calls the broker server-to-server. The broker only needs to answer "which user
is this request for?".

```
browser --(session cookie, TLS)--> frontend BFF --(private net)--> broker
         ^^^ auth happens here                     ^^^ trusts the BFF
```

### Pick one of two options

**Option A — sessions in the frontend BFF (simplest, no third party).**

- FastAPI BFF issues a signed, `HttpOnly`, `Secure`, `SameSite=Lax` session
  cookie on login. Store password hashes with `argon2` (here you *do* want a
  slow hash — passwords are low entropy, unlike API keys).
- The BFF then calls the broker with the resolved user id.
- Change `require_user` to verify a short-lived **service token** (a shared
  HMAC/JWT between BFF and broker) and read the user id from its claims.
  Do **not** just keep trusting an unsigned header — network segmentation is
  one layer, not the whole answer.
- You own password reset, email verification, MFA, and lockout. That is real
  work; do not underestimate it.

**Option B — an identity provider (recommended).**

Use **Authentik** (self-hosted, no vendor, matches "keep it on our OpenStack")
or **Auth.js/Keycloak**. Then:

- the frontend does the OIDC redirect dance and receives an access token;
- the BFF passes that token to the broker;
- `require_user` becomes: fetch the IdP's JWKS (cache it), verify the
  signature, check `iss`, `aud`, `exp`, `nbf`, then `return claims["sub"]`.

That's roughly 30 lines with `pyjwt[crypto]`. You get MFA, SSO, password reset
and account lockout for free, which is why this is the recommendation.

### Concretely, to close the gap

1. Implement the body of `require_user` (option A or B).
2. Set `DEV_AUTH_ENABLED=false` in `.env`. With no implementation in place the
   endpoints then return `501` rather than silently trusting a header — a
   deliberate fail-closed.
3. Add a test that `POST /keys` without credentials returns `401`.

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
