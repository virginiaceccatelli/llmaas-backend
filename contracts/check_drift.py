"""Assert the two tiers pin the same versions of the packages they share.

Both requirements.txt files carry a comment saying they are "pinned to the same
versions, so the two tiers cannot drift". This is the thing that makes that
true. It matters because the frontend and the broker exchange JWTs (PyJWT),
JSON models (pydantic) and an SSE stream (httpx/fastapi) — a version skew in
any of those is a bug that only shows up between the tiers, which is the
hardest place to see it.

Stdlib only, so CI can run it before installing anything.

    python contracts/check_drift.py

Exits 1 and prints every mismatch. Packages present in only one file are fine
and ignored — asyncpg belongs to the broker alone.
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

FILES = {
    "broker": ROOT / "broker" / "requirements.txt",
    "frontend": ROOT / "frontend" / "requirements.txt",
}

# "PyJWT[crypto]==2.10.1" -> ("pyjwt", "2.10.1"). Extras are stripped: the
# broker needs PyJWT[crypto] for OIDC's RS256 and the frontend does not, but it
# is the same package and the same version has to hold.
PIN = re.compile(r"^([A-Za-z0-9._-]+)(?:\[[^\]]*\])?==([^\s;#]+)")


def parse(path: Path) -> dict[str, str]:
    if not path.exists():
        sys.exit(
            f"{path} not found.\n"
            "If this is frontend/requirements.txt, the submodule is not "
            "checked out. Run:\n"
            "    git submodule update --init --recursive"
        )
    pins: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = PIN.match(line)
        if m:
            pins[m.group(1).lower()] = m.group(2)
    return pins


def main() -> int:
    pins = {tier: parse(path) for tier, path in FILES.items()}
    shared = sorted(set(pins["broker"]) & set(pins["frontend"]))

    if not shared:
        print("no shared packages found — is one requirements.txt empty?")
        return 1

    drift = [
        (pkg, pins["broker"][pkg], pins["frontend"][pkg])
        for pkg in shared
        if pins["broker"][pkg] != pins["frontend"][pkg]
    ]

    if drift:
        print(f"{len(drift)} shared package(s) have drifted:\n")
        for pkg, b, f in drift:
            print(f"  {pkg:<20} broker {b:<12} frontend {f}")
        print(
            "\nPick one version, set it in both files, and commit the frontend "
            "change\nfirst — then bump the submodule pointer here so the pair "
            "is recorded."
        )
        return 1

    print(f"ok - {len(shared)} shared package(s) agree: {', '.join(shared)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
