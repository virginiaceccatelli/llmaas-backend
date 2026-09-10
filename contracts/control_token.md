# Contract: the control-plane token

The credential the **frontend BFF** presents to the **broker** when acting on
behalf of a logged-in human, for `/keys` and `/usage`.

This is not the API key. API keys are the *data plane* (`/v1/*`) and are issued
by the control plane; see [../docs/AUTH.md](../docs/AUTH.md) for why the two are
kept apart.

```
browser --(session cookie, TLS)--> frontend BFF --(this token)--> broker
         session lives here                       signature verified here
```

## Producer and consumer

| | Repo | File | Function |
|---|---|---|---|
| mints | frontend | `app/broker.py` | `mint_control_token` |
| verifies | backend | `broker/app/security.py` | `require_user` -> `_verify_hs256` / `_verify_oidc` |

## The claims

| Claim | Required | Value | Enforced by |
|---|---|---|---|
| `sub` | **yes** | the user id the broker will bill | `_decode_options`: `require: ["exp", "sub"]`, plus an explicit non-empty string check |
| `exp` | **yes** | now + `AUTH_JWT_TTL_S` (default 300s) | `require: ["exp", "sub"]`; expiry raises 401 `token expired` |
| `iat` | no | issue time | not checked |
| `iss` | when configured | `AUTH_JWT_ISSUER`, default `llmaas-frontend` | `verify_iss` — **only when the broker has `AUTH_JWT_ISSUER` set** |
| `aud` | when configured | `AUTH_JWT_AUDIENCE`, default `llmaas-broker` | `verify_aud` — **only when the broker has `AUTH_JWT_AUDIENCE` set** |

Algorithm is **pinned** on the verifying side (`algorithms=["HS256"]`, or
`["RS256", "ES256"]` for OIDC), so `alg: none` and algorithm-confusion attacks
fail rather than being caught by a later check.

## The settings that must match

| Frontend | Backend | Consequence of a mismatch |
|---|---|---|
| `BROKER_AUTH_MODE` | `AUTH_MODE` | every control-plane call 401s |
| `AUTH_JWT_SECRET` | `AUTH_JWT_SECRET` | every control-plane call 401s |
| `AUTH_JWT_ISSUER` | `AUTH_JWT_ISSUER` | 401 **only if the broker sets it** |
| `AUTH_JWT_AUDIENCE` | `AUTH_JWT_AUDIENCE` | 401 **only if the broker sets it** |

The last two rows are the trap. Leaving `AUTH_JWT_AUDIENCE` empty on the broker
does not fail loudly — it **disables the check**, and a token minted for a
different service is then accepted here. The broker logs a warning at startup
and `docs/AUTH.md` says to set both in production; this table is the same
statement in the place you look when it breaks.

## Mode matrix

| Broker `AUTH_MODE` | Frontend `BROKER_AUTH_MODE` | What crosses the wire | Use |
|---|---|---|---|
| `dev` | `dev` | `X-Dev-User: <id>`, unverified | a laptop, only |
| `hs256` | `hs256` | this token, signed with the shared secret | a BFF with no IdP |
| `oidc` | **not implemented in the frontend** | the IdP's own token, forwarded | production target |

`oidc` is the gap. The broker verifies OIDC tokens today (`_verify_oidc`,
covered by `tests/test_auth.py`); the frontend cannot produce them — its
`AUTH_MODES` tuple is `("dev", "hs256")` and it refuses to start otherwise.
Closing that means the BFF stops *minting* a token and starts *forwarding* the
one it got from the IdP redirect. See the integration plan, Phase 2.

## Rotating `AUTH_JWT_SECRET`

Today this is: edit two `.env` files, restart both tiers, and accept that every
in-flight control-plane call between the two restarts fails. Tokens live 5
minutes, so a rolling rotation needs the broker to accept the old **and** new
secret for one TTL — a small change to `_verify_hs256` (try both). Worth writing
when you write the Vault rotation runbook (Phase 6).

## Changing this contract

Both sides, one commit pair:

1. change the frontend, commit, push;
2. `cd frontend && git pull && cd .. && git add frontend` here — that pins the
   new frontend commit to this backend commit;
3. update this file;
4. CI runs both smoke suites against `docker-compose.full.yml`.
