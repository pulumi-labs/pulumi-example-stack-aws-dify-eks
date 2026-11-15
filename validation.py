"""Small secret-format boundary matching the pinned Agent Stub implementation."""
import base64
import binascii
import re


def validate_agent_server_secret(value: str) -> str:
    message = "agentServerSecretKey must be canonical unpadded base64url encoding of exactly 32 bytes"
    if not re.fullmatch(r"[A-Za-z0-9_-]{43}", value):
        raise ValueError(message)
    try:
        decoded = base64.b64decode(value + "=", altchars=b"-_", validate=True)
    except (binascii.Error, ValueError):
        raise ValueError(message) from None
    if len(decoded) != 32 or base64.urlsafe_b64encode(decoded).rstrip(b"=").decode("ascii") != value:
        raise ValueError(message)
    return value
