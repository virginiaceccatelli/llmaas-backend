# Third-party accounts and services

## Needed now, to run the PoC

### 1. Hugging Face — **required**, free

- Sign up at <https://huggingface.co>.
- **Settings → Access Tokens → Create new token**, with the
  *"Make calls to Inference Providers"* permission.
- Put it in `.env` as `HF_TOKEN=hf_...`.

**The free tier is a small monthly credit, not unlimited.** Once it is spent,
every model returns `HTTP 402 "You have depleted your monthly included
credits"` — an account state, not a broker bug. Either wait for the monthly
reset, buy a few dollars of pre-paid credits, or switch the broker to the free
offline mock (`MODELS_FILE=./serving/models.mock.yaml`).

Used for **two different things** — don't confuse them:

| Where | Why | Cost |
|---|---|---|
| **Dev (now)** — broker → `router.huggingface.co` | Stands in for a GPU so you can test the full flow from a laptop | Small free credit, then pay-per-token |
| **Prod (later)** — GPU VM downloads weights | Fetching `Qwen/Qwen2.5-7B-Instruct` | Free; token not even required for open models |

Once real vLLM is running, the dev usage goes away entirely and the only
remaining dependency is a weights download.

### 2. Nothing else

Postgres, Redis, vLLM, Envoy and Vault are all self-hosted containers. No
account, no license, no egress.

---

## Needed later, in rough order

| When | Service | Purpose | Notes |
|---|---|---|---|
| Before any public URL | **TLS certificates** | HTTPS on the frontend VM | Let's Encrypt via Caddy/Traefik is free and automatic; check whether your institution already issues certs |
| Before public URL | **DNS** | A domain to point at the frontend VM | May already be available from your institution |
| When you add login | **Identity provider** | User accounts, MFA, SSO | Self-host **Authentik** (no third party, no cost) or use Auth0/Clerk (faster, external dependency, per-MAU pricing) — see [AUTH.md](AUTH.md) |
| When you charge money | **Payment processor** | Billing on the `usage` table | Stripe is the default. Real compliance obligations attach here — do not build this until the rest is solid |
| When you have users | **Transactional email** | Verification, password reset, quota alerts | Postmark / SES / your institution's SMTP relay |
| Whenever | **Error tracking** | Exceptions in broker and frontend | Sentry has a free tier; self-hostable |

---

## Not third-party, but on the same critical path

- **OpenStack project quota** — you need enough vCPU/RAM for three VMs plus a
  GPU flavour with PCI passthrough. Request this early; GPU quota approval is
  usually the long pole.
- **Model licences** — Qwen2.5 instruct models are Apache 2.0, so commercial
  use is fine. Check the licence on the model card before swapping in any
  other model; several popular open-weight families have use restrictions.
