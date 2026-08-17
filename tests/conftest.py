"""Shared fixtures — clear all registries and the event bus between tests."""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from openjarvis.core.config import GpuInfo, HardwareInfo
from openjarvis.core.events import EventBus, reset_event_bus
from openjarvis.core.registry import (
    AgentRegistry,
    BenchmarkRegistry,
    ChannelRegistry,
    CompressionRegistry,
    ConnectorRegistry,
    EngineRegistry,
    FactStoreRegistry,
    MemoryRegistry,
    MinerRegistry,
    ModelRegistry,
    RouterPolicyRegistry,
    SkillRegistry,
    SpeechRegistry,
    ToolRegistry,
    TTSRegistry,
)


@pytest.fixture(autouse=True)
def _no_update_check(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never let the CLI's PyPI update-check nag run during tests.

    ``check_for_updates`` writes its banner to stderr, which ``CliRunner``
    merges into ``result.output`` — polluting JSON/CSV output of any test
    that invokes a CLI command. It already self-disables when ``CI`` is
    set, but that only helps in CI; locally (e.g. a dev with a stale
    version-check cache and network access) it fires for real.
    """
    monkeypatch.setenv("OPENJARVIS_NO_UPDATE_CHECK", "1")


@pytest.fixture(autouse=True)
def _deterministic_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    """Render CLI output the same way regardless of the developer's terminal.

    Rich decides whether to emit ANSI from the *ambient* environment, and
    ``$FORCE_COLOR`` overrides its "not a terminal, so no colour" default.
    Several terminals and agent harnesses export it.  When they do, Rich styles
    output even under ``CliRunner``, and every substring assertion against
    ``result.output`` is really being made against colour-interleaved text —
    ``'[dashboard]'`` becomes ``'\\x1b[1m[dashboard]\\x1b[0m'`` and the ``in``
    check fails on content that is perfectly correct.

    ``$COLUMNS`` is pinned for the same reason: Rich wraps to the detected
    width, so a developer with a wide or narrow terminal gets different line
    breaks and different results from the same code.  80 is Rich's own default,
    so this pins tests to what CI already sees rather than inventing a width.

    This normalises *presentation*, not behaviour — no assertion is weakened,
    and nothing is silenced.  Tests that care about colour can set their own
    console explicitly.
    """
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("TERM", "dumb")
    monkeypatch.setenv("COLUMNS", "80")


#: Markers whose tests are *meant* to talk to something real, and therefore
#: need the ambient environment left exactly as the operator configured it.
_LIVE_MARKERS = frozenset(
    {
        "live",
        "live_channel",
        "live_external",
        "live_claude",
        "cloud",
        "modal",
        "hub",
    }
)


def _live_target_env_vars() -> frozenset[str]:
    """Every environment variable that points JARVIS at a real application.

    Derived from ``ENV_ALIASES`` rather than hand-listed, because the whole
    point of that table is that new spellings get added to it.  A hard-coded
    copy here would silently stop covering the newest alias — which is the
    exact failure this fixture exists to prevent, reintroduced one level up.
    """
    from openjarvis.reliability.target import ENV_ALIASES

    names = {name for aliases in ENV_ALIASES.values() for name in aliases}
    # Credential variables are not in ENV_ALIASES (it holds identifiers only),
    # but a unit test must not be able to reach a real integration either.
    names.update(
        {
            "VERCEL_READONLY_TOKEN",
            "SUPABASE_READONLY_TOKEN",
            "GITHUB_READONLY_TOKEN",
            "GITHUB_TOKEN",
            "TELEGRAM_BOT_TOKEN",
        }
    )
    return frozenset(names)


@pytest.fixture(autouse=True)
def _isolate_from_live_target_env(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unit tests must not see the machine's real monitoring configuration.

    On a machine actually configured to monitor production — which is the
    machine a JARVIS developer runs — ``$TARGET_REPOSITORY``,
    ``$TARGET_PRODUCTION_URL``, ``$VERCEL_PROJECT`` and ``$SUPABASE_PROJECT_REF``
    are all exported.  ``resolve_target`` reads canonical names *before*
    aliases, so a test that monkeypatches the alias ``TARGET_REPO`` was quietly
    losing to the ambient canonical name and asserting against the real
    repository.  Fourteen tests failed that way here, and they fail *only* on a
    correctly configured machine — the configuration is the trigger, so CI is
    green and the developer who can actually run JARVIS for real is the one who
    sees a red suite.

    Clearing the variables is the fix rather than teaching each test to
    overwrite them: a test should not have to know which spellings exist to be
    deterministic, and the next alias added to ``ENV_ALIASES`` would otherwise
    reintroduce the bug in tests nobody thought to touch.

    Tests marked as needing real credentials are exempt — for them the ambient
    environment *is* the input.
    """
    if _LIVE_MARKERS & {marker.name for marker in request.node.iter_markers()}:
        return
    for name in _live_target_env_vars():
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def _clean_registries() -> None:
    """Ensure each test starts with empty registries and a fresh event bus."""
    ModelRegistry.clear()
    EngineRegistry.clear()
    MemoryRegistry.clear()
    FactStoreRegistry.clear()
    MinerRegistry.clear()
    AgentRegistry.clear()
    ToolRegistry.clear()
    RouterPolicyRegistry.clear()
    BenchmarkRegistry.clear()
    ChannelRegistry.clear()
    SpeechRegistry.clear()
    CompressionRegistry.clear()
    ConnectorRegistry.clear()
    TTSRegistry.clear()
    SkillRegistry.clear()
    reset_event_bus()


# ---------------------------------------------------------------------------
# Hardware fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def nvidia_gpu() -> GpuInfo:
    """NVIDIA A100 GPU fixture."""
    return GpuInfo(vendor="nvidia", name="NVIDIA A100-SXM4-80GB", vram_gb=80.0, count=1)


@pytest.fixture
def nvidia_consumer_gpu() -> GpuInfo:
    """NVIDIA consumer GPU fixture."""
    return GpuInfo(
        vendor="nvidia",
        name="NVIDIA GeForce RTX 4090",
        vram_gb=24.0,
        count=1,
    )


@pytest.fixture
def nvidia_multi_gpu() -> GpuInfo:
    """NVIDIA multi-GPU fixture."""
    return GpuInfo(vendor="nvidia", name="NVIDIA H100", vram_gb=80.0, count=4)


@pytest.fixture
def amd_gpu() -> GpuInfo:
    """AMD MI300X GPU fixture."""
    return GpuInfo(vendor="amd", name="AMD Instinct MI300X", vram_gb=192.0, count=1)


@pytest.fixture
def apple_gpu() -> GpuInfo:
    """Apple Silicon GPU fixture."""
    return GpuInfo(vendor="apple", name="Apple M4 Max", vram_gb=128.0, count=1)


@pytest.fixture
def hardware_nvidia(nvidia_gpu: GpuInfo) -> HardwareInfo:
    """Full NVIDIA hardware profile."""
    return HardwareInfo(
        platform="linux",
        cpu_brand="AMD EPYC 7763",
        cpu_count=64,
        ram_gb=512.0,
        gpu=nvidia_gpu,
    )


@pytest.fixture
def hardware_nvidia_consumer(nvidia_consumer_gpu: GpuInfo) -> HardwareInfo:
    """Consumer NVIDIA hardware profile."""
    return HardwareInfo(
        platform="linux",
        cpu_brand="Intel Core i9-14900K",
        cpu_count=24,
        ram_gb=64.0,
        gpu=nvidia_consumer_gpu,
    )


@pytest.fixture
def hardware_amd(amd_gpu: GpuInfo) -> HardwareInfo:
    """Full AMD hardware profile."""
    return HardwareInfo(
        platform="linux",
        cpu_brand="AMD EPYC 9654",
        cpu_count=96,
        ram_gb=768.0,
        gpu=amd_gpu,
    )


@pytest.fixture
def hardware_apple(apple_gpu: GpuInfo) -> HardwareInfo:
    """Apple Silicon hardware profile."""
    return HardwareInfo(
        platform="darwin",
        cpu_brand="Apple M4 Max",
        cpu_count=16,
        ram_gb=128.0,
        gpu=apple_gpu,
    )


@pytest.fixture
def hardware_cpu_only() -> HardwareInfo:
    """CPU-only hardware profile (no GPU)."""
    return HardwareInfo(
        platform="linux",
        cpu_brand="Intel Xeon E5-2686 v4",
        cpu_count=8,
        ram_gb=32.0,
        gpu=None,
    )


# ---------------------------------------------------------------------------
# Engine availability fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def has_ollama() -> bool:
    """Check if Ollama is running locally."""
    try:
        import httpx

        resp = httpx.get("http://localhost:11434/api/tags", timeout=2.0)
        return resp.status_code == 200
    except Exception:
        return False


@pytest.fixture
def has_vllm() -> bool:
    """Check if vLLM is running locally."""
    try:
        import httpx

        resp = httpx.get("http://localhost:8000/v1/models", timeout=2.0)
        return resp.status_code == 200
    except Exception:
        return False


@pytest.fixture
def has_llamacpp() -> bool:
    """Check if llama.cpp server is running locally."""
    try:
        import httpx

        resp = httpx.get("http://localhost:8080/v1/models", timeout=2.0)
        return resp.status_code == 200
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Cloud API key fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def has_openai_key() -> bool:
    """Check if OPENAI_API_KEY is set."""
    return bool(os.environ.get("OPENAI_API_KEY"))


@pytest.fixture
def has_anthropic_key() -> bool:
    """Check if ANTHROPIC_API_KEY is set."""
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


@pytest.fixture
def has_gemini_key() -> bool:
    """Check if GEMINI_API_KEY or GOOGLE_API_KEY is set."""
    return bool(os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"))


# ---------------------------------------------------------------------------
# Mock engine factory
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_engine():
    """Factory for mock InferenceEngine instances."""

    def _factory(
        engine_id: str = "mock",
        model_response: str = "Hello!",
        tool_calls: list | None = None,
        models: list[str] | None = None,
    ) -> MagicMock:
        engine = MagicMock()
        engine.engine_id = engine_id
        engine.health.return_value = True
        engine.list_models.return_value = models or ["test-model"]

        result = {
            "content": model_response,
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            "model": "test-model",
            "finish_reason": "stop",
        }
        if tool_calls:
            result["tool_calls"] = tool_calls
            result["finish_reason"] = "tool_calls"
        engine.generate.return_value = result
        return engine

    return _factory


@pytest.fixture
def event_bus() -> EventBus:
    """Fresh EventBus with history recording enabled."""
    return EventBus(record_history=True)


# ---------------------------------------------------------------------------
# Mining sidecar fixtures (shared across tests/mining/ and tests/engine/)
# ---------------------------------------------------------------------------


@pytest.fixture
def sample_sidecar_payload() -> dict:
    """A valid vllm-pearl sidecar payload with all expected fields."""
    return {
        "provider": "vllm-pearl",
        "vllm_endpoint": "http://127.0.0.1:8000/v1",
        "model": "pearl-ai/Llama-3.3-70B-Instruct-pearl",
        "gateway_url": "http://127.0.0.1:8337",
        "gateway_metrics_url": "http://127.0.0.1:8339",
        "container_id": "abc123def456",
        "wallet_address": "prl1qexampleaddress",
        "started_at": 1714867200,
    }


@pytest.fixture
def sidecar_path(tmp_path: Path) -> Path:
    """Path to a (not-yet-written) mining sidecar JSON file."""
    return tmp_path / "mining.json"


@pytest.fixture
def written_sidecar(sidecar_path: Path, sample_sidecar_payload: dict) -> Path:
    """A written mining sidecar JSON file; returns the path."""
    sidecar_path.write_text(json.dumps(sample_sidecar_payload))
    return sidecar_path
