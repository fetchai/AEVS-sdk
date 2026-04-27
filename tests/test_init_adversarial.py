"""Adversarial tests for aevs/__init__.py proxy functions.

Targets uncovered lines: get_reference_id, get_reference_ids,
clear_reference_ids proxy functions that import from _api.
"""

from __future__ import annotations

import aevs
import aevs._api as _api_mod


class TestInitProxies:
    def test_get_reference_id_proxies_to_api(self):
        _api_mod._reference_registry.clear()
        _api_mod._reference_deque.clear()
        _api_mod._record_reference(1, "tool", "ref-abc", "run-xyz", "tc-999")

        assert aevs.get_reference_id("run-xyz") == "ref-abc"
        assert aevs.get_reference_id("tc-999") == "ref-abc"
        assert aevs.get_reference_id("nonexistent") is None

    def test_get_reference_ids_proxies_to_api(self):
        _api_mod._reference_deque.clear()
        _api_mod._reference_registry.clear()
        _api_mod._record_reference(1, "t1", "ref-1", "r1", None)
        _api_mod._record_reference(2, "t2", "ref-2", "r2", None)

        ids = aevs.get_reference_ids()
        assert len(ids) == 2
        assert ids[0]["reference_id"] == "ref-1"

    def test_get_reference_ids_with_clear_proxies(self):
        _api_mod._reference_deque.clear()
        _api_mod._reference_registry.clear()
        _api_mod._record_reference(1, "t", "ref-1", "r1", None)

        ids = aevs.get_reference_ids(clear=True)
        assert len(ids) == 1
        assert len(_api_mod._reference_deque) == 0

    def test_clear_reference_ids_proxies_to_api(self):
        _api_mod._reference_deque.clear()
        _api_mod._reference_registry.clear()
        _api_mod._record_reference(1, "t", "ref-1", "r1", None)

        aevs.clear_reference_ids()
        assert len(_api_mod._reference_deque) == 0
        assert len(_api_mod._reference_registry) == 0

    def test_is_healthy_proxies_to_api_healthy(self):
        _api_mod._consecutive_store_failures = 0
        assert aevs.is_healthy() is True

    def test_is_healthy_proxies_to_api_unhealthy(self):
        _api_mod._consecutive_store_failures = 5
        assert aevs.is_healthy() is False

    def test_is_healthy_respects_threshold(self):
        _api_mod._consecutive_store_failures = 2
        assert aevs.is_healthy(threshold=3) is True
        assert aevs.is_healthy(threshold=2) is False

    def teardown_method(self, _method):
        _api_mod._consecutive_store_failures = 0
