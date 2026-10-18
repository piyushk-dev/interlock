---
title: "Graph Hacks diary: a dropped idea, a night on Reddit, and locks for AI agents"
published: false
description: How I threw away my first hackathon project, found a better problem in Reddit comments, and built Interlock, a FalkorDB graph that stops AI agents from colliding in production.
tags: ai, agents, graphdatabase, hackathon
cover_image: https://raw.githubusercontent.com/piyushk-dev/interlock/main/docs/screens/live-desktop.png
---

## Friday: the idea I loved

Graph Hacks started on a Thursday. By Friday night I had a project I was proud of.

It was called Airside. You told it your nationality and the visas you held, and it checked flight routes connection by connection. A layover in Frankfurt? Fine for you. A layover in London? You need a transit visa, unless you hold a US one. Every answer came with the rule behind it, pulled from a graph of 3,000 airports and 35,000 routes. It was clean. It was fast. It had a map.

I showed it to a friend. He clicked around for a minute and said one sentence.

"A chatbot can google that."

I wanted to argue. I opened my mouth to argue. Then I closed it, because he was right. Airside was a nice interface over facts that already exist on the internet. A judge would ask the same question in the first ten seconds.

So at 11 p.m. on Friday, with two days left, I had a working project and no reason for it to exist.

## Saturday, 1 a.m.: Reddit

I did what I should have done first. I stopped thinking about graphs and went to read what people were actually stuck on.

Reddit blocks most bots, so I read it the slow way, thread by thread. I had one rule: read the comments, not just the posts. The posts tell you what happened. The comments tell you what hurts.

Around 1 a.m., three threads sat next to each other in my tabs.

> "An AI agent deleted our production database." (r/LocalLLM)

> "Who approved the command, how do you prove what it did." (r/devops, on why security teams keep blocking AI agents)

> "Merge conflicts, duplicated functions, agents working at cross-purposes." (r/ClaudeAI)

None of these people were asking for a smarter agent. They had smart agents. Their problem was many agents at once, and nobody in charge of them.

Then the picture came together. A large company does not run one agent. It runs dozens: one deploys, one migrates databases, one flips feature flags, one cleans up old data, one is somebody's coding agent with a production token. Each one is careful. Each one checks its own work. And none of them can see the others.

## The scene I kept picturing

I could not stop imagining one night. I never saw it happen. I think it happens somewhere every week.

It is 2:47 a.m. on the first night of a Diwali sale at an online store. The on-call engineer's phone buzzes twice, then does not stop. Checkout is failing for one customer in three.

He asks what changed, and the answer is two lines:

```text
02:41  migrator-11   migrate  orders-db@prod   "add index for order history"   OK
02:43  deployer-03   deploy   payments@prod    "v2.4 rollout"                  OK
```

Two agents, two green ticks. Both did exactly what they were asked. But the migration slowed `orders`, the new payments build retried harder, and `checkout` depends on both. The two tasks looked unrelated. In the dependency graph, they met at checkout.

He rolls both back. Forty minutes later checkout is green again. The next day, security asks who approved the migration. The honest answer is that nobody did. The agent had a token, and the token had permission. Permission is not approval.

![Both changes reach checkout](https://raw.githubusercontent.com/piyushk-dev/interlock/main/docs/screens/ex-step3.png)

That was the moment it clicked for me. "Can this change run now?" is not a question about one service. It is a question about a graph: the target, everything that depends on it, every other change touching those things, the team that owns them, and the human behind the agent.

A graph hackathon, and a problem that is a graph. I deleted nothing, moved Airside to a folder called `airside`, and started again.

## Saturday: Interlock

The idea fits in one sentence: an agent asks Interlock before it acts.

It asks over MCP or plain HTTP, and it gets one of four answers: **granted**, **waiting**, **needs approval** or **denied**. Every answer is written into the graph before the agent acts. So the lock table, the rules and the audit trail all live in one FalkorDB graph.

![Interlock graph model](https://raw.githubusercontent.com/piyushk-dev/interlock/main/docs/screens/graph-model.png)

The first query I wrote finds the impact zone. A change to `orders-db` hurts more than `orders-db`; it hurts everything that depends on it. So Interlock walks the dependency edges backwards from the target, one to three hops depending on the action:

```cypher
MATCH (t:Resource {id: $target})
OPTIONAL MATCH p = (d:Resource)-[:DEPENDS_ON*1..2]->(t)
WITH t, d, min(length(p)) AS hops
RETURN t.id AS target, collect({id: d.id, hops: hops, tier: d.tier}) AS zone
```

The second query is the one I am proudest of. Every lock manager has the same trap: two agents ask in the same millisecond, both clear the check, and both win. FalkorDB runs each write query atomically, so I made the check and the grant one statement:

```cypher
MATCH (n:Intent {id: $id})
OPTIONAL MATCH (n)-[a:IMPACTS]->(r:Resource)<-[b:IMPACTS]-(g:Intent {status: 'granted'})
WHERE g <> n AND (
      (a.role = 'target' AND n.mode = 'exclusive')
   OR (b.role = 'target' AND g.mode = 'exclusive')
   OR (r.tier = 1 AND n.mode = 'exclusive' AND g.mode = 'exclusive'))
WITH n, collect(DISTINCT g) AS blockers
SET n.status = CASE WHEN size(blockers) = 0 THEN 'granted' ELSE 'waiting' END
FOREACH (g IN blockers | MERGE (n)-[:WAITS_FOR]->(g))
```

The last line of the `WHERE` is the Diwali-night rule: two exclusive changes that both reach a customer-facing service cannot run together. `orders-db` and `payments` look far apart. The graph knows they are not.

Then came the rules that answer the security team:

- **Who is behind this agent?** Every agent must trace back to a human who can change the owning team's resources, through up to three sub-agents: `(h:Human)-[:AUTHORIZED]->(:Agent)-[:DELEGATED_TO*0..3]->(a)`. A sub-agent spawned by an agent with no authority over payments gets denied.
- **Deadlocks** are cycles in the waits-on graph: `(a:Agent)-[:WAITS_ON*2..8]->(a)`. Interlock rolls back the less urgent agent.
- **Crashes**: every lock is a lease. An agent that goes down stops sending heartbeats, and its lock expires after 40 seconds.
- **Freezes, environment scope and sign-off** for deletes in prod and payment database migrations.

## Saturday night: the red map

To show any of this, I needed agents. So I built a fake online store (40 production services, databases, topics and flags) and a fleet of 24 simulated agents that deploy, migrate, scale and flip flags all day. Then I added one switch: Enforcement on or off.

The first time I turned enforcement off, the map went red in about four seconds. Cards flashing, collisions stacking up in the feed. I laughed out loud at 2 a.m., alone, which tells you something about hackathons.

![Interlock console with 24 agents](https://raw.githubusercontent.com/piyushk-dev/interlock/main/docs/screens/live-desktop.png)

Then I measured it properly: same fleet, same random seed, 180 simulated seconds.

| | Without Interlock | With Interlock |
|---|---:|---:|
| Changes made | 255 | 107 |
| Collided with another change in flight | 197 | 0 |
| Collided on a customer-facing service | 116 | 0 |
| Landed cleanly | 58 | 107 |
| Average wait before a change starts | 0 s | 29.5 s |

Without coordination, the fleet looks twice as busy. Most of that work collides. With Interlock, fewer changes go out, and all of them land. The 29.5-second wait is the price of a strict rule, and I left it in the results on purpose. I would rather show the cost than hide it.

## Sunday: the parts nobody puts in the demo

**Docker broke.** Docker Desktop crashed on a stale socket and would not start again. FalkorDB is a Redis module, so I downloaded the module and ran it natively in WSL. It took ten minutes, and it never crashed once after that.

**Cypher surprised me twice.** FalkorDB does not accept a parameter as a path length (`*1..$depth`). The error says "Encountered unhandled type in inlined properties", which took me a while to decode. Now the depth goes into the query text. I also learned to keep `ro_query` for reads only, after a write I had sent through it failed.

**My first UI looked like every AI demo.** Pills, glowing gradients, a catchy hero line. The feedback was blunt: no slogans, say plainly what it is. So the product page now opens with "Interlock coordinates AI agents that change production." It is boring, and it is better. The map got rebuilt too: named cards instead of dots, curved edges, and a dark tag on each card for the agent that holds the lock.

**The video voice kept changing accents.** I generated the narration clip by clip. Halfway through, I hit the free quota on one text-to-speech model and finished on another. Each clip sounded like a different person. I threw the whole thing away and regenerated every line with one Indian English voice from Sarvam's Bulbul v3. The lesson cost me an hour: one voice, one model, one setting, or nothing.

![Every decision explains itself](https://raw.githubusercontent.com/piyushk-dev/interlock/main/docs/screens/card-desktop.png)

## What I learned

1. **Drop the idea, not the hackathon.** Airside worked. It was also pointless. One honest sentence from a friend saved the weekend.
2. **Read the comments.** The best problem statements on Reddit were never in the posts.
3. **An explanation beats a verdict.** "Waiting" makes an agent retry without knowing why. "Waiting for migrator-11, because both changes reach checkout" tells an agent, or a person, what to do.
4. **A graph database can be a lock manager.** One atomic write query replaced the transaction protocol I thought I would need.
5. **Policies should read like sentences.** When security asks what the rule is, I can show them the query.
6. **Show the cost.** A 29.5-second wait is a real trade-off. Judges and users trust numbers that include the bad part.

## Try it

- **Live demo:** https://43.204.238.218.sslip.io. Open the console, turn Enforcement off and watch the map turn red. Then turn it back on.
- **Code:** https://github.com/piyushk-dev/interlock. The README has the graph model, every Cypher query and the setup.
- **Video:** https://github.com/piyushk-dev/interlock/releases/download/v1.0/interlock-demo.mp4

You can put it in front of Claude Code too:

```bash
claude mcp add interlock \
  -e INTERLOCK_URL=https://your-interlock \
  -e INTERLOCK_AGENT=claude-code \
  -- python -m interlock.mcp_server
```

Ask Claude to deploy something, and watch it ask first.

---

*Built for Graph Hacks 2026 by WeMakeDevs and FalkorDB. The Diwali-night outage is an imagined scene; the Reddit quotes, the code and the numbers are real. I built Interlock with help from Claude Code, and Sarvam Bulbul v3 narrated the demo video.*
