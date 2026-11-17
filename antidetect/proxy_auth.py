"""Bound proxy authentication retries to one attempt per CDP request."""
from collections import OrderedDict


class ProxyAuthAttempts:
    def __init__(self, capacity=4096):
        self.capacity = capacity
        self.attempted = OrderedDict()

    def response(self, session_id, request_id, challenge, credentials):
        if not request_id or not credentials or challenge.get("source") != "Proxy":
            return {"response": "CancelAuth"}
        key = (session_id, request_id)
        if key in self.attempted:
            return {"response": "CancelAuth"}
        # A bounded per-session controller: a server repeatedly rejecting the
        # same request must not create an endless challenge/credential loop.
        if len(self.attempted) >= self.capacity:
            return {"response": "CancelAuth"}
        self.attempted[key] = None
        return {"response": "ProvideCredentials", "username": credentials[0], "password": credentials[1]}

    def discard_session(self, session_id):
        for key in list(self.attempted):
            if key[0] == session_id:
                self.attempted.pop(key, None)
