# EvoUndo Security Policy

## Reporting Security Issues

The security of agent recovery and state mutation is important to the EvoUndo project.

If you discover a security vulnerability, please report it privately:

- **Email:** tradertanmay@gmail.com
- **GitHub Security Advisory:** Use the repository's Security tab to submit a private vulnerability report.

Please do not disclose suspected vulnerabilities through public GitHub issues.

We will review reports as promptly as possible and coordinate responsible disclosure when appropriate.

---

## Security Safeguards

### SSRF Protection for External Probes

EvoUndo includes protections for HTTP post-condition probes such as `evoundo.probe.HTTPProbe`.

The probe layer is designed to reject unsafe destinations, including private, loopback, link-local, and other restricted network addresses.

It also performs checks intended to reduce risks from DNS rebinding, unsafe redirects, and alternative IP-address representations.

### Sensitive Data Redaction

Mutation witnesses and journal payloads can pass through `PayloadProtector` before persistence.

The default protection layer supports redaction of common sensitive fields such as:

- API tokens
- passwords
- bearer credentials
- authentication keys

Applications may provide custom `PayloadProtector` implementations for domain-specific data handling.

### SQL Safety

Supported SQL recovery drivers use defensive handling for identifiers and values.

Safeguards include:

- identifier validation
- dialect-appropriate identifier quoting
- parameterized query values
- validation of recovery operations before execution

### Recovery Authorization

EvoUndo provides generic authorization hooks that applications can use to control whether a recovery operation is permitted.

EvoUndo includes local recovery safeguards and does not include organization-level governance or hosted control-plane functionality.

---

## Recovery Safety Properties

EvoUndo is designed to fail safely when recovery cannot be verified.

Core safeguards include:

- conflict detection before overwriting newer state
- blocking unsupported recovery of irreversible actions
- duplicate-execution suppression for supported retry scenarios
- post-recovery state verification
- explicit failure when recovery cannot be safely completed
- memory invalidation when recovered state makes prior agent memory stale
- compensation handling for actions that cannot be literally undone

These safeguards reduce recovery risk but do not eliminate the need for application-specific authorization, testing, backups, or operational controls.

---

## Supported Use

Security behavior depends on the specific driver, integration, and external system being used.

Users should validate EvoUndo in their own environment before relying on recovery behavior for critical workloads.

Security-sensitive deployments should also apply appropriate:

- access controls
- credential management
- network restrictions
- backups
- monitoring
- application-specific validation

---

## Disclosure

Please avoid publicly sharing proof-of-concept exploits or sensitive technical details before a fix or mitigation is available.

We appreciate responsible disclosure and reports that help improve EvoUndo.