"""
Deep verification of a running broker. Stdlib only — no pip install needed.

    python scripts/verify.py                         # defaults to :8080
    python scripts/verify.py http://127.0.0.1:8091

Goes well beyond scripts/smoke.py: streaming, metering accuracy, multi-model
routing, rate limiting, tenant isolation, and the error paths. Each check
prints PASS / FAIL / SKIP with a reason, and the exit code is non-zero if
anything failed.

Checks that need multiple users are skipped when AUTH_MODE is not "dev",
because a single login token only proves one identity.
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8080").rstrip("/")
CONTROL_TOKEN = os.environ.get("LLMAAS_CONTROL_TOKEN", "")
# The mock upstream exposes /debug/last so we can assert what the broker sent.
MOCK_URL = os.environ.get("MOCK_URL", "http://127.0.0.1:8000").rstrip("/")

results: list[tuple[str, str, str]] = []


def call(method, path, body=None, key=None, user=None, raw=False, timeout=120):
    """Returns (status, parsed_or_text). Never raises on HTTP errors."""
    req = urllib.request.Request(BASE + path, method=method)
    if key:
        req.add_header("Authorization", f"Bearer {key}")
    elif CONTROL_TOKEN:
        req.add_header("Authorization", f"Bearer {CONTROL_TOKEN}")
    elif user:
        req.add_header("X-Dev-User", user)
    data = None
    if body is not None:
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, data, timeout=timeout) as r:
            payload = r.read()
            return r.status, (payload.decode("utf-8", "replace") if raw
                              else json.loads(payload or b"null"))
    except urllib.error.HTTPError as e:
        text = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(text)
        except json.JSONDecodeError:
            return e.code, text
    except Exception as e:  # noqa: BLE001
        return 0, f"{type(e).__name__}: {e}"


def mock_get(path):
    """Query the mock upstream directly (not through the broker)."""
    try:
        with urllib.request.urlopen(MOCK_URL + path, timeout=5) as r:
            return r.status, json.loads(r.read() or b"null")
    except Exception as e:  # noqa: BLE001
        return 0, f"{type(e).__name__}: {e}"


def record(name, ok, detail="", skip=False):
    state = "SKIP" if skip else ("PASS" if ok else "FAIL")
    results.append((state, name, str(detail)))
    print(f"  {state:4}  {name}" + (f"  —  {detail}" if detail else ""))
    return ok


def new_key(user, label="verify"):
    st, body = call("POST", "/keys", {"label": label}, user=user)
    return body["api_key"] if st == 201 else None


def usage_for(user):
    st, body = call("GET", "/usage", user=user)
    if st != 200 or not isinstance(body, list):
        return {}
    return {r["model"]: r for r in body}


def main() -> int:
    print(f"\nVerifying {BASE}\n")

    # ---------------------------------------------------------------- infra
    print("Infrastructure")
    st, body = call("GET", "/ready")
    checks = body.get("checks", {}) if isinstance(body, dict) else {}
    record("broker is ready", st == 200 and body.get("status") == "ok", checks)
    if st != 200:
        print("\nBroker not reachable — start it first.\n")
        return 1

    dev_mode = not CONTROL_TOKEN
    st, _ = call("GET", "/keys", user="probe-dev-mode")
    if not dev_mode:
        record("auth mode", True, "token-based (multi-user checks will skip)")
    else:
        record("auth mode", st == 200, "dev (X-Dev-User trusted)")

    # ------------------------------------------------------------ key mgmt
    print("\nAPI keys")
    alice = f"verify-alice-{uuid.uuid4().hex[:8]}"
    bob = f"verify-bob-{uuid.uuid4().hex[:8]}"
    ak = new_key(alice)
    record("key can be minted", bool(ak), ak[:13] + "..." if ak else "failed")
    if not ak:
        return 1

    st, body = call("GET", "/keys", user=alice)
    record("key is listed once", st == 200 and len(body) == 1)
    record(
        "plaintext key is never returned again",
        st == 200 and all("api_key" not in k for k in body),
    )

    st, _ = call("GET", "/v1/models", key="wiit_totally_made_up_key")
    record("forged key rejected", st == 401, f"HTTP {st}")

    # ------------------------------------------------------------- routing
    print("\nModel routing")
    st, body = call("GET", "/v1/models", key=ak)
    models = [m["id"] for m in body.get("data", [])] if st == 200 else []
    record("models advertised", bool(models), models)
    record(
        "upstream ids are not leaked",
        all("/" not in m for m in models),
        "public names only",
    )

    working = []
    for m in models:
        st, body = call("POST", "/v1/chat/completions", {
            "model": m,
            "messages": [{"role": "user", "content": "Reply with exactly: pong"}],
            "max_tokens": 512,     # thinking models need headroom
        }, key=ak)
        content = ""
        if st == 200:
            content = (body["choices"][0]["message"].get("content") or "").strip()
            working.append(m)
        record(f"chat via {m}", st == 200 and bool(content),
               repr(content[:40]) if st == 200 else str(body)[:90])

    st, body = call("POST", "/v1/chat/completions",
                    {"model": "no-such-model", "messages": []}, key=ak)
    record("unknown model gives 404", st == 404, f"HTTP {st}")

    st, body = call("POST", "/v1/chat/completions", b"{not json", key=ak)
    record("malformed JSON gives 400", st == 400, f"HTTP {st}")

    if not working:
        print("\nNo working model — remaining checks need one.\n")
        return 1
    model = working[0]

    # ------------------------------------------------------------ metering
    print("\nUsage metering")
    before = usage_for(alice).get(model, {}).get("requests", 0)
    call("POST", "/v1/chat/completions", {
        "model": model, "messages": [{"role": "user", "content": "one two three"}],
        "max_tokens": 512}, key=ak)
    time.sleep(0.5)
    after = usage_for(alice).get(model, {})
    record("non-streamed request is metered",
           after.get("requests", 0) == before + 1,
           f"{before} -> {after.get('requests')}")
    record("token counts are non-zero",
           after.get("prompt_tokens", 0) > 0 and after.get("completion_tokens", 0) > 0,
           f"{after.get('prompt_tokens')}p / {after.get('completion_tokens')}c")

    # ----------------------------------------------------------- streaming
    print("\nStreaming")
    before = usage_for(alice).get(model, {}).get("completion_tokens", 0)
    st, text = call("POST", "/v1/chat/completions", {
        "model": model, "messages": [{"role": "user", "content": "count to three"}],
        "max_tokens": 512, "stream": True}, key=ak, raw=True)
    frames = [ln for ln in str(text).splitlines() if ln.startswith("data:")]
    record("stream returns SSE frames", st == 200 and len(frames) > 1, f"{len(frames)} frames")
    record("stream terminates with [DONE]", any("[DONE]" in f for f in frames))
    time.sleep(1.0)
    after = usage_for(alice).get(model, {}).get("completion_tokens", 0)
    record("streamed request is metered (the hard case)", after > before,
           f"completion_tokens {before} -> {after}")

    # ------------------------------------------------------ tenant isolation
    print("\nTenant isolation")
    if not dev_mode:
        record("second tenant", False, "needs AUTH_MODE=dev", skip=True)
        record("cross-tenant key revocation blocked", False, "needs AUTH_MODE=dev", skip=True)
        record("usage is per tenant", False, "needs AUTH_MODE=dev", skip=True)
    else:
        bk = new_key(bob)
        record("second tenant can mint a key", bool(bk))
        st, body = call("GET", "/keys", user=bob)
        record("tenants do not see each other's keys",
               st == 200 and len(body) == 1 and all(k["prefix"] != ak[:13] for k in body))

        st, akeys = call("GET", "/keys", user=alice)
        victim = akeys[0]["id"]
        st, _ = call("DELETE", f"/keys/{victim}", user=bob)
        record("cross-tenant key revocation blocked", st == 404, f"HTTP {st}")
        st, _ = call("GET", "/v1/models", key=ak)
        record("victim's key still works after that attempt", st == 200, f"HTTP {st}")

        call("POST", "/v1/chat/completions", {
            "model": model, "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 512}, key=bk)
        time.sleep(0.5)
        au = usage_for(alice).get(model, {}).get("requests", 0)
        bu = usage_for(bob).get(model, {}).get("requests", 0)
        record("usage is per tenant", bu == 1 and au > 1, f"alice={au} bob={bu}")

        # cache_salt only reaches vLLM-kind upstreams; the mock records it.
        salt_a = salt_b = None
        st, before_last = mock_get("/debug/last")
        seq_before = before_last.get("seq") if isinstance(before_last, dict) else None
        if st == 200 and seq_before is not None:
            # Confirm the broker actually routes to THIS mock; a stale reading
            # from an unrelated mock would otherwise fake a pass.
            call("POST", "/v1/chat/completions", {
                "model": model, "messages": [{"role": "user", "content": "probe"}]}, key=ak)
            _, probe = mock_get("/debug/last")
            if probe.get("seq") == seq_before:
                st = 0      # mock never saw it -> broker is pointed elsewhere
        if st != 200 or seq_before is None:
            record("per-tenant cache_salt", False,
                   "broker is not routed to the mock at " + MOCK_URL, skip=True)
        else:
            call("POST", "/v1/chat/completions", {
                "model": model, "messages": [{"role": "user", "content": "x"}]}, key=ak)
            _, la = mock_get("/debug/last")
            salt_a = la.get("cache_salt")
            call("POST", "/v1/chat/completions", {
                "model": model, "messages": [{"role": "user", "content": "x"}]}, key=bk)
            _, lb = mock_get("/debug/last")
            salt_b = lb.get("cache_salt")
            record("cache_salt sent to vLLM upstream", bool(salt_a), salt_a)
            record("cache_salt differs per tenant (blocks cache-timing leak)",
                   bool(salt_a) and salt_a != salt_b)

    # --------------------------------------------------------- rate limiting
    print("\nRate limiting")
    rl_user = f"verify-rl-{uuid.uuid4().hex[:8]}"
    rk = new_key(rl_user)
    codes = []
    for _ in range(70):
        st, _ = call("GET", "/v1/models", key=rk, timeout=10)
        codes.append(st)
        if st == 429:
            break
    limited = 429 in codes
    record("rate limit eventually triggers", limited,
           f"429 after {codes.index(429) + 1} requests" if limited
           else f"no 429 in {len(codes)} requests")
    if limited:
        record("limit is not absurdly low", codes.index(429) >= 10,
               f"allowed {codes.index(429)}")

    # ------------------------------------------------------------ revocation
    print("\nRevocation")
    st, keys = call("GET", "/keys", user=alice)
    st, _ = call("DELETE", f"/keys/{keys[0]['id']}", user=alice)
    record("owner can revoke own key", st == 200)
    st, _ = call("GET", "/v1/models", key=ak)
    record("revoked key stops working immediately", st == 401, f"HTTP {st}")
    st, _ = call("DELETE", "/keys/not-a-uuid", user=alice)
    record("bad key id gives 404 not 500", st == 404, f"HTTP {st}")

    # ----------------------------------------------------------------- done
    failed = [r for r in results if r[0] == "FAIL"]
    skipped = [r for r in results if r[0] == "SKIP"]
    passed = [r for r in results if r[0] == "PASS"]
    print(f"\n{len(passed)} passed, {len(failed)} failed, {len(skipped)} skipped")
    if failed:
        print("\nFailures:")
        for _, name, detail in failed:
            print(f"  - {name}: {detail}")
    print()
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
