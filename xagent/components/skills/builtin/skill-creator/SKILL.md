---
name: skill-creator
description: Helps create, improve, or validate xAgent Agent Skills. Use when the user asks to make a reusable skill, teach the agent a workflow, or save domain knowledge as a skill.
---

# Skill Creator

Use this skill to create, refine, or validate an xAgent Agent Skill.

## Skill Package Shape

An Agent Skill is a directory under the runtime skills root:

```text
skills/<skill-name>/
  SKILL.md
  references/   optional supporting markdown or text files
  scripts/      optional scripts invoked through normal tools
  assets/       optional static assets
```

`SKILL.md` is required and must start with:

```yaml
---
name: example-skill
description: Clear discovery metadata. Say what the skill does and when to use it.
---
```

Directory name and frontmatter `name` must match `^[a-z0-9]+(?:-[a-z0-9]+)*$`.

## Progressive Loading

xAgent exposes only enabled skill names and descriptions in the prompt catalog. Full instructions load only after the skill matches. Keep descriptions compact; put workflow rules in the body.

## Authoring Workflow

1. Confirm the skill is reusable procedure or domain knowledge, not a one-off note.
2. Choose a short lowercase hyphenated name.
3. Write a description that includes both capability and trigger conditions.
4. Write operational body instructions: inputs, steps, checks, and failure modes.
5. Add `references/`, `scripts/`, or `assets/` only when needed.
6. Validate that `SKILL.md` has frontmatter, name matches directory, and the description is not empty.

## Creating Files

The `read_skill` result includes this skill's `skill_root`; its parent is the runtime skills root. To save a new skill, create `skills/<skill-name>/SKILL.md` there. Do not overwrite an existing skill unless explicitly asked.

Skill scripts are not automatically registered as tools. If a skill uses a script, execution still goes through the available command tool and its normal safety policy.
