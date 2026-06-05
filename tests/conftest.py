import pytest

from aevs.config import reset_config

# A valid API key for testing (key_id="testkey", secret=32 bytes of hex)
TEST_API_KEY = "aevs_sk_testkey_" + "ab" * 32
TEST_KEY_ID = "testkey"
TEST_KEY_SECRET = bytes.fromhex("ab" * 32)

# A valid v4 UUID for agent_id testing
TEST_AGENT_ID = "12345678-1234-4234-8234-123456789abc"

# Single source of truth for the mock backend.
#  - tests pass TEST_BASE_URL to `configure(base_url=...)`
#  - respx routes use TEST_RECEIPTS_URL so the mock and the client agree
TEST_BASE_URL = "http://localhost:8000/v1"
TEST_RECEIPTS_URL = f"{TEST_BASE_URL}/receipts"
TEST_RECEIPTS_BATCH_URL = f"{TEST_BASE_URL}/receipts/batch"


@pytest.fixture(autouse=True)
def _clean_config():
    """Reset global config before each test."""
    reset_config()
    yield
    reset_config()
