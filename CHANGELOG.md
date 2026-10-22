# Changelog

## [Unreleased]
### Added
- Optional Notifier module (`orchestrator/notifier.py`) with `ntfy` push notifications and `Resend` HTML email reports using Fire-and-Forget execution.
- Provider-agnostic Git integration (`orchestrator/git_provider.py`) supporting GitHub PRs and GitLab MRs (including self-hosted instances and nested subgroups).

## [v1.0.0] - 2026-10-22
### Added
- Initial public release of `agent-orchestrator-lite`.
- DAG Orchestration engine with Temporal workflows, OpenHands agent execution, and Streamlit HITL approval gate.
- Interactive CLI diagnostics (`agent-cli.py`).
- Comprehensive documentation: tutorials (`notification-service`, `self-update`, `feature-bundling`), blueprints, and production deployment guide (NPM + Cloudflare Access).
