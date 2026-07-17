from __future__ import annotations

from src.utils.security import redact_sensitive_data


def test_report_redaction_masks_sensitive_values() -> None:
    data = {
        "settings": {
            "mt5": {
                "login": 10011660287,
                "password": "secret",
                "server": "MetaQuotes-Demo",
                "path": "C:\\Users\\adem2\\Downloads\\tradam",
            }
        },
        "env": {"NEWSAPI_API_KEY": "abc"},
    }
    redacted = redact_sensitive_data(data)
    assert redacted["settings"]["mt5"]["login"] == "***0287"
    assert redacted["settings"]["mt5"]["password"] == "***"
    assert redacted["env"]["NEWSAPI_API_KEY"] == "***"
    assert "adem2" not in str(redacted)

