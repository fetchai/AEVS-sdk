"""Adversarial tests for ReceiptBuilder — designed to break it.

Targets uncovered line: the `seq` property.
"""

from __future__ import annotations

from aevs.config import configure, get_config
from aevs.core.receipt import ReceiptBuilder
from tests.conftest import TEST_API_KEY


class TestReceiptBuilderSeqProperty:
    def test_seq_starts_at_zero(self):
        configure(api_key=TEST_API_KEY)
        builder = ReceiptBuilder(get_config())
        assert builder.seq == 0

    def test_seq_starts_at_custom_value(self):
        configure(api_key=TEST_API_KEY)
        builder = ReceiptBuilder(get_config(), start_seq=42)
        assert builder.seq == 42

    def test_seq_reflects_start_seq_not_builds(self):
        configure(api_key=TEST_API_KEY)
        builder = ReceiptBuilder(get_config(), start_seq=10)
        assert builder.seq == 10
