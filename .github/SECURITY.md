# Security Policy

## Reporting a vulnerability

Please do **not** open a public issue for security vulnerabilities.

Report them privately instead. Use GitHub's private reporting — **Security →
Report a vulnerability** on the repository — or email the maintainers
directly.

We aim to acknowledge reports within 48 hours and to publish a fix as soon as
a patch is ready and tested.

## Scope

The most sensitive surface is the Gateway API — in particular
`POST /api/v1/plan/direct`, which starts agents and therefore must never be
reachable anonymously. See
[`docs/deployment/securing-with-npm-and-cloudflare.md`](docs/deployment/securing-with-npm-and-cloudflare.md).

Out of scope: issues that require an attacker to already have local access to
the host or to the `.env` file.

## Secrets

- `GITHUB_TOKEN`, `LLM_API_KEY` and `OPENHANDS_SESSION_API_KEY` must live only
  in `.env` / secret storage — never in a plan, a URL or a log.
- If you believe a secret has leaked, rotate it immediately.

## Supported versions

Only the latest `main` branch is supported.
