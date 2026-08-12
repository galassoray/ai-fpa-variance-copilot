"""
agent/
======
FP&A close-cycle agent. Phase 1 delivers the *bounded surface* only: the
materialization step that publishes computed outputs as queryable marts, and the
tool registry the agent is permitted to act through.

No LLM is imported anywhere in this package at Phase 1. That is deliberate and
testable: the tool surface must be provably correct before a model is allowed to
choose among its members.
"""
