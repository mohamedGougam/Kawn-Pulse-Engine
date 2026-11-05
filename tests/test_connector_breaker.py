import asyncio

import pytest

from app.config import settings
from app.services import connector_health
from app.services.aggregation_service import AggregationService
from app.utils.http_errors import ErrorInfo


class _Boom:
    calls = 0

    async def enabled(self):
        return True

    async def fetch(self, topic, *, limit, language=None):
        _Boom.calls += 1
        raise ValueError("html instead of json")


@pytest.fixture(autouse=True)
def _live(monkeypatch):
    monkeypatch.setattr(settings, "enable_mock_data", False)
    connector_health.reset()
    _Boom.calls = 0
    yield
    connector_health.reset()


async def test_breaker_skips_source_after_repeated_failures():
    svc = AggregationService()
    for _ in range(settings.connector_breaker_threshold):
        assert await svc._safe_fetch(_Boom(), "intel", 10, fallback_source="Hashnode") == []
    assert _Boom.calls == settings.connector_breaker_threshold
    assert connector_health.breaker_open_for("Hashnode") is not None

    # Open breaker: no further calls reach the connector.
    assert await svc._safe_fetch(_Boom(), "intel", 10, fallback_source="Hashnode") == []
    assert _Boom.calls == settings.connector_breaker_threshold


async def test_breaker_probes_again_after_cooldown(monkeypatch):
    svc = AggregationService()
    for _ in range(settings.connector_breaker_threshold):
        await svc._safe_fetch(_Boom(), "intel", 10, fallback_source="Hashnode")
    monkeypatch.setattr(settings, "connector_breaker_cooldown_seconds", 0.0)
    assert connector_health.breaker_open_for("Hashnode") is None
    await svc._safe_fetch(_Boom(), "intel", 10, fallback_source="Hashnode")
    assert _Boom.calls == settings.connector_breaker_threshold + 1


def test_success_closes_breaker():
    for _ in range(settings.connector_breaker_threshold):
        connector_health.record_failure("X", ErrorInfo("error", "x"), 0.0)
    assert connector_health.breaker_open_for("X") is not None
    connector_health.record_success("X", 3, 0.0)
    assert connector_health.breaker_open_for("X") is None
