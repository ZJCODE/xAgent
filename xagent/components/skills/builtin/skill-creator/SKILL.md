---
name: skill-creator
description: Helps create, improve, or validate xAgent Agent Skills. Use when the user asks to make a reusable skill, teach the agent a workflow, or save domain knowledge as a skill.
---

# Skill Creator

Use this skill when the task is to create, refine, or validate an Agent Skill package for xAgent.

## Skill Package Shape

An Agent Skill is a directory under the runtime skills root:

```text
skills/<skill-name>/
  SKILL.md
  references/   optional supporting markdown or text files
  scripts/      optional scripts invoked through normal tools
  assets/       optional static assets
```

`SKILL.md` is required. It must begin with YAML frontmatter containing at least:

```yaml
---
name: example-skill
description: Clear discovery metadata. Say what the skill does and when to use it.
---
```

The directory name and the frontmatter `name` must match. Skill names must match `^[a-z0-9]+(?:-[a-z0-9]+)*$`.

## Progressive Loading

xAgent exposes only enabled skill names and descriptions in the prompt catalog. The full `SKILL.md` is loaded only when the task matches the description. Referenced files are read only when needed.

Keep the description compact and specific. Put detailed workflow rules in the body of `SKILL.md`, not in the description.

## Authoring Workflow

1. Confirm the skill is useful as reusable procedure or domain knowledge, not just a one-off note.
2. Choose a short lowercase hyphenated name.
3. Write a description that includes both capability and trigger conditions.
4. Write body instructions that are operational: inputs to look for, steps to follow, checks to run, and failure modes.
5. Add `references/`, `scripts/`, or `assets/` only when the body genuinely needs them.
6. Validate that `SKILL.md` has frontmatter, name matches directory, and the description is not empty.

## Creating Files

The `read_skill` result includes this skill's `skill_root`. The runtime skills root is the parent directory of that path. When the user wants a new skill saved, use the normal file or shell tooling to create `skills/<skill-name>/SKILL.md` under that parent directory. Do not overwrite an existing skill unless the user explicitly asks.

Skill scripts are not automatically registered as tools. If a skill uses a script, execution still goes through the available command tool and its normal safety policy.