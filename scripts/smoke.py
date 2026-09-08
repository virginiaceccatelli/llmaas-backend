"""
End-to-end smoke test. Stdlib only — no pip install needed.

    python scripts/smoke.py

Walks the whole flow: mint a key -> list keys -> call the model with that key
-> check usage was recorded -> revoke the key -> confirm it now fails.
"""
import json
import os
import sys
import urllib.error
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8080"

# With AUTH_MODE=hs256/oidc the control-plane endpoints need a real token:
#   $env:LLMAAS_CONTROL_TOKEN = (python scripts\make_token.py --user alice)
# With AUTH_MODE=dev, leave it unset and the X-Dev-User header is used.
CONTROL_TOKEN = os.environ.get("LLMAAS_CONTROL_TOKEN", "")


def call(method: str, path: str, body=None, token=None, user="demo-user"):
    req = urllib.request.Request(BASE + path, method=method)
    if token:
        # Data plane: the customer's API key.
        req.add_header("Authorization", f"Bearer {token}")
    elif CONTROL_TOKEN:
        # Control plane: the user's login token.
        req.add_header("Authorization", f"Bearer {CONTROL_TOKEN}")
    else:
        req.add_header("X-Dev-User", user)      # dev-auth only
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, data, timeout=120) as resp:
            return resp.status, json.loads(resp.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def step(label, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{'  ' + str(detail) if detail else ''}")
    return ok


def main() -> int:
    ok = True
    print(f"\nSmoke testing {BASE}\n")

    status, body = call("GET", "/ready")
    ok &= step("readiness", status == 200 and body.get("status") == "ok", body)

    status, key = call("POST", "/keys", {"label": "smoke-test"})
    ok &= step("create key", status == 201, key if status != 201 else key.get("prefix"))
    if status != 201:
        return 1
    token, key_id = key["api_key"], key["id"]

    status, body = call("GET", "/keys")
    ok &= step("list keys", status == 200 and any(k["id"] == key_id for k in body))

    status, body = call("GET", "/v1/models", token=token)
    models = [m["id"] for m in body.get("data", [])] if status == 200 else []
    ok &= step("list models", status == 200 and bool(models), models)

    status, _ = call("GET", "/v1/models")
    ok &= step("unauthenticated request is rejected", status == 401, status)

    if models:
        status, body = call("POST", "/v1/chat/completions", {
            "model": models[0],
            "messages": [{"role": "user", "content": "Reply with exactly: pong"}],
            "max_tokens": 16,
        }, token=token)
        reply = (body.get("choices") or [{}])[0].get("message", {}).get("content") \
            if status == 200 else body
        ok &= step("chat completion", status == 200, repr(reply)[:120])

    status, body = call("GET", "/usage")
    ok &= step("usage recorded", status == 200 and len(body) > 0, body)

    status, _ = call("DELETE", f"/keys/{key_id}")
    ok &= step("revoke key", status == 200)

    status, _ = call("GET", "/v1/models", token=token)
    ok &= step("revoked key is rejected", status == 401, status)

    print(f"\n{'ALL CHECKS PASSED' if ok else 'SOME CHECKS FAILED'}\n")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
