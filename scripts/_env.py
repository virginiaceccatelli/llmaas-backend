"""Shared credential helpers for the scripts in this directory.

Why this exists: `make_token.py` read settings from `.env`, but `smoke.py` and
`verify.py` read only the process environment. That difference is invisible
under AUTH_MODE=dev — nothing needs a credential — and turns into a confusing
"missing bearer token" the moment the broker is switched to hs256, even though
the secret is sitting in `.env` two directories up.

Both scripts now source credentials the same way, so the commands written in
docs/WORKFLOW.md work in whichever mode the broker happens to be running.

Stdlib only, deliberately: these scripts are meant to run anywhere the broker
runs, with no venv and nothing installed.
"""
import base64
import hashlib
import hmac
import json
import os
import pathlib
import time

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


def load_dotenv(path: pathlib.Path | None = None) -> dict[str, str]:
    """Parse a .env file into a dict. Missing file is not an error."""
    path = path or (REPO_ROOT / ".env")
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def settings() -> dict[str, str]:
    """`.env` overlaid with the real environment — the environment wins.

    Same precedence rule the broker itself uses in config.load_env_file, so a
    script and the broker it is testing never disagree about which value is in
    effect.
    """
    return {**load_dotenv(), **os.environ}


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def mint_token(sub: str, env: dict[str, str] | None = None, ttl_s: int = 600) -> str:
    """An HS256 control-plane token for `sub`.

    Hand-rolled rather than using PyJWT so these scripts stay dependency-free.
    The claims match the frontend BFF's broker.mint_control_token and
    scripts/make_token.py — see contracts/control_token.md.

    Returns "" when no signing secret is configured, so callers can fall back
    to dev-mode headers.
    """
    env = env if env is not None else settings()
    secret = env.get("AUTH_JWT_SECRET", "")
    if not secret:
        return ""

    header = _b64(json.dumps({"alg": "HS256", "typ": "JWT"},
                             separators=(",", ":")).encode())
    now = int(time.time())
    claims = {"sub": sub, "iat": now, "exp": now + ttl_s}
    # Only set when configured: the broker checks iss/aud only if IT has them
    # set, and a claim it is not expecting is simply ignored.
    if env.get("AUTH_JWT_ISSUER"):
        claims["iss"] = env["AUTH_JWT_ISSUER"]
    if env.get("AUTH_JWT_AUDIENCE"):
        claims["aud"] = env["AUTH_JWT_AUDIENCE"]
    payload = _b64(json.dumps(claims, separators=(",", ":")).encode())
    sig = _b64(hmac.new(secret.encode(), f"{header}.{payload}".encode(),
                        hashlib.sha256).digest())
    return f"{header}.{payload}.{sig}"
