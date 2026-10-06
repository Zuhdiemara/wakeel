# The agent graph

Generated from the compiled LangGraph by `scripts/graph_doc.py`; do not edit by hand.
Solid edges are fixed; dotted edges are the supervisor's routing, a plain function of the state.

```mermaid
---
config:
  flowchart:
    curve: linear
---
graph TD;
	__start__([<p>__start__</p>]):::first
	intake(intake)
	supervisor(supervisor)
	policy_agent(policy_agent)
	ops_agent(ops_agent)
	approval(approval)
	clarify(clarify)
	execute(execute)
	handoff(handoff)
	respond(respond)
	__end__([<p>__end__</p>]):::last
	__start__ --> intake;
	approval --> supervisor;
	clarify --> supervisor;
	execute --> supervisor;
	intake --> supervisor;
	ops_agent --> supervisor;
	policy_agent --> supervisor;
	supervisor -.-> approval;
	supervisor -.-> clarify;
	supervisor -.-> execute;
	supervisor -.-> handoff;
	supervisor -.-> ops_agent;
	supervisor -.-> policy_agent;
	supervisor -.-> respond;
	handoff --> __end__;
	respond --> __end__;
	classDef default fill:#f2f0ff,line-height:1.2
	classDef first fill-opacity:0
	classDef last fill:#bfb6fc
```
