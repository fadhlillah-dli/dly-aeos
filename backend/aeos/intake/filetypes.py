"""Upload type policy: extension allow-list + content signature consistency (§05 step 5, U-11).

Attachments are never executed. Executable/active content is rejected by extension *and* by
content signature (a renamed ``.exe`` uploaded as ``notes.txt`` is still rejected).
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from pathlib import PurePosixPath

import filetype

TEXT_KINDS = {".md": "markdown", ".markdown": "markdown", ".txt": "text", ".json": "json", ".yaml": "yaml",
              ".yml": "yaml", ".csv": "csv"}
BINARY_KINDS = {
    ".pdf": ("pdf", {"application/pdf"}),
    ".png": ("image", {"image/png"}),
    ".jpg": ("image", {"image/jpeg"}),
    ".jpeg": ("image", {"image/jpeg"}),
    ".gif": ("image", {"image/gif"}),
    ".webp": ("image", {"image/webp"}),
    ".docx": ("docx", {"application/vnd.openxmlformats-officedocument.wordprocessingml.document", "application/zip"}),
    ".zip": ("zip", {"application/zip"}),
}
ALLOWED_EXTENSIONS = tuple(sorted(set(TEXT_KINDS) | set(BINARY_KINDS)))

# Active/executable content — rejected by any suffix, and also as archive members.
ACTIVE_EXTENSIONS = frozenset({
    ".exe", ".dll", ".com", ".scr", ".pif", ".msi", ".msp", ".bat", ".cmd", ".ps1", ".psm1", ".psd1", ".vbs",
    ".vbe", ".js", ".jse", ".mjs", ".cjs", ".wsf", ".wsh", ".hta", ".sh", ".bash", ".zsh", ".csh", ".ksh",
    ".command", ".app", ".pkg", ".dmg", ".jar", ".class", ".py", ".pyc", ".pyw", ".pl", ".rb", ".php", ".lnk",
    ".reg", ".scf", ".url", ".html", ".htm", ".xhtml", ".svg", ".docm", ".xlsm", ".pptm", ".dotm",
    ".so", ".dylib", ".elf", ".deb", ".rpm", ".apk", ".ipa", ".iso", ".vhd", ".vhdx",
    ".appx", ".msix", ".cpl", ".gadget", ".inf", ".ins", ".sct", ".ws", ".application", ".xbap",
})

# Magic numbers of native executables (Windows PE, ELF, Mach-O incl. fat binaries).
_EXEC_MAGIC = (b"MZ", b"\x7fELF", b"\xfe\xed\xfa\xce", b"\xfe\xed\xfa\xcf", b"\xce\xfa\xed\xfe",
               b"\xcf\xfa\xed\xfe", b"\xca\xfe\xba\xbe")
# Strong (multi-byte) signatures that must never be accepted under a text extension.
_STRONG_BINARY_PREFIXES = ("application/", "image/", "video/", "audio/", "font/")
_CTRL = re.compile(r"[\x00-\x1f\x7f]")


class UploadRejected(Exception):
    def __init__(self, reasons: list[str], code: str = "ATTACHMENT_REJECTED"):
        super().__init__("; ".join(reasons))
        self.reasons = reasons
        self.code = code


@dataclass(frozen=True)
class DetectedType:
    extension: str
    kind: str  # markdown|text|json|yaml|csv|pdf|image|docx|zip
    declared_mime: str
    detected_mime: str
    is_text: bool


def sanitize_filename(raw: str | None) -> str:
    """Display name only (never used as a path). Strips directories, control and format chars."""
    name = (raw or "").replace("\\", "/").split("/")[-1]
    name = unicodedata.normalize("NFC", _CTRL.sub("", name))
    # Bidi overrides (Cf) can disguise extensions ("txt.exe" rendered as "exe.txt").
    name = "".join(ch for ch in name if unicodedata.category(ch) != "Cf").strip().strip(".")
    return name[:200] or "upload"


def suffixes(name: str) -> list[str]:
    return [s.lower() for s in PurePosixPath(name).suffixes]


def is_active_name(name: str) -> bool:
    return any(s in ACTIVE_EXTENSIONS for s in suffixes(name))


def detect(filename: str, data: bytes, declared_mime: str = "") -> DetectedType:
    """Validate extension against the allow-list and the content signature. Raises UploadRejected."""
    reasons: list[str] = []
    sfx = suffixes(filename)
    ext = sfx[-1] if sfx else ""
    active = [s for s in sfx if s in ACTIVE_EXTENSIONS]
    if active:
        reasons.append(f"active/executable content is not accepted ({', '.join(active)}); attachments are never executed")
    if ext not in ALLOWED_EXTENSIONS:
        reasons.append(f"file type {ext or '(none)'} is not allowed; allowed: {' '.join(ALLOWED_EXTENSIONS)}")
    if data.startswith(_EXEC_MAGIC):
        reasons.append("content is a native executable; attachments are never executed")
    if reasons:
        raise UploadRejected(reasons)
    if not data:
        raise UploadRejected(["file is empty"])

    guess = filetype.guess(data)
    detected_mime = guess.mime if guess else ""
    if ext in TEXT_KINDS:
        if b"\x00" in data[:65536] and not data.startswith((b"\xff\xfe", b"\xfe\xff")):
            raise UploadRejected([f"extension {ext} declares text but content is binary"])
        try:
            decode_text(data)
        except UploadRejected as exc:
            raise UploadRejected([f"extension {ext} declares text but {exc.reasons[0]}"]) from exc
        # Two-byte signatures (e.g. "BM") collide with ordinary words; only multi-byte binary
        # signatures that cannot be valid prose are treated as a mismatch.
        if guess is not None and guess.mime.startswith(_STRONG_BINARY_PREFIXES) and guess.extension not in {"bmp"}:
            raise UploadRejected([f"extension {ext} does not match detected content type {detected_mime}"])
        if data.lstrip()[:2] == b"#!":
            raise UploadRejected(["script with interpreter line (#!) is active content; attachments are never executed"])
        return DetectedType(ext, TEXT_KINDS[ext], declared_mime, "text/plain", True)

    kind, mimes = BINARY_KINDS[ext]
    if guess is None or detected_mime not in mimes:
        raise UploadRejected([f"extension {ext} does not match detected content type {detected_mime or 'unknown'}"])
    return DetectedType(ext, kind, declared_mime, detected_mime, False)


def decode_text(data: bytes) -> str:
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            return data.decode("utf-16")
        except UnicodeDecodeError as exc:
            raise UploadRejected(["content is not valid UTF-16"]) from exc
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise UploadRejected(["content is not valid UTF-8"]) from exc
