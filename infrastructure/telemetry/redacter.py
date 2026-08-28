import structlog
from typing import Any, Dict

SENSITIVE_KEYS = {"source_token", "card_number", "cvv", "pan", "password", "authorization"}

def mask_sensitive_data(logger: structlog.types.WrappedLogger, method_name: str, event_dict: structlog.types.EventDict) -> structlog.types.EventDict:
    """
    Deeply scan the event dictionary and redact any sensitive keys matching
    the blocklist before they are serialized.
    """
    def redact(data: Any) -> Any:
        if isinstance(data, dict):
            return {
                k: ("[REDACTED]" if k.lower() in SENSITIVE_KEYS else redact(v))
                for k, v in data.items()
            }
        elif isinstance(data, list):
            return [redact(item) for item in data]
        return data

    return redact(event_dict)
