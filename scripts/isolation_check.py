"""Prove two users on ONE browser are isolated.

This is the check behind the PoC demo: sign in as alice, chat, sign out, sign
in as bob in the SAME cookie jar, and confirm bob sees none of alice's
conversations, keys or usage — then sign back in as alice and confirm hers
survived.

Before the conversations moved server-side this could not pass: chat history
lived in the browser's localStorage, so it followed the browser, not the user.

    python scripts/isolation_check.py
    python scripts/isolation_check.py --base http://127.0.0.1:8081 \
        --a alice --a-password ... --b bob --b-password ...

Stdlib only. Exits non-zero on the first failure.
"""
import argparse
import http.cookiejar
import json
import urllib.error
import urllib.request
import uuid

# Unique per run: this script must be re-runnable against a database that
# already holds conversations from a previous run.
RUN = uuid.uuid4().hex[:8]
A_CONV = f"alice-chat-{RUN}"
B_CONV = f"bob-chat-{RUN}"
A_TITLE = f"Alice's private chat {RUN}"
B_TITLE = f"Bob's chat {RUN}"

results: list[tuple[bool, str, str]] = []


def record(name: str, ok: bool, detail: str = "") -> None:
    results.append((ok, name, detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  -  {detail}" if detail else ""))


class Browser:
    """One cookie jar == one browser profile."""

    def __init__(self, base: str):
        self.base = base.rstrip("/")
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar)
        )
        self.csrf = ""

    def call(self, method, path, body=None):
        req = urllib.request.Request(self.base + path, method=method)
        req.add_header("Accept", "application/json")
        if self.csrf and method != "GET":
            req.add_header("X-CSRF-Token", self.csrf)
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            req.add_header("Content-Type", "application/json")
        try:
            with self.opener.open(req, data, timeout=60) as r:
                text = r.read().decode("utf-8", "replace")
                return r.status, (json.loads(text) if text else None)
        except urllib.error.HTTPError as e:
            text = e.read().decode("utf-8", "replace")
            try:
                return e.code, json.loads(text)
            except json.JSONDecodeError:
                return e.code, text
        except Exception as e:  # noqa: BLE001
            return 0, f"{type(e).__name__}: {e}"

    def login(self, user, password):
        st, body = self.call("POST", "/api/login",
                             {"user_id": user, "password": password})
        if st == 200 and isinstance(body, dict):
            self.csrf = body.get("csrf", "")
        return st, body

    def logout(self):
        st, body = self.call("POST", "/api/logout")
        self.csrf = ""
        return st, body


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base", default="http://127.0.0.1:8081")
    ap.add_argument("--a", default="alice")
    ap.add_argument("--a-password", default="alice-password-1")
    ap.add_argument("--b", default="bob")
    ap.add_argument("--b-password", default="bob-password-1")
    args = ap.parse_args()

    br = Browser(args.base)
    print(f"\nOne browser, two users, against {args.base}\n")

    # --- alice ------------------------------------------------------------
    print(f"{args.a} signs in")
    st, _ = br.login(args.a, args.a_password)
    record("sign-in succeeds", st == 200, f"HTTP {st}")
    if st != 200:
        print("\nCannot continue. Is ALLOW_PASSWORDLESS=false with these users?")
        return 1

    st, _ = br.call("PUT", f"/api/conversations/{A_CONV}", {
        "title": A_TITLE,
        "messages": [{"role": "user", "content": "alice secret question"},
                     {"role": "assistant", "content": "alice secret answer"}],
    })
    record("alice saves a conversation", st == 200, f"HTTP {st}")

    st, _ = br.call("POST", "/api/chat", {
        "model": "qwen-instruct", "stream": False,
        "messages": [{"role": "user", "content": "ping"}],
    })
    record("alice can chat (mints her key)", st == 200, f"HTTP {st}")

    st, convs = br.call("GET", "/api/conversations")
    a_titles = [c["title"] for c in convs] if isinstance(convs, list) else []
    record("alice sees her conversation", st == 200 and A_TITLE in a_titles,
           f"{len(a_titles)} conversation(s)")
    st, akeys = br.call("GET", "/api/keys")
    a_keys = len(akeys) if isinstance(akeys, list) else 0
    a_prefixes = [k["prefix"] for k in akeys] if isinstance(akeys, list) else []
    record("alice has key(s)", a_keys > 0, f"{a_keys} key(s)")
    st, a_usage = br.call("GET", "/api/usage")
    a_requests = sum(r.get("requests", 0) for r in a_usage) if isinstance(a_usage, list) else 0

    br.logout()
    print(f"\n{args.b} signs in — SAME cookie jar, same browser")
    st, _ = br.login(args.b, args.b_password)
    record("sign-in succeeds", st == 200, f"HTTP {st}")

    # --- the actual isolation assertions ----------------------------------
    st, convs = br.call("GET", "/api/conversations")
    b_titles = [c["title"] for c in convs] if isinstance(convs, list) else []
    b_ids = [c["id"] for c in convs] if isinstance(convs, list) else []
    record("bob sees NONE of alice's conversations",
           st == 200 and A_TITLE not in b_titles and A_CONV not in b_ids,
           f"bob has {len(b_titles)}, none of them alice's")

    st, body = br.call("GET", f"/api/conversations/{A_CONV}")
    record("bob cannot open alice's conversation by id", st == 404, f"HTTP {st}")

    st, body = br.call("DELETE", f"/api/conversations/{A_CONV}")
    record("bob cannot delete alice's conversation", st == 404, f"HTTP {st}")

    st, bkeys = br.call("GET", "/api/keys")
    b_prefixes = [k["prefix"] for k in bkeys] if isinstance(bkeys, list) else []
    record("bob sees none of alice's keys",
           st == 200 and not (set(b_prefixes) & set(a_prefixes)),
           f"bob has {len(b_prefixes)}, no overlap with alice's")

    st, b_usage = br.call("GET", "/api/usage")
    b_req = sum(r.get("requests", 0) for r in b_usage) if isinstance(b_usage, list) else -1
    record("bob's usage is his own, not alice's", st == 200 and b_req < a_requests,
           f"bob {b_req} request(s) vs alice {a_requests}")

    st, _ = br.call("PUT", f"/api/conversations/{B_CONV}", {
        "title": B_TITLE,
        "messages": [{"role": "user", "content": "bob question"}],
    })
    record("bob saves his own conversation", st == 200, f"HTTP {st}")

    br.logout()
    print(f"\n{args.a} signs back in")
    br.login(args.a, args.a_password)

    st, convs = br.call("GET", "/api/conversations")
    titles = [c["title"] for c in convs] if isinstance(convs, list) else []
    record("alice's conversation survived", A_TITLE in titles,
           f"{len(titles)} conversation(s)")
    record("alice does NOT see bob's conversation", B_TITLE not in titles)

    st, conv = br.call("GET", f"/api/conversations/{A_CONV}")
    ok = isinstance(conv, dict) and len(conv.get("messages", [])) == 2
    record("alice's messages came back intact", ok,
           f"{len(conv.get('messages', [])) if isinstance(conv, dict) else 0} message(s)")

    st, akeys2 = br.call("GET", "/api/keys")
    record("alice still sees only her keys", isinstance(akeys2, list) and len(akeys2) == a_keys,
           f"{len(akeys2) if isinstance(akeys2, list) else 0} key(s)")

    # --- key expiry -------------------------------------------------------
    print("\nAuto-minted key expiry")
    minted = [k for k in akeys2 if isinstance(k, dict) and k.get("expires_at")]
    record("session key carries an expires_at", bool(minted),
           minted[0]["expires_at"] if minted else "no key has one")

    failed = [n for ok, n, _ in results if not ok]
    print(f"\n{len(results) - len(failed)} passed, {len(failed)} failed\n")
    if failed:
        for n in failed:
            print(f"  - {n}")
        return 1
    print("Two users. One browser. Fully isolated.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
