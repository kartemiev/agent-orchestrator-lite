# Changelog

## [Unreleased]

## [v1.1.0] - 2026-10-22
### Added
- Optional Notifier module (`orchestrator/notifier.py`) with `ntfy` push notifications and `Resend` HTML email reports using Fire-and-Forget execution.
- Provider-agnostic Git integration (`orchestrator/git_provider.py`) supporting GitHub PRs and GitLab MRs (including self-hosted instances and nested subgroups).

### Fixed
- Resolved security issue with Git token leakage in `.git/FETCH_HEAD` by migrating to `http.extraHeader` authorization.
- Fixed HITL approval state reset race condition in Temporal workflows.
- Synchronized environment variable contracts for OpenHands and LLM base URLs.
- Replaced hardcoded branch defaults with dynamic default branch detection.

## [v1.0.0] - 2026-10-22
### Added
- Initial public release of `agent-orchestrator-lite`.
- DAG Orchestration engine with Temporal workflows, OpenHands agent execution, and Streamlit HITL approval gate.
- Interactive CLI diagnostics (`agent-cli.py`).
- Comprehensive documentation: tutorials (`notification-service`, `self-update`, `feature-bundling`), blueprints, and production deployment guide (NPM + Cloudflare Access).
