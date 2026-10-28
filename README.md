# Durable Swarms

Run concurrent workflow simulations across a fixed VM pool and resume failed
workers from durable state. The implementation uses Python 3.10+ and its standard
library: no external packages or LLM API keys are required.

Team: Pete Crowley, Billy McCune, Jonathan Ouwerx.

## Run locally

From the repository root, start the orchestrator in one terminal:

```sh
python3 -m durable_swarms orchestrator --config examples/cluster.json
```

Start one VM daemon in each of two more terminals:

```sh
python3 -m durable_swarms vm --id vm-1
```

```sh
python3 -m durable_swarms vm --id vm-2
```

Submit a workflow and inspect its state:

```sh
python3 -m durable_swarms submit examples/workflow.json --task-id example-1
python3 -m durable_swarms status example-1
python3 -m durable_swarms events example-1 --limit 500
```

The example spawns an implementation subtask and a dependent review subtask. It
deliberately exits the implementation worker after applying a simulated patch but
before checkpointing the step. Recovery resumes that worker, returns the original
effect receipt, and completes the workflow with `patches_applied: 1`. Give each new
run a different task ID; resubmitting the same ID/spec returns the existing task.

State is stored in `swarms.sqlite3` in the working directory. Restarting the
orchestrator with the same database reconstructs workload state from the log.
Interrupting a VM daemon terminates its workers; the other registered VM can
recover their workloads. Use Ctrl-C to stop these services.

## Run across machines

Put this repository and Python on each existing VM. Set `host` to `0.0.0.0` in
the orchestrator configuration, choose a persistent database path, and set the
fixed `vms` map to the VM IDs and their worker-slot counts. Run the orchestrator
on a machine reachable over the trusted private network. On each worker VM:

```sh
python3 -m durable_swarms vm --id vm-1 --url http://ORCHESTRATOR_PRIVATE_IP:8080
```

Use the matching configured ID on each machine. Client commands accept the same
`--url` option. Keep the worker heartbeat interval (default 0.5 seconds) comfortably
below the configured lease and progress timeouts. The API has no authentication;
keep it on the private network. The recreate command fences a VM's assignments
and permits worker-service re-registration; booting a stopped VM remains the
responsibility of your existing VM manager.

## Workflow format

Workflows declare a `seed`, one `leader`, a list of `workers`, and optional
`faults`. Each worker has an `id`, ordered `steps`, optional `depends_on` child IDs,
and a declared `result`. Dependencies must form a DAG. Leaders automatically
spawn their declared children and yield their slot while waiting for them.

- `compute`: `seconds` is a nonnegative duration or a seeded `[min, max]` range.
- `effect`: increments the workload-local counter `key` by integer `delta`;
  optionally includes a delay in `seconds`.
- Fault: `worker`, positive `attempt`, zero-based `step`, and `kind` from
  `worker_exit`, `vm_loss`, `stall`, `retry_loop`, `invalid_result`, or
  `effect_interruption`.

The worker checkpoint records the next incomplete step. Completed steps are not
reexecuted; an interrupted delay can restart. Effect operation IDs are stable
across worker attempts and machines. Effects and their receipts commit atomically,
so retrying an effect after losing its response does not apply it twice.

## Implementation checks

```sh
python3 -m unittest discover -s tests -v
```

These checks cover leases, stale-attempt rejection, parent/child recovery,
capacity/dependencies, durable effects, all modeled failures over HTTP, and an
orchestrator process restart. Integration checks create local server and VM-daemon
processes; they are not measurements on separate physical VMs.

See [architecture and recovery semantics](docs/architecture.md), the
[implementation checklist](ARCHITECTURE_AND_IMPLEMENTATION_TODO.md), and the
[broader project to-do list](TODO.md).
