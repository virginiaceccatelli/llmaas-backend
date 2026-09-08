"""
Tests for the model registry and upstream error handling.

The regression these exist for: a `${VAR}` that expanded to nothing used to
leave the broker running with an EMPTY upstream API key. It started fine, then
every chat request went out unauthenticated and the upstream answered with an
HTML login page, which surfaced to the customer as an unreadable error.
"""
import os

import pytest

from app import registry
from app.config import load_env_file
from app.upstream import describe_error

DEV = "./serving/models.dev.yaml"
MOCK = "./serving/models.mock.yaml"
PROD = "./serving/models.prod.yaml"


@pytest.fixture
def clean_env(monkeypatch):
    for var in ("HF_TOKEN", "VLLM_API_KEY", "VLLM_QWEN_HOST"):
        monkeypatch.delenv(var, raising=False)
    return monkeypatch


# --- the actual regression -------------------------------------------------
def test_unset_token_refuses_to_start(clean_env):
    with pytest.raises(ValueError, match="HF_TOKEN"):
        registry.Registry.from_file(DEV)


def test_empty_token_refuses_to_start(clean_env):
    """An empty value is as broken as a missing one, and easier to end up with."""
    clean_env.setenv("HF_TOKEN", "")
    with pytest.raises(ValueError, match="HF_TOKEN"):
        registry.Registry.from_file(DEV)


def test_prod_reports_every_missing_var_at_once(clean_env):
    with pytest.raises(ValueError) as e:
        registry.Registry.from_file(PROD)
    assert "VLLM_API_KEY" in str(e.value)
    assert "VLLM_QWEN_HOST" in str(e.value)


def test_token_present_loads_cleanly(clean_env):
    clean_env.setenv("HF_TOKEN", "hf_dummy")
    reg = registry.Registry.from_file(DEV)
    route = reg.get("qwen-instruct")
    assert route.api_key == "hf_dummy"
    assert route.kind == "openai"      # no cache_salt to a non-vLLM upstream


def test_mock_registry_needs_no_env(clean_env):
    """The offline registry must work with a completely empty environment."""
    reg = registry.Registry.from_file(MOCK)
    assert reg.get("qwen-instruct").api_key is None
    assert reg.get("qwen-instruct").kind == "vllm"


def test_missing_file_message_is_actionable():
    with pytest.raises(FileNotFoundError, match="MODELS_FILE"):
        registry.Registry.from_file("./serving/does-not-exist.yaml")


# --- .env -> os.environ ----------------------------------------------------
def test_load_env_file_populates_environ(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text('HF_TOKEN=hf_from_file\nQUOTED="v"\n# comment\nBLANK=\n')
    monkeypatch.delenv("HF_TOKEN", raising=False)
    loaded = load_env_file(str(env))
    assert "HF_TOKEN" in loaded
    assert os.environ["HF_TOKEN"] == "hf_from_file"
    assert os.environ["QUOTED"] == "v"       # surrounding quotes stripped
    for k in ("HF_TOKEN", "QUOTED", "BLANK"):
        os.environ.pop(k, None)


def test_real_environment_wins_over_env_file(tmp_path, monkeypatch):
    """Docker/Kubernetes set real env vars; .env must never override them."""
    env = tmp_path / ".env"
    env.write_text("HF_TOKEN=from_file\n")
    monkeypatch.setenv("HF_TOKEN", "from_real_env")
    load_env_file(str(env))
    assert os.environ["HF_TOKEN"] == "from_real_env"


def test_missing_env_file_is_not_an_error():
    assert load_env_file("./nope/.env") == []


# --- upstream error summarisation -----------------------------------------
def test_html_error_page_is_not_echoed_back():
    msg = describe_error(403, "<!DOCTYPE html><html><head><title>Blocked</title>")
    assert "<" not in msg and "DOCTYPE" not in msg


def test_html_401_explains_the_credential_problem():
    msg = describe_error(401, "<!DOCTYPE html><html>login</html>")
    assert "credential" in msg


def test_json_error_message_is_surfaced():
    body = '{"error":{"message":"You have depleted your monthly credits"}}'
    assert "depleted your monthly credits" in describe_error(402, body)


def test_plain_string_error_is_surfaced():
    assert "boom" in describe_error(500, '{"error":"boom"}')


def test_non_json_non_html_body_is_truncated():
    msg = describe_error(500, "x" * 5000)
    assert len(msg) < 300
