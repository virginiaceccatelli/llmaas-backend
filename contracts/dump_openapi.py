"""Regenerate contracts/openapi.broker.json from the broker's own app.

The frontend BFF is a typed client of this surface: app/routers/keys.py reads
`created["api_key"]`, `["id"]`, `["prefix"]`; app/apikeys.py depends on the same
fields. Rename one of them in the broker and nothing fails until a user clicks
a button in a browser on another VM.

Committing the schema turns that into a diff in a pull request. CI runs this
and fails when the checked-in file is stale.

    python contracts/dump_openapi.py            # rewrite the file
    python contracts/dump_openapi.py --check    # fail if it would change

Needs the broker's dependencies, so run it inside .venv.
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "contracts" / "openapi.broker.json"

sys.path.insert(0, str(ROOT / "broker"))


def schema() -> str:
    # Importing app.main does NOT run the lifespan, so no database, Redis or
    # model registry is needed here — only the route definitions.
    from app.main import app  # noqa: PLC0415 - after the sys.path insert

    return json.dumps(app.openapi(), indent=2, sort_keys=True) + "\n"


def main() -> int:
    current = schema()

    if "--check" in sys.argv:
        if not OUT.exists():
            print(f"{OUT.name} is missing. Run: python contracts/dump_openapi.py")
            return 1
        if OUT.read_text(encoding="utf-8") != current:
            print(
                f"{OUT.name} is stale — the broker's HTTP surface changed.\n"
                "Run `python contracts/dump_openapi.py` and review the diff. If "
                "it touches\n/keys or /usage, the frontend BFF needs the matching "
                "change in the same\ncommit pair (see contracts/control_token.md)."
            )
            return 1
        print(f"ok - {OUT.name} matches the broker")
        return 0

    OUT.write_text(current, encoding="utf-8")
    print(f"wrote {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
