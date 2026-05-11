"""Adversarial tests for ReceiptBuilder — designed to break it.

Targets uncovered line: the `seq` property.
"""

from __future__ import annotations

from aevs.config import configure, get_config
from aevs.core.receipt import ReceiptBuilder
from tests.conftest import TEST_AGENT_ID, TEST_API_KEY

_TEST_SESSION_ID = "00000000-0000-4000-8000-000000000003"


class TestReceiptBuilderSeqProperty:
    def test_seq_starts_at_zero(self):
        configure(api_key=TEST_API_KEY, agent_id=TEST_AGENT_ID)
        builder = ReceiptBuilder(get_config(), session_id=_TEST_SESSION_ID)
        assert builder.seq == 0

    def test_seq_starts_at_custom_value(self):
        configure(api_key=TEST_API_KEY, agent_id=TEST_AGENT_ID)
        builder = ReceiptBuilder(
            get_config(), session_id=_TEST_SESSION_ID, start_seq=42
        )
        assert builder.seq == 42

    def test_seq_reflects_start_seq_not_builds(self):
        configure(api_key=TEST_API_KEY, agent_id=TEST_AGENT_ID)
        builder = ReceiptBuilder(
            get_config(), session_id=_TEST_SESSION_ID, start_seq=10
        )
        assert builder.seq == 10

    def test_session_id_property_exposed(self):
        configure(api_key=TEST_API_KEY, agent_id=TEST_AGENT_ID)
        builder = ReceiptBuilder(get_config(), session_id=_TEST_SESSION_ID)
        assert builder.session_id == _TEST_SESSION_ID
