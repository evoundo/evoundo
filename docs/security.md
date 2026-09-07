# Security Architecture & Safeguards

This document describes the security model, safeguards, and defensive properties implemented in EvoUndo.

---

## 1. Vulnerability Reporting

Please report security vulnerabilities responsibly:

- **Email**: tradertanmay@gmail.com
- **GitHub Security Advisory**: Submit a private advisory via GitHub Security tab.

Please do not disclose vulnerabilities via public issues.

---

## 2. SSRF Protection for External Probes

EvoUndo includes protections for HTTP post-condition probes (`evoundo.probe.HTTPProbe`):

- **Blocked Destinations**: By default, private, loopback, link-local, and cloud metadata IP ranges are strictly rejected (`127.0.0.0/8`, `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`, `169.254.169.254`, IPv6 equivalents, and octal/hex encodings).
- **DNS Rebinding Defense**: DNS resolution is performed and validated prior to dispatching requests.
- **Redirect Loops & Fencing**: Probes reject redirects leading into restricted network segments.

---

## 3. Sensitive Secret Redaction

Mutation witnesses and journal payloads pass through `PayloadProtector` before serialization:

- **Automatic Pattern Matching**: Redacts API keys, bearer tokens, passwords, and private certificates.
- **Custom Redaction Hooks**: Applications can register domain-specific protectors to scrub proprietary tokens or customer PII.

---

## 4. SQL Injection Prevention

All relational drivers (SQLite, PostgreSQL, MySQL, SQL Server) enforce:

- **Strict Identifier Validation**: Column and table names are checked against safe alphanumeric patterns.
- **Dialect Quoting**: Uses dialect-specific quoting (e.g. double quotes, backticks, brackets).
- **Parameterized Queries**: All runtime values in inverses and probes are bound via parameterized placeholders; zero string interpolation is permitted.

---

## 5. Recovery Authorization Hooks

EvoUndo provides pluggable protocols in `evoundo.governance`:

- `RecoveryAuthorizer`: Controls whether a caller is authorized to revert a given mutation.
- `TenantContext`: Fences mutations to ensure multi-tenant boundary isolation.
- `ApprovalProvider`: Hooks for multi-party review workflows on critical operations.

EvoUndo includes local recovery safeguards and does not include organization-level governance or hosted control-plane functionality.
