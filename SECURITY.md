# Security policy

## Supported versions

| Version | Supported |
|---|---|
| 0.1.x | yes |
| older than 0.1.0 (unreleased builds of `main`) | no: update to the latest 0.1.x release |

Security fixes go into the latest 0.1.x release. Nothing is backported to unreleased builds.

## Reporting a vulnerability

**Do not report vulnerabilities in public issues, discussions or pull requests.**

Report them privately through GitHub private vulnerability reporting:

1. Open the repository on GitHub and go to **Security**.
2. Choose **Report a vulnerability**. The direct link is
   <https://github.com/fadhlillah-dli/dly-aeos/security/advisories/new>.
3. Fill in the form. Only you, the repository owner and anyone the owner adds to the advisory can
   see the report.

If you cannot use that form, open a public issue that only asks the owner
([@fadhlillah-dli](https://github.com/fadhlillah-dli)) for a private channel. Put no details of the
problem in that issue.

## What to include

- The affected version (release tag or commit id) and host: Windows, macOS on Apple Silicon or
  macOS on Intel.
- The affected component, for example a module path under `backend/aeos/`, a Control Center page, a
  CLI command, a bootstrap script, a sandbox image definition or the CI workflow.
- What an attacker can do. Say which boundary is crossed, for example authentication, CSRF or
  origin checks, the PERSONAL/OFFICE domain separation, the worker sandbox, the capability broker,
  the independent verifier, decision binding, or secret handling.
- Steps to reproduce, and a minimal proof of concept if you have one.
- Whether the problem has been disclosed anywhere else.

Never include real secrets, tokens, passwords, personal data or company data in a report. Use
obviously fake values.

## Scope

**In scope:** the code and configuration in this repository. That covers the control-plane backend,
the Control Center frontend, the CLI, the bootstrap and launcher scripts, the sandbox image
definitions, the install manifest and policy files, and the CI workflow.

**Out of scope:**

- Third-party services and software, such as GitHub, the Claude Code client, OpenRouter, OpenBao,
  Docker, OpenCode, Google and Atlassian. Report problems in those to their vendors. If DLY AEOS uses
  one of them in an unsafe way, that part is in scope.
- Vulnerabilities in dependencies that DLY AEOS does not reach in an exploitable way. Report those
  upstream.
- Attacks that need an already compromised operating-system account on the host. The exception is
  when a DLY AEOS boundary (sandbox, broker, verifier, authentication) should have stopped the
  attack.
- Social engineering, denial of service against GitHub, and automated scanner output without a
  demonstrated impact.

DLY AEOS is source-available for viewing only. [LICENSE](LICENSE) gives no right to run, copy or
modify the software. Reports based on reading the source are welcome. If you need to run DLY AEOS to
confirm a finding, ask for permission in the private report first.

## Coordinated disclosure

- Keep the details private until a fix is released, or until you and the owner agree on a
  disclosure date.
- The owner confirms the problem in the private advisory, fixes it and publishes a GitHub security
  advisory with the release that contains the fix.
- If you want credit, say so. You will be credited in the advisory.
- There is no bug bounty.

## Response

DLY AEOS is maintained by one person. Reports are handled on a best-effort basis, and no response or
fix time is guaranteed. Progress is posted in the private advisory thread.
