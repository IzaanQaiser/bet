# 0001 — No orchestrator agent

**Status:** Decided

## Context
The obvious way to build a multi-step agentic pipeline is one orchestrator agent that decides, at each step, what happens next — call the extractor, call the resolver, decide whether to ask a clarifying question. This is also the pattern most Taskmaster submissions will reach for, since agent frameworks make it the path of least resistance.

## Decision
There is no orchestrator agent. Control flow is an explicit, code-defined state machine (`docs/architecture/state-machine.md`). LLM agents (Extractor, Resolver, Dispatcher) are invoked by pipeline code with a fixed next step; they are never given a tool that lets them call the next stage themselves or decide the pipeline's shape.

## Alternatives considered
- **Single orchestrator agent with tool access to each stage.** Rejected: an LLM deciding control flow means a hallucinated or injected instruction (e.g. from a photographed note) could, in principle, cause the pipeline to skip a stage — skip confirmation, retry indefinitely, or route around dedupe. A state machine cannot be talked into skipping a state.
- **ADK's built-in multi-agent delegation patterns.** Considered for the Extractor→Resolver handoff specifically. Rejected for the same reason: delegation is still an LLM decision about control flow, even if scoped to one framework's abstraction.

## Consequences
- Every transition is enumerable and testable independently of any LLM call.
- Debugging on camera is just "what state is this item in" — a database read, not a reasoning trace.
- This is a deliberate point to state explicitly in the demo and write-up (per PRD §5), since it reads as restraint rather than a missing feature — most competing submissions will not have made this distinction.
- Cost: slightly more pipeline code (explicit transition functions) than "give the agent tools and let it figure it out." Accepted — this is exactly the tradeoff AGENTS.md asks for ("simple, explicit, testable designs over speculative abstraction").
