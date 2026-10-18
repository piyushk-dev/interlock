# Graph Hacks submission: Interlock

Paste these into the form. The repo, video and live demo links below are final.

## Project title

Interlock: locks, approvals and an audit trail for AI agents that change production

## Description (what problem does it solve, and who is it for?)

Companies now run many AI agents against the same production systems: deploy bots, migration agents, flag flippers, clean-up scripts and coding agents. Each agent knows only its own task. Two tasks can look unrelated and still break the same service. One agent migrates orders-db while another deploys payments; checkout depends on both, so checkout fails, and nobody can say which change caused it. Engineers report these failures on Reddit ("An AI agent deleted our production database", r/LocalLLM; "Who approved the command, how do you prove what it did", r/devops).

Interlock sits outside the agents. An agent asks Interlock before it acts, over MCP or HTTP. Interlock finds the change's impact zone in a FalkorDB dependency graph and compares it with the changes already running. The answer is granted, waiting (with the other change and the dependency path named), needs approval, or denied. It enforces environment scope, the chain from each agent and sub-agent back to an authorising human, code freezes, sign-off for risky changes, incident priority, deadlock breaking and lease expiry. Every decision is stored in the graph before the action runs.

It is for platform and SRE teams that want to let agents change production without losing control or the audit trail.

In a benchmark with 24 simulated agents over 180 simulated seconds, the fleet without Interlock made 255 changes and 197 of them collided with another change in flight. With Interlock it made 107 changes and none collided, at the cost of an average 29.5 s wait.

## Tracks

- Agent Memory and Coordination (main)
- Agents That Act on Connected Data

## Repository

https://github.com/piyushk-dev/interlock

## Demo video

https://github.com/piyushk-dev/interlock/releases/download/v1.0/interlock-demo.mp4

## Live demo or deployment

https://43.204.238.218.sslip.io (console: /console). Local setup is in the README.

## How did you use FalkorDB? What technical problems did you solve with it?

FalkorDB holds all of Interlock's state: 80 resources (services, datastores, topics and flags in prod and staging) with 118 DEPENDS_ON edges, teams, humans, agents, and at run time the intents, decisions and waits. The lock table, the authority model and the audit trail are one graph.

1. Impact zones. A change locks its target and everything that depends on it, 1 to 3 hops deep depending on the action. That is a bounded reverse traversal over DEPENDS_ON, stored as IMPACTS edges on the Intent node.
2. Atomic check-and-grant. One Cypher write matches the new intent's IMPACTS edges against every granted intent's, decides the conflict (target inside the other's zone, or two exclusive changes reaching the same customer-facing service), sets the status and MERGEs WAITS_FOR edges. FalkorDB runs each write query atomically, so two agents can never both be granted overlapping changes.
3. Explanations. shortestPath over DEPENDS_ON turns each wait into "Both changes reach checkout: orders-db, orders, checkout, and payments, checkout."
4. Authority through sub-agents. A variable-length DELEGATED_TO*0..3 path from an AUTHORIZED edge, filtered by CAN_CHANGE on the owning team, finds the human behind any agent. A sub-agent spawned by an agent without authority over Payments is denied.
5. Deadlocks. Agent-level WAITS_ON edges are derived from intent queues; a cycle WAITS_ON*2..8 back to the start node is a deadlock, and the least urgent agent is rolled back.
6. Audit. Decision nodes with the verdict, reason, dependency path and authority chain are linked to the intent and to the human who signed off.

## Blog link

(optional)

## What did you like about FalkorDB?

Each write query is atomic, so the core of a lock manager (check for conflicts, then grant) is one Cypher statement instead of a transaction protocol. Each policy reads like the sentence it enforces: "a human authorised this agent, directly or through up to three sub-agents" is (h:Human)-[:AUTHORIZED]->(:Agent)-[:DELEGATED_TO*0..3]->(a). Variable-length paths, shortestPath and pattern comprehensions covered every check without a separate analytics stack, and the queries are fast enough to sit in front of every agent action. It runs as a Redis module, so when Docker failed on my laptop I ran it natively in WSL in a few minutes.

## What improvements would you like to see in FalkorDB?

- Interactive transactions: read, decide in application code, then write, as one atomic unit. MULTI/EXEC can batch queries but cannot branch on a result, so I serialise multi-query workflows with an application lock.
- Parameters in variable-length bounds. [:DEPENDS_ON*1..$depth] is not allowed, so the impact-zone query formats the depth into the query string, which defeats query caching.
- A built-in cycle-detection procedure that returns one cycle quickly. I use a variable-length path back to the start node, which is fine at this size but would not scale to thousands of agents.

## AI assistance disclosure

Built with Claude Code (Anthropic's Claude) as a coding assistant for research, code, UI and documentation. The video narration uses Sarvam Bulbul v3 text-to-speech (Indian English voice). The author chose the idea, directed the design and reviewed the results.
