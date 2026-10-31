"""Knowledge Hub client helper (kh-helper).

The helper is the only thing on the client that holds KH credentials. Assistants ask it to make a call
and get back the JSON answer; they never see a password, refresh token or access token.
"""
import time

# The MCP startup deadline counts from this moment.
STARTED = time.monotonic()

__version__ = "1.19.3"
