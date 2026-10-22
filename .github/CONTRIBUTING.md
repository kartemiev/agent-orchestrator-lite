# Contributing to agent-orchestrator-lite

Thanks for helping! Here is the shortest path to a useful contribution.

## Before you start

- Read [`AGENTS.md`](../AGENTS.md) — it describes the `ExecutionPlan` JSON
  format and the DAG rules. This applies to code, JSON plans and docs alike.
- Check the live system before changing code or plans:
  `python3 scripts/agent-cli.py list --since 24h`.
- Keep documentation bilingual: every change to a `*.md` file should be
  mirrored in its `*.ru.md` counterpart.

## Reporting bugs

Use the **Bug report** template. A good report includes:

1. Steps to reproduce.
2. Expected vs actual behaviour.
3. The `workflow_id` of the failed sprint.
4. `python3 scripts/agent-cli.py diagnose <workflow_id>` output.
5. Redacted logs (`docker logs` / `agent-cli.py logs`) — **no secrets**.

## Proposing features

Use the **Feature request** template and, if possible, attach a draft
`ExecutionPlan` JSON following `AGENTS.md`.

## Code changes

1. Fork the repository and create a branch: `feature/<name>`.
2. Make your changes.
3. Validate locally:

   ```bash
   python3 -m json.tool examples/*.json
   docker compose config -q
   ```

4. Open a Pull Request and fill in the checklist.

## Rules

- Never commit secrets (`.env`, `GITHUB_TOKEN`, `LLM_API_KEY`,
  `OPENHANDS_SESSION_API_KEY`).
- Follow the existing kebab-case naming and the `docs/` layout.
- Run `python3 scripts/agent-cli.py diagnose <workflow_id>` to understand
  failures before guessing.

## Questions?

Open a discussion or an issue with the "question" label.
