# Security Policy

Security is very important for Fetch.ai and its community. This document outlines security procedures and general policies for the AEVS SDK — a security-sensitive library that produces tamper-evident receipts for AI agent tool calls and signs them with secrets held by the host process.

## How to Report

Please follow the steps below to report a security issue:

- Describe the issue clearly with reference to the underlying source code, and indicate whether the bug is **Critical** or **Non-critical**.
- Attach all information needed to reproduce the bug in a test environment (proof-of-concept code, payloads, environment).
- Include the SDK version (`aevs.__version__`), the Python version, and any other relevant system information.
- Include suggested solutions or mitigations if known.
- Send the email to [aevs@fetch.ai](mailto:aevs@fetch.ai) and start the subject with your classification **Critical** or **Non-critical** followed by a short title of the bug.

If you prefer encrypted communication, request our PGP key in your first message and we will reply with it.

The Fetch team will review your information and confirm the classification.

## Disclosure Policy

When the security team receives a report, they will assign it to a primary handler who coordinates the fix and release process:

- Confirm the problem and determine the affected versions.
- Audit related code paths to find any similar issues.
- Prepare fixes for all releases still under maintenance, released to PyPI as quickly as possible.

For **non-critical** bugs, the team will create an issue or pull request so reporters can follow progress on the fix.

For **critical** bugs (e.g. ones that compromise receipt integrity, leak signing secrets, or enable chain forgery), the patched version is deployed before the exploit is acknowledged publicly. Critical bugs and their fixes are shared after the code is patched, to prevent targeting of unpatched exploits.

## Scope

In scope:

- The SDK source under `src/aevs/`, its public API, and the cryptographic primitives in `aevs.crypto` (HKDF, HMAC, hash chain).
- Receipt construction, signing, buffering, and transport in `aevs.core`.
- Adapters in `aevs.adapters` (LangChain, MCP) for issues that compromise receipt integrity, leak secrets, or crash the host agent.
- Build and release artifacts published to PyPI under the `aevs` name.

Out of scope:

- Vulnerabilities in third-party dependencies — please report upstream; we pick up fixes via dependency bumps.
- The AEVS backend service is **not** part of this repository. Send backend reports to [aevs@fetch.ai](mailto:aevs@fetch.ai) with `[AEVS Backend]` in the subject.
- Issues that require a compromised host process or local filesystem access — the SDK trusts the process it runs in by design.

## Public Discussions

Please refrain from publicly discussing a potential security vulnerability. It's better to discuss privately first and limit the potential impact as much as possible.

## Comments on this Policy

If you have suggestions on how this process could be improved, please submit a pull request.

---

Thanks for your help!
