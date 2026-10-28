"""JSON client shared by the CLI and worker daemon."""

import json
from urllib.error import HTTPError
from urllib.request import Request, urlopen


class RemoteError(Exception):
    # Preserve the server's HTTP status alongside its message for client recovery logic.
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


# Send GET when body is absent or POST with a JSON body, then decode the response.
# HTTP failures become RemoteError; connection errors and timeouts propagate to the
# caller, which decides whether retrying the operation is appropriate.
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
