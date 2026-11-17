# Security

## Supported deployment scope

The current release targets a **single trusted local operator**. Bind the main
application and MCP/classifier services to loopback. Do not expose this version
directly to the public Internet or use it with real customer data.

There is no login, registration, RBAC, authenticated customer identity or
multi-tenant isolation. The browser UUID is an anonymous conversation key, not
an identity credential. A separate management menu is not authorization.
Some operations enforce loopback/same-origin access or an explicit `ADMIN_TOKEN`;
that is not a complete security boundary for a public multi-user deployment.
Reverse proxies are not a substitute for authentication and ownership checks.

## Data and external models

- Keep `.env`, database dumps, conversation logs, training data and model artifacts
  out of Git. Review Git history as well as the working tree before publishing.
- Knowledge mining, normalization, evaluation and chat may send text to your
  configured model provider. Configure an appropriate provider and obtain the
  necessary consent before using private data. A paid-action confirmation is not
  itself permission to send another person's data.
- Model outputs and retrieved documents are untrusted. Tool arguments require
  validation; business writes require confirmation and ownership checks.
- Demo tools do not execute real payment refunds or connect a human agent.

## Reporting a vulnerability

Please report security vulnerabilities privately via GitHub Private Vulnerability
Reporting at:

- <https://github.com/yzd05/PawHelp/security/advisories/new>

Include affected files or endpoints, reproduction steps, impact assessment, and
any suggested remediation. Do **not** publish exploit details, live credentials,
or private records in public GitHub issues or pull requests.

If a secret leaks, revoke or rotate it immediately at the provider first. Adding
a `.gitignore` rule or deleting the file in a new commit does not remove the
secret from Git history; history rewriting requires explicit coordination with
maintainers and existing clones.
