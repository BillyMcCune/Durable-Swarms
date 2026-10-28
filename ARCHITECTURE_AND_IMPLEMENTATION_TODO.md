# Architecture and Implementation To-Do

Implemented in `durable_swarms/`; see `docs/architecture.md` for the concrete
interfaces, state transitions, and recovery guarantees. `examples/` provides a
runnable fixed-pool configuration. VM recreation means resetting the worker
service and fencing old assignments; provisioning or booting VMs remains with the
existing VM manager. Local multi-process verification does not claim deployment
on separate physical VMs.

## Scope and guiding decisions

- [x] Keep the project focused on simulating agent-shaped workflows on VMs; do not integrate live LLM agents or real coding tools.
- [x] Use one logical orchestrator and a durable workload log as the system of record; do not introduce consensus, distributed transactions, or a separate tracing platform.
- [x] Use at-least-once worker execution together with idempotent simulated effects, rather than attempting unsupported exactly-once execution.
- [x] Define the boundary between the platform and application: the application submits a workflow specification; the platform owns placement, lifecycle, logging, recovery, and status.

## Architecture specification

- [x] Define the topology: application, orchestrator, durable workload-log store, fixed VM pool, and worker slots per VM.
- [x] Define the workload model: one logical leader per workload, simulated subtask workers, parent/child relationships, dependencies, and completion rules.
- [x] Define the workload lifecycle: queued, running, recovering, and terminal completed/failed/escalated states.
- [x] Define the worker-attempt lifecycle, progress/failure reports, supersession, and slot-yielding leader waits.
- [x] Define placement rules for assigning a worker attempt to an available VM slot and releasing the slot on completion or failure.
- [x] Specify heartbeat and lease behavior, including timeout handling and how the system identifies a failed VM or worker.
- [x] Specify ownership boundaries: the orchestrator recovers leader failures; the leader manages its simulated subtask workers.

## Interfaces and data model

- [x] Specify the application API: `SubmitTask(spec)` and `GetStatus(task_id)`.
- [x] Specify the orchestrator API: create workload, spawn worker, place/stop worker, receive status, and reset a VM worker service.
- [x] Specify the worker API: run workflow step, report progress/result, spawn subtask, and invoke a simulated effect.
- [x] Define durable workload-log records: workload ID, worker ID, attempt ID, parent ID, sequence number, event type, payload, timestamp, and state version.
- [x] Define the essential event types: workload creation, assignment, lease renewal, progress, subtask creation, effect intent, effect completion, worker completion/failure, recovery start, and recovery completion.
- [x] Assign each worker run an incrementing attempt/lease ID and reject writes or reports from superseded attempts.
- [x] Define deterministic operation IDs for simulated side effects, derived from workload, workflow step, and attempt-independent logical operation identity.

## Durable execution and recovery

- [x] Implement append and ordered-read operations for the workload log, indexed by workload and filterable by worker.
- [x] Persist a recovery checkpoint that identifies each worker's latest durable state and next incomplete workflow step.
- [x] Record simulated-effect intent before execution and its receipt/result after execution.
- [x] Implement an idempotent simulated-effect registry: repeated calls with the same operation ID return the original receipt/result without reapplying the effect.
- [x] Implement recovery reconciliation: inspect the log, fence old worker attempts, resolve incomplete effects from their operation IDs, and create a replacement attempt.
- [x] Resume only the first incomplete step after the latest checkpoint; do not restart completed workflow steps.
- [x] Implement terminal failure and escalation for invalid results, exhausted recovery budgets, or inconsistent effect receipts.

## Workflow simulator

- [x] Define a compact workflow-spec format for simulated leaders, subtasks, dependencies, execution duration, progress events, effects, and terminal result.
- [x] Implement a worker simulator with deterministic declared results and seeded durations that emits durable progress events.
- [x] Implement simulated subtask spawning and result delivery to the parent leader.
- [x] Implement configurable modeled failures: worker exit, VM loss, stall, retry loop, invalid result, and effect interruption.
- [x] Record the seed and fault schedule in the workload log; distinguish reproducible inputs from timing-dependent concurrent scheduling.

## Orchestrator and VM runtime

- [x] Implement VM registration, capacity tracking, heartbeats, and worker-slot allocation.
- [x] Implement the scheduling loop for multiple concurrent workloads and available VM slots.
- [x] Implement the monitoring loop that evaluates leases, heartbeats, and durable progress to trigger recovery.
- [x] Implement status aggregation from workload-log events for the application API.
- [x] Expose minimal operational visibility: current workload state, active worker attempts, assigned VM, latest progress, and recovery reason.

## Integration deliverables

- [x] Document the final architecture diagram and recovery sequence using the implemented terms and interfaces.
- [x] Document replay semantics, including what guarantees are provided for simulated effects and when the system escalates instead of replaying.
- [x] Provide a runnable configuration for the VM-pool workflow simulator and orchestrator.
