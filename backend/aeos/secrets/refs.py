"""Well-known secret references (logical names only — never values) and what they enable (§26)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class KnownRef:
    ref: str
    integration: str
    purpose: str  # Indonesian description for the UI
    used_by: str
    domain: str | None  # PERSONAL | OFFICE | None (platform/worker)

    @property
    def namespace(self) -> str:
        return self.ref.split("/", 1)[0]

    @property
    def remedy(self) -> str:
        return f"aeos secrets set {self.ref}"


KNOWN_REFS: tuple[KnownRef, ...] = (
    KnownRef("personal/github", "github", "Token GitHub PERSONAL untuk broker (push cabang aeos/*, membuka PR)",
             "broker", "PERSONAL"),
    KnownRef("personal/google-oauth", "google", "Kredensial OAuth Google Drive/Docs (scope drive.file)",
             "publisher", "PERSONAL"),
    KnownRef("workers/openrouter", "openrouter", "Kunci OpenRouter untuk rute pekerja (dipakai broker, bukan sandbox)",
             "broker / routing", None),
    KnownRef("personal/openrouter", "openrouter", "Kunci OpenRouter pribadi (cadangan info kunci/katalog)",
             "routing", "PERSONAL"),
    KnownRef("workers/opencode-zen", "opencode", "Kunci OpenCode Zen untuk rute pekerja", "broker", None),
    KnownRef("workers/gemini", "gemini", "Kunci Gemini API (pembanding Free Tier)", "broker", None),
    KnownRef("office/atlassian", "atlassian", "Token Atlassian OFFICE (Jira/Confluence) - diblokir sampai otorisasi kantor",
             "publisher", "OFFICE"),
    KnownRef("office/bitbucket", "atlassian", "Token Bitbucket OFFICE - diblokir sampai otorisasi kantor",
             "broker", "OFFICE"),
)


def known(ref: str) -> KnownRef | None:
    return next((k for k in KNOWN_REFS if k.ref == ref), None)
