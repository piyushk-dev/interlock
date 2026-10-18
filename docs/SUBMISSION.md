# Graph Hacks submission: Interlock

Paste these into the form. The text follows ASD-STE100 style at about 80%: short sentences, one idea each, active voice, simple words.

## Project title

Interlock: locks, approvals and an audit trail for AI agents that change production

## Description (what problem does it solve, and who is it for?)

Companies now run many AI agents on the same production systems. These agents deploy code, migrate databases, change feature flags and delete old data. Each agent knows only its own task. Two tasks can look unrelated but still break the same service. For example, one agent migrates orders-db and another agent deploys payments. Checkout uses both services, so checkout fails. After the failure, nobody can say which change caused it. Engineers report these failures on Reddit. One post says: "An AI agent deleted our production database" (r/LocalLLM). Another post asks: "Who approved the command, how do you prove what it did" (r/devops).

Interlock is a control layer outside the agents. An agent asks Interlock before it makes a change. The agent can use MCP or HTTP. Interlock finds the impact zone of the change in a FalkorDB dependency graph. Then it compares the zone with the changes that run now. Interlock gives one of four answers: granted, waiting, needs approval or denied. When a change must wait, Interlock names the other change and the dependency path between them.

Interlock also enforces these rules:
- An agent can change only the environments it is scoped to.
- Each agent and sub-agent must trace back to a human with authority.
- A code freeze stops changes.
- A risky change needs a human to approve it.
- An incident fix goes first.
- Interlock breaks deadlocks.
- A lock expires if its agent crashes.

Interlock records each decision in the graph before the action runs.

In a benchmark, 24 simulated agents ran for 180 simulated seconds. Without Interlock, the agents made 255 changes, and 197 of them collided. With Interlock, the agents made 107 changes, and no change collided. The cost was an average wait of 29.5 seconds.

Interlock is for platform and SRE teams. It lets AI agents change production without loss of control or of the audit trail.

## Tracks

- Agent Memory and Coordination
- Agents That Act on Connected Data

## Repository

https://github.com/piyushk-dev/interlock

## Demo video

YouTube link (upload `video/out/interlock-demo.mp4` as Unlisted). Backup: https://github.com/piyushk-dev/interlock/releases/download/v1.0/interlock-demo.mp4

## Live demo or deployment

https://43.204.238.218.sslip.io (console: /console). Local setup is in the README.

## How did you use FalkorDB? What technical problems did you solve with it?

FalkorDB holds all the state of Interlock in one graph. The graph has 80 resources: services, datastores, topics and feature flags in prod and staging. 118 DEPENDS_ON edges connect them. The graph also holds teams, humans and agents. While agents work, Interlock writes intents, decisions and waits to the same graph. Thus the lock table, the authority model and the audit trail are one graph.

1. Impact zones. A change locks its target and all resources that depend on the target. The depth is 1 to 3 hops, and it depends on the action. Interlock finds the zone with a bounded reverse traversal over DEPENDS_ON. It stores the zone as IMPACTS edges on the Intent node.
2. Atomic check and grant. One Cypher write query does all the work. It compares the IMPACTS edges of the new intent with those of each granted intent. It decides if they conflict. Then it sets the status and creates WAITS_FOR edges. FalkorDB runs each write query atomically. Thus two agents can never get overlapping changes at the same time.
3. Explanations. Interlock uses shortestPath over DEPENDS_ON to explain each wait. For example: "Both changes reach checkout: orders-db, orders, checkout, and payments, checkout."
4. Authority through sub-agents. A human must authorise each agent, directly or through up to three sub-agents. A variable-length DELEGATED_TO*0..3 path finds this human. The human must also have a CAN_CHANGE edge to the team that owns the target. If no such human exists, Interlock denies the change.
5. Deadlocks. Interlock derives WAITS_ON edges between agents from the intent queues. A WAITS_ON*2..8 path back to the start node is a deadlock. Interlock then rolls back the least urgent agent.
6. Audit. Each Decision node holds the verdict, the reason, the dependency path and the authority chain. It links to the intent and to the human who approved it.

## Blog link

(optional)

## What did you like about FalkorDB?

Each write query in FalkorDB is atomic. Thus the core of a lock manager is one Cypher statement: it checks for conflicts and grants the lock. I did not need a transaction protocol. Each policy reads like the rule it enforces. For example, the rule "a human authorised this agent, directly or through up to three sub-agents" is one pattern: (h:Human)-[:AUTHORIZED]->(:Agent)-[:DELEGATED_TO*0..3]->(a). Variable-length paths, shortestPath and pattern comprehensions covered all the checks. I did not need a separate analytics tool. The queries are fast, so they can run before each agent action. FalkorDB also runs as a Redis module. Docker failed on my laptop, so I ran FalkorDB directly in WSL. This took a few minutes.

## What improvements would you like to see in FalkorDB?

- Interactive transactions. I want to read, decide in application code, and then write, as one atomic unit. MULTI/EXEC can group queries, but it cannot branch on a result. So I use an application lock around workflows with many queries.
- Parameters in variable-length bounds. FalkorDB rejects [:DEPENDS_ON*1..$depth]. So the impact-zone query puts the depth into the query text. This stops query caching.
- A built-in procedure for cycle detection that returns one cycle fast. Now I use a variable-length path back to the start node. This works for 31 agents, but it will not scale to thousands.

## AI assistance disclosure

I built Interlock with Claude Code (Anthropic's Claude) as a coding assistant for research, code, UI and documentation. Sarvam Bulbul v3 text-to-speech made the video narration in an Indian English voice. I chose the idea, directed the design and reviewed the results.
