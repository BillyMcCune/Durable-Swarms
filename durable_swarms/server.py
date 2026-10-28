"""Small HTTP transport for the orchestrator, on a trusted private network."""

import fcntl
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .engine import APIError, Engine
from .store import Store


# Acquire exclusive database ownership, rebuild workload state, and run the HTTP
# server with its scheduler thread. Shut down the thread and release the lock on exit.
# Inputs:
#   config: Dictionary of database path, HTTP host/port, VM capacities, and timeouts.
# Output: None after shutdown; serves HTTP requests until interrupted and exits
#         with SystemExit if another orchestrator already holds the database lock.
def serve(config):
    path = config.get("database", "swarms.sqlite3")
    # Prevent two independently configured orchestrators from sharing the same database.
    lockfile = open(path + ".lock", "a")
    try:
        fcntl.flock(lockfile, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lockfile.close()
        raise SystemExit("another orchestrator is using this database")
    store = Store(path)
    store.rebuild()
    engine = Engine(store, config)
    stop = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        # Keep routine HTTP access logs at debug level to avoid noisy worker polling.
        # Inputs:
        #   self: HTTP request handler instance.
        #   fmt: Logging format string.
        #   *args: Values substituted into the format string.
        # Output: None; emits a debug log entry.
        def log_message(self, fmt, *args):
            logging.debug(fmt, *args)

        # Serialize a JSON response with its status and byte length. A disconnected
        # client does not undo a request that has already committed successfully.
        # Inputs:
        #   self: HTTP handler with the client's writable response stream.
        #   code: HTTP response status integer.
        #   body: JSON-serializable response object.
        # Output: None; writes HTTP headers and JSON bytes to the client connection.
        def respond(self, code, body):
            data = json.dumps(body, allow_nan=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass

        # Dispatch read endpoints or JSON commands to the engine, translating known
        # API/validation errors into HTTP responses and unexpected failures into 503.
        # Inputs:
        #   self: HTTP handler containing the request method, path, headers, and body;
        #         engine and store are captured from serve.
        # Output: None; sends a JSON success or error response over HTTP.
        def handle_request(self):
            url = urlparse(self.path)
            query = parse_qs(url.query)
            try:
                if self.command == "GET":
                    if url.path == "/health":
                        result = {"ok": True}
                    elif url.path == "/status":
                        result = engine.status(query["task_id"][0])
                    elif url.path == "/events":
                        with store.transaction():
                            engine.task(query["task_id"][0])
                            result = store.events(query["task_id"][0], query.get("worker_id", [None])[0],
                                                  int(query.get("after", [0])[0]), min(500, max(1, int(query.get("limit", [100])[0]))))
                    elif url.path == "/vms":
                        with store.transaction():
                            result = store.all("vms")
                    else:
                        raise APIError("unknown endpoint", 404)
                else:
                    length = int(self.headers.get("Content-Length", 0))
                    if length < 1 or length > 2_000_000:
                        raise APIError("JSON request must be between 1 byte and 2 MB", 400)
                    body = json.loads(self.rfile.read(length))
                    if not isinstance(body, dict):
                        raise APIError("request must be an object", 400)
                    routes = {"/" + name: getattr(engine, name) for name in (
                        "submit", "register", "heartbeat", "poll", "start", "manage", "progress", "intent",
                        "effect", "checkpoint", "finish", "fail", "stop", "recreate")}
                    if url.path not in routes:
                        raise APIError("unknown endpoint", 404)
                    result = routes[url.path](body)
                self.respond(200, result)
            except APIError as exc:
                self.respond(exc.code, {"error": str(exc)})
            except (KeyError, ValueError, TypeError, OverflowError) as exc:
                self.respond(400, {"error": str(exc)})
            except Exception:
                logging.exception("request failed")
                self.respond(503, {"error": "orchestrator unavailable; retry using the same operation identity"})

        do_GET = handle_request
        do_POST = handle_request

    server = ThreadingHTTPServer((config.get("host", "127.0.0.1"), config.get("port", 8080)), Handler)
    server.daemon_threads = True

    # Run scheduling and failure detection periodically until shutdown is requested.
    # Log a failed tick and retry on the next interval rather than losing monitoring.
    # Inputs: No arguments; captures stop, config, and engine from serve.
    # Output: None when the stop event is set; runs scheduler ticks until then.
    def monitor():
        while not stop.wait(config.get("tick_seconds", 0.2)):
            try:
                engine.tick()
            except Exception:
                logging.exception("scheduler tick failed")

    thread = threading.Thread(target=monitor, daemon=True)
    thread.start()
    logging.info("orchestrator listening on %s:%s", *server.server_address)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        thread.join()
        server.server_close()
        lockfile.close()
