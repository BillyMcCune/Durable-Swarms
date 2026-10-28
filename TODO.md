# Project To-Do List

## Project context

**Team members:** Pete Crowley, Billy McCune, Jonathan Ouwerx

**Goal:** Build a fault-tolerant, resource-efficient system for running many independent, concurrent workflow simulations across multiple machines. The simulations model agent swarms without requiring live LLM agents. The system should provide a simple application-layer abstraction: programmers supply workload-specific functions while the platform handles distribution, orchestration, monitoring, recovery, and coordination.

**Canonical workload:** A remote coding-agent-shaped workflow simulation serving many customers simultaneously. Each workload models a leader and subtask workers distributed across one or more machines; separate customer workloads remain independently managed. Simulated workers produce progress events, delays, tool-call-like side effects, failures, and completion results that exercise the orchestration and recovery paths.

**Scope:** The project evaluates distributed orchestration and durable recovery, not LLM quality, prompting, or real coding-agent behavior.

## Core system design

- [ ] Define the programming model and application-layer API: what workload-specific functions authors implement, and what the platform manages automatically.
- [ ] Define the workflow simulator: worker states, task/subtask dependencies, progress events, configurable execution delays, and deterministic completion criteria.
- [ ] Define simulated tool actions and their side effects so replay/idempotency behavior can be tested without invoking real external tools.
- [ ] Define the control-plane architecture for scheduling multiple independent swarms across machines.
- [ ] Implement workload isolation, agent membership tracking, and inter-agent communication for each swarm.
- [ ] Implement resource-aware placement and scheduling; track compute, memory, and machine utilization per workload and agent.
- [ ] Define and implement runtime health monitoring for agent loops, stalls, tool failures, worker crashes, duplicate work, and misaligned subagents.
- [ ] Define misalignment signals and the intervention policy: detection thresholds, escalation, containment, and recovery actions.

## Highest priority: make recovery concrete

- [ ] Define the workload-log schema: task and agent identifiers, ordered steps, tool inputs, results, checkpoints, timestamps, and side-effect/idempotency metadata.
- [ ] Specify durable-write and checkpoint rules so the log remains usable after a VM or process crash.
- [ ] Document the resume flow: detect a failed agent, provision a replacement, load the latest checkpoint and workload log, and continue from the first incomplete step.
- [ ] Design replay protections for side-effecting tool calls: record completion receipts/idempotency keys, reconcile ambiguous calls, and never automatically repeat an action whose outcome is unknown.
- [ ] Implement and test recovery paths for clean interruption, crash during a tool call, and crash immediately after an external side effect.

## Evaluation

- [ ] Build a hardware-appropriate testbed with a small distributed workflow simulation and multiple concurrent workloads.
- [ ] Create representative coding-agent-shaped simulated workflows with clear completion criteria and controlled worker-fault injection.
- [ ] Inject and categorize faults: worker/VM crashes, stalled or looping agents, tool errors, duplicate-work scenarios, and misaligned subagents.
- [ ] Measure recovery efficacy: successful recovery rate, workload/task completion rate, and correctness after recovery.
- [ ] Measure mean recovery time per agent, including failure detection, replacement provisioning, state restoration, and resumed progress.
- [ ] Measure orchestration and monitoring overhead: CPU, memory, network/logging cost, and impact on workload latency/throughput.
- [ ] Establish baselines and experimental controls so results distinguish platform overhead from model/task variability.
- [ ] Document why simulation is used and which conclusions about real agent deployments it can and cannot support.

## Related work and positioning

- [ ] Verify and normalize all related-work citations and numbering in the final bibliography.
- [ ] Clearly position the project against self-healing orchestrators, AgentScope, observability/fault-injection systems, and post-hoc failure-attribution work.
- [ ] Emphasize the distinction: runtime management of many independent, distributed swarms—not just a single workflow, tracing, or post-hoc attribution.

## Final report

- [ ] Migrate the final report to the USENIX template.
- [ ] Update report structure, citations, figures, and build instructions to conform to the template.
- [ ] Add a clear recovery-story section covering the workload log, resume procedure, and safe replay semantics.

## Demo

- [ ] Design an easy-to-follow recovery demo around a simulated coding-agent workflow.
- [ ] Start the workflow on one VM, deliberately kill that VM halfway through, then resume the simulated worker on a different machine.
- [ ] Show the workload log and checkpoint before and after recovery, including why completed side-effecting actions are not repeated.
- [ ] Script the demo and prepare a short narrated walkthrough with expected output and fallback steps.

## Follow-up

- [ ] Review the recovery design and demo with the team; incorporate feedback into the report and implementation plan.
- [ ] Assign owners and target dates for architecture, recovery implementation, evaluation, report, and demo work.
