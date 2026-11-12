"""Model wrapper: mini-swe-agent's text-based litellm model pointed at Ollama.

Every request goes to LLM_URL (the laptop's Ollama, usually through an SSH
reverse tunnel) and its start and end are reported to the orchestrator so the
dashboard can show live LLM traffic.
"""

import os
import time
import uuid

from minisweagent.models.litellm_textbased_model import LitellmTextbasedModel

DEFAULT_LLM_URL = "http://localhost:11434"
OBSERVATION = """{% if output.exception_info %}<exception>{{ output.exception_info }}</exception>
{% endif %}<returncode>{{ output.returncode }}</returncode>
{% if output.output | length <= 4000 %}<output>
{{ output.output }}</output>{% else %}<output_head>
{{ output.output[:2500] }}
</output_head>
<elided>{{ output.output | length - 4000 }} characters elided; print less next time</elided>
<output_tail>
{{ output.output[-1500:] }}</output_tail>{% endif %}{% if repeat_note %}
<warning>{{ repeat_note }}</warning>{% endif %}"""
FORMAT_ERROR = """Your reply was rejected: it contained {{ actions | length }} command blocks, but it must contain exactly ONE.
Reply with a short THOUGHT followed by one block like this:

```mswea_bash_command
your command here
```

If you have finished, the command is: echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"""


class MeteredModel(LitellmTextbasedModel):
    # Inputs:
    #   report: Callable(**fields) that sends call telemetry; must not raise.
    #   **kwargs: LitellmTextbasedModel configuration.
    def __init__(self, *, report, **kwargs):
        super().__init__(**kwargs)
        self._report = report

    # Time one completion request and report its outcome and token counts.
    def _query(self, messages, **kwargs):
        call_id, started = str(uuid.uuid4()), time.monotonic()
        self._report(phase="start", call_id=call_id, model=self.config.model_name)
        try:
            response = super()._query(messages, **kwargs)
        except BaseException:
            self._report(phase="end", call_id=call_id, ok=False, latency=time.monotonic() - started)
            raise
        usage = getattr(response, "usage", None)
        self._report(phase="end", call_id=call_id, ok=True, latency=time.monotonic() - started,
                     prompt_tokens=getattr(usage, "prompt_tokens", None),
                     completion_tokens=getattr(usage, "completion_tokens", None))
        return response


# Build the model for one agent.
# Inputs:
#   name: Ollama model tag, for example "qwen2.5-coder:7b".
#   report: Telemetry callable passed to MeteredModel.
# Output: A MeteredModel using LLM_URL, LLM_NUM_CTX and LLM_TEMPERATURE from the environment.
def build_model(name, report):
    return MeteredModel(
        report=report,
        model_name="ollama_chat/" + name,
        cost_tracking="ignore_errors",
        observation_template=OBSERVATION,
        format_error_template=FORMAT_ERROR,
        model_kwargs={"api_base": os.environ.get("LLM_URL", DEFAULT_LLM_URL),
                      "num_ctx": int(os.environ.get("LLM_NUM_CTX", "16384")),
                      "temperature": float(os.environ.get("LLM_TEMPERATURE", "0.2")),
                      "timeout": 240, "drop_params": True})
