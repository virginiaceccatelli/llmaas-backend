"""
Mint a control-plane JWT for testing AUTH_MODE=hs256.

In production the frontend BFF does this after a user logs in — this script
exists so you can test the broker before the frontend exists.

    python scripts/make_token.py --user alice@example.com
    python scripts/make_token.py --user alice --expires-in 30    # seconds

Then:
    curl -H "Authorization: Bearer <token>" localhost:8080/keys
"""
import argparse
import datetime as dt
import os
import pathlib
import sys

try:
    import jwt
except ImportError:
    sys.exit("PyJWT not installed. Run: pip install -r broker/requirements-dev.txt")


def load_dotenv(path: pathlib.Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def main() -> int:
    repo_root = pathlib.Path(__file__).resolve().parent.parent
    env = {**load_dotenv(repo_root / ".env"), **os.environ}

    p = argparse.ArgumentParser()
    p.add_argument("--user", required=True, help="user id -> the `sub` claim")
    p.add_argument("--expires-in", type=int, default=3600, help="seconds (default 3600)")
    p.add_argument("--secret", default=env.get("AUTH_JWT_SECRET", ""))
    p.add_argument("--issuer", default=env.get("AUTH_JWT_ISSUER", ""))
    p.add_argument("--audience", default=env.get("AUTH_JWT_AUDIENCE", ""))
    args = p.parse_args()

    if not args.secret:
        return int(bool(sys.stderr.write(
            "No secret. Set AUTH_JWT_SECRET in .env or pass --secret.\n"
            "Generate one with:\n"
            "  python -c \"import secrets; print(secrets.token_urlsafe(32))\"\n"
        ))) or 1

    now = dt.datetime.now(dt.timezone.utc)
    claims = {
        "sub": args.user,
        "iat": now,
        "exp": now + dt.timedelta(seconds=args.expires_in),
    }
    # Only include these if configured — the broker verifies them only when
    # its own AUTH_JWT_ISSUER / AUTH_JWT_AUDIENCE are set.
    if args.issuer:
        claims["iss"] = args.issuer
    if args.audience:
        claims["aud"] = args.audience

    print(jwt.encode(claims, args.secret, algorithm="HS256"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
