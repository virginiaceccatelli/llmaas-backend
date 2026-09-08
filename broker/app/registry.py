"""
Model registry: the routing table of the whole platform.

A public model name (what the customer puts in `"model": ...`) maps to:
  - a base URL   -> which vLLM instance / GPU VM serves it
  - an upstream model id -> what that backend actually calls it
  - an optional API key  -> credential the broker uses to talk to that backend
  - a kind ("vllm" | "openai") -> enables vLLM-only features like cache_salt

Keeping this in a YAML file (not in code) means adding a model is a config
change plus a vLLM container, not a code deploy.
"""
import os
import re
from dataclasses import dataclass

import yaml

_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _expand_env(text: str) -> tuple[str, set[str]]:
    """Replace ${VAR} with its environment value.

    Returns the expanded text plus the names of any referenced variables that
    were not set, so the caller can refuse to start rather than silently
    running with an empty credential.

    Hand-rolled rather than os.path.expandvars, which is path-oriented and
    behaves differently on Windows and POSIX.
    """
    missing: set[str] = set()

    def sub(m: re.Match) -> str:
        name = m.group(1)
        value = os.environ.get(name)
        if value is None or value == "":
            missing.add(name)
            return ""
        return value

    return _ENV_REF.sub(sub, text), missing


@dataclass(frozen=True)
class ModelRoute:
    name: str            # public name, e.g. "qwen2.5-7b-instruct"
    base_url: str        # e.g. "http://10.0.0.21:8000/v1"
    upstream_model: str  # e.g. "Qwen/Qwen2.5-7B-Instruct"
    api_key: str | None  # bearer token for the upstream, if it needs one
    kind: str = "vllm"   # "vllm" | "openai"


class Registry:
    def __init__(self, routes: dict[str, ModelRoute]):
        self._routes = routes

    @classmethod
    def from_file(cls, path: str) -> "Registry":
        if not os.path.exists(path):
            raise FileNotFoundError(
                f"model registry not found at {path!r}. Inside Docker this is "
                f"mounted at /srv/models.yaml; running the broker directly, set "
                f"MODELS_FILE=./serving/models.dev.yaml in your .env "
                f"(see docs/WORKFLOW.md)."
            )
        with open(path, "r", encoding="utf-8") as fh:
            # ${VAR} in the YAML is filled from the environment, so secrets
            # (upstream API keys) never live in the config file itself.
            raw, missing = _expand_env(fh.read())
        if missing:
            # Without this the broker starts happily, then sends unauthenticated
            # requests upstream and gets back an HTML error page. Fail here.
            raise ValueError(
                f"{path} references environment variables that are unset or "
                f"empty: {', '.join(sorted(missing))}. Set them in .env "
                f"(see .env.example)."
            )
        doc = yaml.safe_load(raw) or {}

        routes: dict[str, ModelRoute] = {}
        for name, spec in (doc.get("models") or {}).items():
            base_url = (spec.get("base_url") or "").strip()
            # Catch a ${VAR} that expanded to nothing — otherwise the broker
            # starts happily and every request fails with a confusing error.
            if not re.match(r"^https?://[^/:\s]+", base_url):
                raise ValueError(
                    f"model {name!r}: base_url is {base_url!r} — is an "
                    f"environment variable referenced in {path} unset?"
                )
            api_key = (spec.get("api_key") or "").strip() or None
            routes[name] = ModelRoute(
                name=name,
                base_url=base_url.rstrip("/"),
                upstream_model=spec.get("upstream_model", name),
                api_key=api_key,
                kind=spec.get("kind", "vllm"),
            )
        if not routes:
            raise ValueError(f"no models defined in {path}")
        return cls(routes)

    def get(self, name: str) -> ModelRoute | None:
        return self._routes.get(name)

    def all(self) -> list[ModelRoute]:
        return list(self._routes.values())


_registry: Registry | None = None


def load(path: str) -> None:
    global _registry
    _registry = Registry.from_file(path)


def registry() -> Registry:
    if _registry is None:
        raise RuntimeError("model registry not loaded")
    return _registry

# EXTEND: per-model access control. Add an `allowed_plans: [free, pro]` field
# here and check it in routers/chat.py, so not every key can reach every model.
