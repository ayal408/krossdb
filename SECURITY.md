# Security Policy

## Reporting a Vulnerability

If you discover a security vulnerability in krossdb, please **do not** open a
public issue. Instead, use GitHub's private vulnerability reporting:

1. Go to the [Security tab](../../security/advisories/new) of this repository.
2. Click "Report a vulnerability" and fill in the details.

You should receive an acknowledgement within a few days. Once a fix is
available, we'll coordinate disclosure and credit you in the release notes
(unless you'd prefer to stay anonymous).

## Scope

krossdb translates driver-level errors and builds parameterized queries, but
it does not sanitize or validate application-level data beyond what Pydantic
model definitions enforce — that responsibility stays with the application.
Vulnerabilities of particular interest:

- Any path where user-controlled input could reach a query as a raw string
  fragment instead of a bound parameter.
- Sensitive data (credentials, tokens) leaking into logs despite the
  masking in `krossdb.core.logging`.
- Multi-tenancy isolation bypasses in `krossdb.multitenancy.context`.

## Supported Versions

This project is pre-1.0; only the latest published release receives security
fixes.
