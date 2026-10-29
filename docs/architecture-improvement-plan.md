# Architecture refinement plan

Implement these changes in order, preserving existing workflow specifications and
the documented function-comment format (description, inputs, output).

1. [ ] Fair scheduling: persist the last successfully scheduled workload in the
   assignment transaction and rotate the next pass after it. Preserve the cursor
   when no work can be placed and across orchestrator restarts.
2. [ ] Lean persistence: put lease renewals and partial progress in compact durable
   worker records, separate from full workload checkpoints. Retain transactional
   snapshots for lifecycle, step completion, and effect decisions. Index active
   workloads and avoid scanning finished workloads during scheduling/monitoring.
3. [ ] Execution interface: introduce an executor contract and runtime context for
   progress, checkpoint results, and idempotent effects. Make the simulator its
   default implementation and support an explicitly configured Python executor
   on the orchestrator and VM daemons, without accepting executable code in requests.
4. [ ] Recovery contracts: define and verify stale writers during partitions,
   simultaneous leader/child loss, durable lease renewal across restart, lost
   effect/checkpoint replies, recovery-budget escalation, and waiting for capacity.
5. [ ] Integration: run focused regression and HTTP/process checks, update the
   architecture/checklists and USENIX report, and regenerate its PDF preview.

Completion means existing examples still work, the new guarantees have regression
coverage, and documentation distinguishes supported behavior from remaining limits.
No new service, live agent dependency, or VM provisioning layer is needed.
