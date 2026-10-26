"""G1 secret scan over tracked files (master §31). Uses the same credential patterns as runtime
redaction. Exit 1 when a likely secret is found. Runs without network or third-party tools."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from aeos.core.redaction import _PATTERNS  # noqa: E402

# Files that legitimately contain pattern-shaped examples (tests of the redactor itself).
ALLOW = {
    "tests/unit/test_core.py",
    "tests/unit/test_identity.py",
    "backend/aeos/core/redaction.py",
    "scripts/secret_scan.py",
}
BINARY_SUFFIXES = {".png", ".jpg", ".ico", ".docx", ".sqlite3", ".woff", ".woff2", ".lock"}

# Token-shaped patterns (no capture groups) are high confidence; the generic "key=value" runtime
# redaction rule is too noisy for source code, so source uses a literal-string assignment check.
HIGH_CONFIDENCE = [p for p in _PATTERNS if not p.groups]
# Token-shaped literals inside tests/ are accepted only when they are visibly fake.
FAKE_MARKERS = ("test", "unit", "fake", "example", "dummy", "shouldnot", "abcdefgh", "0123456789", "secretinurl",
                "refreshed", "xxxx")
LITERAL_ASSIGNMENT = re.compile(
    r"(?i)\b(api[_-]?key|secret[_-]?key|client[_-]?secret|access[_-]?token|refresh[_-]?token)\b\s*[:=]\s*"
    r"[\"']([^\"'\s]{16,})[\"']"
)


def tracked_files() -> list[str]:
    out = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True).stdout
    return [line for line in out.splitlines() if line]


# §06 table: blueprints/ and contracts/ (and the docs) are generic schemas/templates with no sensitive
# data. Personal identifiers (aeos.core.pii) and tenant/company hosts there fail G1 unless allow-listed.
PII_SCOPES = ("blueprints/", "contracts/", "docs/")
TENANT_HOST = re.compile(r"(?i)\b[a-z0-9-]+\.(?:atlassian\.net|jira\.com|bitbucket\.org/[a-z0-9-]+|sharepoint\.com)\b")
# No AI-assistant attribution address is allowed: commits and files carry no attribution lines
# (tests/unit/test_repository_hygiene.py), so such an address in the scanned trees is a finding.
PII_ALLOW_VALUES = ("example.com", "example.org", "example.net", "example.invalid", "@users.noreply.github.com",
                    "your-tenant.atlassian.net", "<tenant>.atlassian.net",
                    "company.atlassian.net", "example.atlassian.net")


def pii_findings(rel: str, text: str) -> list[str]:
    from aeos.core.pii import redact_pii

    out: list[str] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        scrubbed = line
        for allowed in PII_ALLOW_VALUES:
            scrubbed = re.sub(r"[\w.+-]*" + re.escape(allowed), "", scrubbed, flags=re.I)
        redacted = redact_pii(scrubbed)
        if redacted != scrubbed:
            kinds = sorted(set(re.findall(r"\[PII:([A-Z]+)\]", redacted)))
            out.append(f"{rel}:{lineno}: personal identifier ({', '.join(kinds)})")
        if TENANT_HOST.search(scrubbed):
            out.append(f"{rel}:{lineno}: tenant/company host")
    return out


def main() -> int:
    findings: list[str] = []
    for rel in tracked_files():
        if rel.startswith(PII_SCOPES) and Path(rel).suffix not in BINARY_SUFFIXES:
            try:
                findings += pii_findings(rel, (ROOT / rel).read_text(encoding="utf-8"))
            except (UnicodeDecodeError, FileNotFoundError):
                pass
        if rel in ALLOW or Path(rel).suffix in BINARY_SUFFIXES or rel.endswith("package-lock.json"):
            continue
        path = ROOT / rel
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, FileNotFoundError):
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            for pat in HIGH_CONFIDENCE:
                for m in pat.finditer(line):
                    if rel.startswith("tests/") and any(k in m.group(0).lower() for k in FAKE_MARKERS):
                        continue
                    findings.append(f"{rel}:{lineno}: matches {pat.pattern[:40]}...")
            m = LITERAL_ASSIGNMENT.search(line)
            if m and not any(w in m.group(2) for w in ("example", "placeholder", "changeme", "xxx", "<", "${")):
                findings.append(f"{rel}:{lineno}: literal credential assignment")
    if findings:
        print("Possible secrets found:")
        print("\n".join(findings))
        return 1
    print(f"secret scan: {len(tracked_files())} tracked files, no findings")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
