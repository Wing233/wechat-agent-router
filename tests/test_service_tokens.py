"""Purpose isolation for service credentials."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import service_tokens


def test_scoped_values_override_legacy_per_scope():
    resolved = service_tokens.resolve({
        "service_token": "legacy",
        "service_tokens": {"inference": "inf", "notify": "ntf"},
    })
    assert resolved == {"inference": "inf", "notify": "ntf", "admin": "legacy"}


def test_single_token_install_keeps_working_everywhere():
    resolved = service_tokens.resolve({"service_token": "legacy"})
    assert set(resolved.values()) == {"legacy"}


def test_match_requires_both_sides_and_exact_value():
    assert service_tokens.match("abc", "abc") is True
    assert service_tokens.match("abc", "abd") is False
    assert service_tokens.match("", "") is False
    assert service_tokens.match("abc", "") is False
