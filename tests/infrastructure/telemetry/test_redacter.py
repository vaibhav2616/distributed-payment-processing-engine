import pytest
import structlog
from io import StringIO
from infrastructure.telemetry.redacter import mask_sensitive_data
import json

def test_mask_sensitive_data():
    # Capture log output
    stream = StringIO()
    
    structlog.configure(
        processors=[
            mask_sensitive_data,
            structlog.processors.JSONRenderer(),
        ],
        logger_factory=structlog.PrintLoggerFactory(stream),
    )
    
    logger = structlog.get_logger("test_logger")
    
    # Emit log with sensitive fields
    sensitive_payload = {
        "user_id": 123,
        "source_token": "tok_123456789",
        "card_number": "4111222233334444",
        "cvv": "123",
        "pan": "4111222233334444",
        "password": "secret_password",
        "authorization": "Bearer token",
        "nested": {
            "source_token": "nested_tok",
            "safe_field": "safe_value"
        },
        "list_of_dicts": [
            {"card_number": "1111222233334444"}
        ]
    }
    
    logger.info("payment_attempt", **sensitive_payload)
    
    log_output = stream.getvalue()
    log_dict = json.loads(log_output)
    
    # Verify redacted fields
    assert log_dict["source_token"] == "[REDACTED]"
    assert log_dict["card_number"] == "[REDACTED]"
    assert log_dict["cvv"] == "[REDACTED]"
    assert log_dict["pan"] == "[REDACTED]"
    assert log_dict["password"] == "[REDACTED]"
    assert log_dict["authorization"] == "[REDACTED]"
    assert log_dict["nested"]["source_token"] == "[REDACTED]"
    assert log_dict["list_of_dicts"][0]["card_number"] == "[REDACTED]"
    
    # Verify safe fields remain intact
    assert log_dict["user_id"] == 123
    assert log_dict["nested"]["safe_field"] == "safe_value"
