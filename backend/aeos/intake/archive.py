"""ZIP inspection without extraction (§05 step 5, §50 "File path traversal/symlink keluar sandbox").

Archives (``.zip`` and the OOXML container of ``.docx``) are inspected from their central directory
only — nothing is written to disk. Rejected: absolute paths, drive letters, ``..`` traversal,
symlink entries, encrypted members, too many members, zip-bomb sizes/ratios, nested archives and
active content members.
"""

from __future__ import annotations

import io
import stat
import zipfile
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from aeos.intake.filetypes import ACTIVE_EXTENSIONS, UploadRejected, suffixes

MAX_RATIO = 100  # uncompressed/compressed per member
RATIO_MIN_BYTES = 1 << 20  # ratios below 1 MiB uncompressed are not suspicious
NESTED_ARCHIVES = frozenset({".zip", ".7z", ".rar", ".gz", ".tgz", ".bz2", ".xz", ".tar", ".cab", ".zst"})
DOCX_MEDIA = frozenset({".svg"})


@dataclass
class ArchiveReport:
    members: int = 0
    total_uncompressed: int = 0
    total_compressed: int = 0
    names: list[str] = field(default_factory=list)
    macro_content: bool = False

    def as_dict(self) -> dict[str, object]:
        return {"members": self.members, "total_uncompressed": self.total_uncompressed,
                "total_compressed": self.total_compressed, "names_sample": self.names[:50]}


def _unsafe_name(name: str) -> str | None:
    n = name.replace("\\", "/")
    if n.startswith("/") or (len(n) > 1 and n[1] == ":") or n.startswith("//"):
        return "absolute path"
    if any(part == ".." for part in PurePosixPath(n).parts):
        return "path traversal (..)"
    if "\x00" in n:
        return "NUL byte in name"
    return None


def _is_symlink(info: zipfile.ZipInfo) -> bool:
    # Unix mode bits live in the high 16 bits of external_attr regardless of the creating OS.
    return stat.S_ISLNK((info.external_attr >> 16) & 0xFFFF)


def inspect_zip(
    data: bytes, *, max_members: int, max_total_uncompressed: int, container: str = "zip",
) -> ArchiveReport:
    """Validate a ZIP container from its central directory. Raises UploadRejected with reasons."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except (zipfile.BadZipFile, OSError, ValueError) as exc:
        raise UploadRejected([f"{container} archive is corrupt or unreadable"]) from exc
    reasons: list[str] = []
    report = ArchiveReport()
    with zf:
        infos = zf.infolist()
        report.members = len(infos)
        if len(infos) > max_members:
            raise UploadRejected([f"archive has {len(infos)} members (limit {max_members})"], code="ARCHIVE_TOO_MANY_MEMBERS")
        for info in infos:
            name = info.filename
            why = _unsafe_name(name)
            if why:
                reasons.append(f"member {name!r}: {why}")
                continue
            if _is_symlink(info):
                reasons.append(f"member {name!r}: symlink entries are not accepted")
                continue
            if info.flag_bits & 0x1:
                reasons.append(f"member {name!r}: encrypted members cannot be scanned")
                continue
            report.total_uncompressed += info.file_size
            report.total_compressed += info.compress_size
            if info.file_size >= RATIO_MIN_BYTES and info.file_size > MAX_RATIO * max(info.compress_size, 1):
                reasons.append(f"member {name!r}: compression ratio exceeds {MAX_RATIO}:1 (zip bomb)")
            if not info.is_dir():
                sfx = suffixes(name)
                if container == "zip" and any(s in ACTIVE_EXTENSIONS for s in sfx):
                    reasons.append(f"member {name!r}: active/executable content is not accepted")
                if container == "zip" and sfx and sfx[-1] in NESTED_ARCHIVES:
                    reasons.append(f"member {name!r}: nested archives are not accepted")
                if container == "docx" and PurePosixPath(name).name.lower() in {"vbaproject.bin", "vbadata.xml"}:
                    report.macro_content = True
                    reasons.append("document contains macros (vbaProject); active content is not accepted")
                if container == "docx" and (any(s in ACTIVE_EXTENSIONS - DOCX_MEDIA for s in sfx)
                                            or name.lower().startswith("word/activex/")):
                    # embedded executables/scripts or ActiveX controls inside the OOXML package
                    # (SVG images are ordinary Word media and are never rendered by AEOS)
                    reasons.append(f"document member {name!r}: embedded active content is not accepted")
                report.names.append(name)
        if report.total_uncompressed > max_total_uncompressed:
            reasons.append(
                f"archive expands to {report.total_uncompressed} bytes (limit {max_total_uncompressed}; zip bomb)")
    if reasons:
        code = "ARCHIVE_UNSAFE"
        if any("zip bomb" in r for r in reasons):
            code = "ARCHIVE_BOMB"
        raise UploadRejected(reasons[:20], code=code)
    return report
