"""A mini-swe-agent whose conversation survives the loss of its machine.

DurableAgent appends every batch of messages to the orchestrator's durable log
as it is produced, together with the workspace files that changed. A new attempt
on any VM rebuilds the message list and the files from that log and carries on:
logged model replies are never requested again.
"""

from pathlib import Path

from minisweagent.agents.default import DefaultAgent
from minisweagent.exceptions import FormatError, InterruptAgentFlow, Submitted

from . import sandbox

MAX_FILE_BYTES = 64 * 1024
MAX_SNAPSHOT_BYTES = 256 * 1024
SKIP_DIRS = {".harness", "__pycache__"}


# Read every workspace file the agent may have produced, as {relative path: text}.
# Harness files, caches, and files that are too large or not text are left out.
def scan(workspace):
    files = {}
    for path in sorted(Path(workspace).rglob("*")):
        relative = path.relative_to(workspace)
        if not path.is_file() or path.is_symlink() or SKIP_DIRS & set(relative.parts):
            continue
        try:
            if path.stat().st_size <= MAX_FILE_BYTES:
                files[str(relative)] = path.read_text()
        except (OSError, UnicodeDecodeError):
            pass
    return files


# Apply the file changes stored in log records, oldest first, to a workspace.
def restore(workspace, records):
    for record in records:
        for name, content in (record["data"].get("files") or {}).items():
            target = Path(workspace) / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
        for name in record["data"].get("deleted") or []:
            (Path(workspace) / name).unlink(missing_ok=True)


class DurableAgent(DefaultAgent):
    # Inputs:
    #   model, env: mini-swe-agent model and environment.
    #   call: Callable(endpoint, **fields) making a fenced orchestrator request.
    #   workspace: Directory the agent works in, already rebuilt from the log.
    #   problems: {problem_id: problem} for the problems assigned to this agent.
    #   before_action: Callable(turn) run before each command executes.
    #   **kwargs: AgentConfig fields (templates, step_limit, ...).
    def __init__(self, model, env, *, call, workspace, problems, before_action, **kwargs):
        super().__init__(model, env, **kwargs)
        self.call = call
        self.workspace = Path(workspace)
        self.problems = problems
        self.before_action = before_action
        self.seq = 0
        self.baseline = {}
        self.suspend_pending = False
        self.final_review = None

    @property
    def flag(self):
        return self.workspace / ".harness" / "suspend"

    # Log each new batch of messages before the agent acts on it.
    def add_messages(self, *messages):
        added = super().add_messages(*messages)
        if added:
            roles = {m.get("role") for m in added}
            if "system" in roles:
                kind = "init"
            elif "exit" in roles:
                kind = "exit"
            elif "assistant" in roles:
                kind = "query"
            elif any(m.get("extra", {}).get("interrupt_type") == "FormatError" for m in added):
                kind = "format_error"
            elif any(m.get("extra", {}).get("review") for m in added):
                kind = "review"
            else:
                kind = "observation"
            self.record(kind, added)
        return added

    # Append one durable record: the messages, the files changed since the last
    # record, visible-test results for changed solutions, and a suspend request.
    def record(self, kind, messages, **extra):
        data = {"messages": [self.slim(m) for m in messages], **extra}
        if kind not in ("init", "query"):
            current = scan(self.workspace)
            changed = {name: text for name, text in current.items() if self.baseline.get(name) != text}
            while sum(len(t) for t in changed.values()) > MAX_SNAPSHOT_BYTES:
                changed.pop(max(changed, key=lambda n: len(changed[n])))
            deleted = sorted(set(self.baseline) - set(current))
            self.baseline = {n: t for n, t in current.items() if n in changed or self.baseline.get(n) == t}
            if changed:
                data["files"] = changed
            if deleted:
                data["deleted"] = deleted
            checks = {}
            for problem_id, problem in self.problems.items():
                if f"problems/{problem_id}/solution.py" in changed:
                    grade = sandbox.grade(self.workspace / "problems" / problem_id / "solution.py",
                                          problem, problem["visible_tests"])
                    checks[problem_id] = {"passed": grade["passed"], "total": grade["total"]}
            if checks:
                data["checks"] = checks
            if kind == "observation" and self.flag.exists():
                self.flag.unlink()
                data["suspend"] = self.suspend_pending = True
        self.call("agent_log", seq=self.seq, kind=kind, data=data)
        self.seq += 1

    # Drop the raw provider response from a message; everything needed to resend
    # the conversation or re-run its command is kept.
    @staticmethod
    def slim(message):
        extra = {k: v for k, v in message.get("extra", {}).items() if k != "response"}
        return {**{k: v for k, v in message.items() if v is not None and k != "extra"}, "extra": extra}

    # Run, or continue, the agent until it exits or suspends.
    # Inputs:
    #   records: The agent's durable log so far (empty on a first attempt).
    #   **template_vars: Variables for the prompt templates.
    # Output: "suspended" if the agent gave up its slot to wait for subagents,
    #         otherwise its exit status ("Submitted", "LimitsExceeded", ...).
    def run_durable(self, records, **template_vars):
        self.extra_template_vars |= {"repeat_note": "", **template_vars}
        self.messages = [m for r in records for m in r["data"].get("messages", [])]
        self.seq = len(records)
        self.n_calls = sum(r["kind"] in ("query", "format_error") for r in records)
        self.baseline = scan(self.workspace)
        if not self.messages:
            self.add_messages(
                self.model.format_message(role="system", content=self._render_template(self.config.system_template)),
                self.model.format_message(role="user", content=self._render_template(self.config.instance_template)))
        self.suspend_pending = bool(records and records[-1]["data"].get("suspend"))
        # A logged reply whose command has no logged result: run the command, not the model.
        pending = self.messages[-1] if self.messages[-1].get("role") == "assistant" else None
        while self.messages[-1].get("role") != "exit":
            if self.suspend_pending:
                if not self.absorb_children():
                    return "suspended"
                continue
            try:
                if pending is not None:
                    message, pending = pending, None
                    self.execute_actions(message)
                else:
                    self.step()
                self.n_consecutive_format_errors = 0
            except FormatError as exc:
                self.n_consecutive_format_errors += 1
                if 0 < self.config.max_consecutive_format_errors <= self.n_consecutive_format_errors:
                    self.add_messages(*exc.messages, {"role": "exit", "content": "RepeatedFormatError",
                                                      "extra": {"exit_status": "RepeatedFormatError", "submission": ""}})
                else:
                    self.add_messages(*exc.messages)
            except Submitted as exc:
                self.submitted(exc)
            except InterruptAgentFlow as exc:
                self.add_messages(*exc.messages)
        return self.messages[-1].get("extra", {}).get("exit_status", "")

    # The source of every assigned solution, as the orchestrator will grade it.
    def solutions(self):
        out = {}
        for problem_id in self.problems:
            path = self.workspace / "problems" / problem_id / "solution.py"
            out[problem_id] = path.read_text(errors="replace")[:MAX_FILE_BYTES] if path.is_file() else ""
        return out

    # The agent said it is done: ask the orchestrator to check. It may accept, send
    # the agent back to work with the failures, or make it wait for its subagents.
    def submitted(self, exc):
        final = 0 < self.config.step_limit <= self.n_calls
        reply = self.call("review", _timeout=300, solutions=self.solutions(), final=final,
                          exit_status="Submitted", turns=self.n_calls)
        if reply.get("wait"):
            self.flag.parent.mkdir(exist_ok=True)
            self.flag.write_text("1")
            self.add_messages({"role": "user", "extra": {"waiting": True}, "content":
                               "Your subagents are still working, so you cannot finish yet. You are paused until they "
                               "are done; you will then see their results and can finish."})
        elif reply["final"]:
            self.final_review = reply
            self.add_messages(*exc.messages)
        else:
            self.add_messages({"role": "user", "content": reply["feedback"], "extra": {"review": True}})

    # Run the reply's command. If the model has sent the identical command several
    # times in a row, say so in the observation so it can break out of the loop.
    def execute_actions(self, message):
        self.before_action(self.n_calls)
        actions = message.get("extra", {}).get("actions")
        repeats = 0
        for earlier in reversed([m for m in self.messages[:-1] if m.get("role") == "assistant"]):
            if earlier.get("extra", {}).get("actions") != actions:
                break
            repeats += 1
        self.extra_template_vars["repeat_note"] = (
            f"You have now run this identical command {repeats + 1} times with the same result. "
            "Repeating it will not help. Read the error carefully and do something different."
            if actions and repeats >= 1 else "")
        return super().execute_actions(message)

    # Give up the slot until subagents finish, or, if they already have, add their
    # results to the conversation and their files to subagents/<id>/.
    # Output: True if the agent should continue now, False if it was suspended.
    def absorb_children(self):
        reply = self.call("suspend")
        if reply["state"] != "ready":
            return False
        lines, unsolved = [], []
        for child_id, child in sorted(reply["children"].items()):
            grades = (child.get("result") or {}).get("problems") or {}
            for problem_id, grade in grades.items():
                target = self.workspace / "subagents" / child_id / "problems" / problem_id / "solution.py"
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(grade.get("code") or "")
                solved = grade["total"] and grade["passed"] == grade["total"]
                if not solved:
                    unsolved.append(problem_id)
                lines.append(f"- {child_id} / {problem_id}: {'PASSED' if solved else 'FAILED'} {grade['passed']}/{grade['total']} hidden tests"
                             f" (subagents/{child_id}/problems/{problem_id}/solution.py)")
        # A problem a subagent solved is finished: its solution counts for this agent as it stands.
        advice = ("Still unsolved: " + ", ".join(unsolved) + ". Fix those in your own problems/<id>/solution.py. The problems "
                  "marked PASSED are finished and count for you as they are: do not solve or test them again."
                  if unsolved else
                  "Every problem you handed out is solved and counts for you as it is: do not solve or test those again.")
        text = ("All your subagents have finished. Hidden-test results:\n" + "\n".join(lines) + "\n\n" + advice
                if lines else "You have no subagents to wait for.")
        self.suspend_pending = False
        self.messages.append({"role": "user", "content": text, "extra": {"children": sorted(reply["children"])}})
        self.record("children", self.messages[-1:])
        return True
