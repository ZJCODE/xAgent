# xAgent Development Guide

## Environment & Build

```bash
# Activate the conda environment
conda activate xagent

# Install in development mode (editable; code changes take effect immediately)
pip install -e .
```

## Code Philosophy

- **Single Responsibility** — A module should do one thing; a function should solve one problem.
- **Explicit Over Implicit** — Avoid magic. Parameters, return values, and side effects should always be predictable.
- **Composition Over Inheritance** — Build capabilities through interfaces and composition; avoid deep inheritance chains.
- **Naming as Documentation** — Good naming eliminates 80% of comments; only unclear logic needs comments.
- **Minimal Dependencies** — Before adding a third-party library, ask: can the standard library solve this?
- **Fail Fast** — Validate boundaries early. Surface errors as soon as possible; never silently swallow exceptions.
- **DRY, But Not Excessively** — Abstract only after something repeats three times; premature abstraction is worse than duplication.
- **Type Annotations** — All public interfaces must use type hints, with Pydantic for runtime validation.
- **Async First** — Use `async/await` for IO-bound operations to keep the event loop responsive.