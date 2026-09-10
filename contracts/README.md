# contracts/ — what crosses the private network

The frontend BFF and the broker are separate repos, separate images and
separate VMs. They agree on a handful of things, and until now that agreement
lived in prose: a table in `docs/AUTH.md`, a comment in each `requirements.txt`
reading *"pinned to the same versions, so the two tiers cannot drift"*.

A comment is not a mechanism. This directory is the mechanism.

| Contract | File | Checked by |
|---|---|---|
| Control-plane JWT claims | [control_token.md](control_token.md) | `frontend/scripts/smoke.py` in `hs256` mode |
| Shared dependency versions | this dir's `check_drift.py` | CI, on every push |
| Broker HTTP surface | `openapi.broker.json` | CI, regenerated and diffed |
| Public model names | `serving/models.*.yaml` | CI, via the frontend's `/api/models` check |

## Running the checks locally

```powershell
python contracts\check_drift.py          # dependency pins across both repos
python contracts\dump_openapi.py         # regenerate openapi.broker.json
```

`check_drift.py` is stdlib-only and needs nothing installed. `dump_openapi.py`
needs the broker's dependencies, so run it inside `.venv`.

## Why the dependency check compares rather than shares

The obvious design is one `requirements.shared.txt` that both repos `-r`. That
does not work here: the frontend's Docker build context is `./frontend`, so a
`-r ../contracts/requirements.shared.txt` is outside the context and the build
fails. Comparing the two files after the fact gives the same guarantee with no
build changes.

## Adding a contract

If you find yourself writing "these two must match" in a comment, it belongs
here with a check next to it.
