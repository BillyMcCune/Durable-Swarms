"""JSON client shared by the CLI and worker daemon."""

import json
from urllib.error import HTTPError
from urllib.request import Request, urlopen


class RemoteError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def request(url, path, body=None, timeout=5):
    data = None if body is None else json.dumps(body, allow_nan=False).encode()
    req = Request(url.rstrip("/") + path, data=data, headers={"Content-Type": "application/json"})
    try:
        with urlopen(req, timeout=timeout) as response:
            return json.load(response)
    except HTTPError as exc:
        try:
            message = json.load(exc).get("error", str(exc))
        except (ValueError, AttributeError):
            message = str(exc)
        raise RemoteError(exc.code, message) from exc
