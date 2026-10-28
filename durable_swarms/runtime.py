"""VM daemon and disposable workflow subprocesses.

Each worker is a separate OS process. Only the daemon holds VM capacity and
heartbeats; a dead subprocess cannot keep renewing its own worker lease.
"""

import json
import logging
import subprocess
import sys
import signal
import time
import uuid

from .client import RemoteError, request


def execute(url, job, vm_id, session, interval=0.5):
    worker = job["worker"]
    identity = {"task_id": job["task_id"], "worker_id": worker["id"], "attempt": worker["attempt"],
                "vm_id": vm_id, "session": session}
    leader = worker["parent_id"] is None

    def call(name, **args):
        return request(url, "/" + name, {**identity, **args})

    call("start")
    if leader:
        call("manage")
    for index in range(worker["next_step"], len(worker["node"]["steps"])):
        step = worker["node"]["steps"][index]
        fault = next((f["kind"] for f in job["faults"] if f["worker"] == worker["id"] and
                      f["attempt"] == worker["attempt"] and f["step"] == index), None)
        if fault == "worker_exit":
            return 23
        if fault == "vm_loss":
            return 24  # The daemon exits and terminates all of its worker processes.
        if fault == "invalid_result":
            call("finish", result={"invalid_simulated_result": True})
            return 0
        if fault in {"stall", "retry_loop"}:
            while True:
                if fault == "retry_loop":
                    # Repeated status traffic is deliberately not advancing progress.
                    call("progress", step=index, progress=0)
                time.sleep(interval)
        start = time.monotonic()
        progress = 0
        while time.monotonic() - start < step["seconds"]:
            time.sleep(min(interval, max(0, step["seconds"] - (time.monotonic() - start))))
            progress += 1
            call("progress", step=index, progress=progress)
            if leader:
                call("manage")
        if step["type"] == "effect":
            call("intent", step=index)
            receipt = call("effect", step=index)
            if receipt.get("escalated"):
                return 0
            if fault == "effect_interruption":
                # Effect is durable but the worker has not checkpointed the step.
                return 23
        call("checkpoint", step=index)
    if leader:
        call("manage")
    call("finish", result=worker["node"]["result"])
    return 0


def worker_main(url):
    payload = json.load(sys.stdin)
    try:
        return execute(url, **payload)
    except RemoteError as exc:
        logging.warning("worker stopped: %s", exc)
        return 0 if exc.code == 409 else 23
    except Exception:
        logging.exception("worker lost contact or failed; lease will expire")
        return 23


def run_vm(url, vm_id, interval=0.5):
    def terminate(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, terminate)
    session = str(uuid.uuid4())
    jobs = {}  # key -> (process, job)
    seen = set()

    def stop_process(process):
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()

    registered = False
    try:
        while True:
            identity = {"vm_id": vm_id, "session": session}
            try:
                if not registered:
                    request(url, "/register", identity)
                    registered = True
                    logging.info("registered VM %s", vm_id)
                for key, (process, job) in list(jobs.items()):
                    code = process.poll()
                    if code is not None:
                        del jobs[key]
                        if code == 24:
                            logging.warning("simulated VM loss on %s", vm_id)
                            return 24
                active = [{"task_id": job["task_id"], "worker_id": job["worker"]["id"], "attempt": job["worker"]["attempt"]}
                          for process, job in jobs.values() if process.poll() is None]
                request(url, "/heartbeat", {**identity, "active": active})
                assignments = request(url, "/poll", identity)["jobs"]
                wanted = {(j["task_id"], j["worker"]["id"], j["worker"]["attempt"]) for j in assignments}
                for key in list(jobs):
                    if key not in wanted:
                        stop_process(jobs.pop(key)[0])
                seen.intersection_update(wanted)
                for job in assignments:
                    key = (job["task_id"], job["worker"]["id"], job["worker"]["attempt"])
                    if key in seen:
                        continue
                    process = subprocess.Popen([sys.executable, "-m", "durable_swarms", "worker", "--url", url], stdin=subprocess.PIPE)
                    process.stdin.write(json.dumps({"job": job, "vm_id": vm_id, "session": session, "interval": interval}).encode())
                    process.stdin.close()
                    jobs[key] = (process, job)
                    seen.add(key)
            except RemoteError as exc:
                if exc.code == 409:
                    for process, _ in jobs.values():
                        stop_process(process)
                    jobs.clear()
                    seen.clear()
                    registered = False
                    session = str(uuid.uuid4())
                logging.warning("VM request failed: %s", exc)
            except Exception as exc:
                logging.warning("orchestrator connection unavailable: %s", exc)
            time.sleep(interval)
    except KeyboardInterrupt:
        return 0
    finally:
        for process, _ in jobs.values():
            stop_process(process)
