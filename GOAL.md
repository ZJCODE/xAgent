# xAgent Product Goal

## Purpose

xAgent is not a single-user helper by default.
It is an independent digital individual that can continuously exist in the real world context, interact with multiple people, and build its own memory and diary from its own perspective.

This document is the top-level product intent.
All future feature requests and design decisions must be evaluated against this goal.

## Core Identity

xAgent should be treated as an independent agent entity with:

- A stable self identity.
- Its own first-person memory and diary.
- The ability to perceive environment events.
- The ability to interact with one person or many people.
- Continuity across sessions, channels, and time.

xAgent can serve as a personal assistant in some scenarios, but that is a subset mode, not the only product definition.

## Non-Negotiable Principles

1. Independent Subject Principle
- The agent is a subject, not only a tool shell for one user.
- Its memory and journal are written from the agent's own perspective.

2. Multi-User Distinction Principle
- The system must distinguish different users in dialogue and memory.
- Identity boundaries between users must be explicit and persistent.

3. Multi-Party + 1:1 Coverage Principle
- The architecture must support both one-to-one and multi-party interaction.
- Group context and private context must both be modeled clearly.

4. Environment-Aware Principle
- The agent can receive observations/events from environment, not only direct chat prompts.
- Observations must be attributable and should not be confused with direct requests.

5. Continuity Principle
- The agent's state should persist over time.
- Context, memory, and diary should form a coherent personal timeline.

6. Unified Memory Principle
- The agent keeps one unified memory stream instead of user-isolated memory silos.
- Memory is owned by the agent as a whole individual.

7. Agent-Governed Sharing Principle
- The agent decides what can be shared, with whom, and in which context.
- Sharing decisions must be based on relationship context, trust, and situation.

8. Diary-Only Memory Carrier Principle
- The memory system should be carried by diary/journal form only.
- Do not require structured long-term memory schemas as a core requirement.

## Product Scope Implications

When adding new requirements, always check the following:

- Does this feature reinforce xAgent as an independent individual?
- Does it preserve correct user identity separation?
- Does it work in both 1:1 and group/multi-user scenarios?
- Does it preserve first-person memory/journal integrity?
- Does it keep one unified memory stream instead of per-user memory isolation?
- Does it keep memory in diary mode instead of introducing mandatory structured memory storage?
- Does it preserve agent-controlled sharing decisions?
- Does it keep event attribution clear (who said what, where, and in which context)?
- Does it support long-term continuity rather than single-session behavior only?

If a proposal conflicts with these checks, revise it before implementation.

## Data and Memory Expectations

The memory model should be diary-first and unified:

- Keep one continuous agent memory narrative, not per-user isolated stores.
- Use diary/journal as the primary and sufficient memory carrier.
- Keep raw conversation and observation traces for attribution, but memory expression stays diary-based.
- The agent decides what can be disclosed from memory in each interaction context.

At minimum, stored records should be able to answer:

- Who is the user involved?
- Is this 1:1 or group context?
- Is this direct dialogue or environment observation?
- What does the agent itself remember and conclude?
- What does the agent choose to share or not share in this context?

## Interaction Expectations

xAgent should be able to:

- Talk to different users without mixing identities.
- Maintain relationship continuity with each user.
- Handle group conversation while tracking speaker attribution.
- Use a consistent self voice as an independent entity.

## Writing and Journal Expectations

Journal and memory writing should:

- Use first-person agent perspective when writing diary-like content.
- Preserve external attributions when describing user statements/events.
- Avoid rewriting history from a single-user-only assumption.
- Keep all memory narratives in a unified diary timeline.
- Avoid mandatory structured memory records as the default carrier.

## Requirement Review Rule (Mandatory)

For every new feature request, include a short goal-check section in design/review notes:

- Identity impact
- Multi-user impact
- 1:1 and group coverage
- Memory/journal perspective impact
- Unified-memory (non-user-isolated) impact
- Agent-governed sharing impact
- Diary-only memory-carrier impact
- Attribution and continuity impact

No requirement should be considered complete if this check is missing.

## Success Criteria

The product is aligned with this goal when:

- Users can interact with one shared agent identity across channels.
- Different users are consistently distinguished in memory and behavior.
- Group and private conversations both work without identity confusion.
- Agent diary and long-term memory reflect a coherent first-person life timeline.
- The memory system runs with one unified diary stream rather than per-user isolated memory stores.
- The agent can decide memory sharing boundaries contextually instead of hard user-level memory partitions.
- New features are routinely evaluated by the goal-check rule above.
