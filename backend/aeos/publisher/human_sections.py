"""Keep human-written HUMAN_GOVERNED content when AEOS updates an official document (§28, §29).

A Google Doc / Confluence page is updated by replacing its body with the newly approved render. Sections
whose content type is ``HUMAN_GOVERNED`` (sign-off, approval, residual risk, decisions ...) are filled by
authorised humans directly in the provider and can never be reproduced by the renderer, so a blind body
replacement would delete them. This is NOT a server-side auto merge: before the write, the provider reads
the current external text, splits it by the document type's (unique) section headings and carries every
line of a HUMAN_GOVERNED section that AEOS did not write itself into the new body, under an explicit marker.
If a human section that AEOS published before can no longer be located, the update is refused
(``HumanContentUnlocatable`` → conflict, needs review) instead of risking the loss.
"""

from __future__ import annotations

import html
import re
from typing import Any

MARKER = "Konten HUMAN_GOVERNED dari dokumen eksternal (ditulis manusia; dipertahankan AEOS, bukan isi AEOS):"
_CONTENT_TYPE_LINE = "Content type: HUMAN_GOVERNED"


class HumanContentUnlocatable(Exception):
    """A HUMAN_GOVERNED section published earlier cannot be found in the external document."""


def _key(line: str) -> str:
    return re.sub(r"[\W_]+", "", line.lower())


def _plain(line: str) -> str:
    s = line.strip()
    s = re.sub(r"^#{1,6}\s+", "", s)
    s = re.sub(r"^>\s?", "", s)
    s = re.sub(r"^([-*]|\d+[.)])\s+", "", s)
    s = re.sub(r"\*\*([^*]+)\*\*", r"\1", s)
    s = re.sub(r"`([^`]+)`", r"\1", s)
    s = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1 (\2)", s)
    return s.strip().strip("_").strip()


def markdown_sections(markdown: str, titles: list[str]) -> dict[str, list[str]]:
    """``## <title>`` blocks of an AEOS render → their plain lines (heading excluded)."""
    wanted = set(titles)
    out: dict[str, list[str]] = {}
    current: str | None = None
    for raw in markdown.replace("\r\n", "\n").split("\n"):
        m = re.match(r"^##\s+(.*)$", raw.strip())
        if m:
            current = m.group(1).strip() if m.group(1).strip() in wanted else None
            if current is not None:
                out.setdefault(current, [])
            continue
        if current is None or not raw.strip() or raw.strip().startswith("<!--") or raw.strip().startswith("```"):
            continue
        text = _plain(raw)
        if text:
            out[current].append(text)
    return out


def storage_to_text(storage: str) -> str:
    """Confluence storage XHTML → one line per block element (for section splitting only)."""
    s = re.sub(r"(?i)<br\s*/?>", "\n", storage)
    s = re.sub(r"(?i)</(h[1-6]|p|li|tr|td|th|blockquote|div)>", "\n", s)
    s = re.sub(r"<[^>]+>", "", s)
    return html.unescape(s)


def external_sections(text: str, titles: list[str]) -> dict[str, list[str]]:
    """External plain text split at the document's section headings (titles are unique per document type and
    appear in spec order). Scanning starts at the first section heading, so preamble lines (the metadata
    table, e.g. a cell "Owner") are never mistaken for a heading; afterwards only a LATER title opens a section."""
    order = [t.strip() for t in titles]
    out: dict[str, list[str]] = {}
    idx = -1
    for raw in text.replace("\r\n", "\n").split("\n"):
        s = raw.strip()
        if not s:
            continue
        nxt = next((i for i in range(idx + 1, len(order)) if order[i] == s), None)
        if nxt is not None and (idx >= 0 or nxt == 0):
            idx = nxt
            out.setdefault(titles[idx], [])
            continue
        if idx >= 0:
            out[titles[idx]].append(s)
    return out


def guard_for(spec_sections: list[tuple[str, str]], previous_markdown: str | None, *,
              human_content_possible: bool) -> dict[str, Any] | None:
    """Payload guard for an update: section titles in order, which are HUMAN_GOVERNED, the plain lines AEOS itself
    published in those sections last time (never mistaken for human content), and whether the external document
    can hold human content at all — only after a human edit was acknowledged (its revision became the baseline)
    or an earlier update already carried human lines. A document only ever written by AEOS has none to lose."""
    titles = [t for t, _ct in spec_sections]
    human = [t for t, ct in spec_sections if ct == "HUMAN_GOVERNED"]
    if not human:
        return None
    prev = markdown_sections(previous_markdown or "", human) if previous_markdown else {}
    return {"titles": titles, "human": human, "previous": {t: prev.get(t, []) for t in human},
            "human_content_possible": bool(human_content_possible)}


def preserve(markdown: str, external_text: str, guard: dict[str, Any] | None) -> tuple[str, dict[str, list[str]]]:
    """Return ``markdown`` with the human-written lines of every HUMAN_GOVERNED section carried over from the
    external document, plus ``{section title: kept lines}``. Raises ``HumanContentUnlocatable``."""
    if not guard or not guard.get("human_content_possible"):
        return markdown, {}
    titles, human = list(guard.get("titles") or []), list(guard.get("human") or [])
    previous = guard.get("previous") or {}
    ext = external_sections(external_text, titles)
    new = markdown_sections(markdown, human)
    kept: dict[str, list[str]] = {}
    for title in human:
        if title not in ext:  # every AEOS render carries every section: a missing heading was removed or unreadable
            raise HumanContentUnlocatable(
                f"HUMAN_GOVERNED section {title!r} cannot be located in the external document; human-governed "
                "content would be lost — not overwritten, needs review")
        known = {_key(x) for x in [*previous.get(title, []), *new.get(title, []), MARKER, _CONTENT_TYPE_LINE]}
        known.discard("")
        additions = [line for line in ext[title] if _key(line) and _key(line) not in known]
        if additions:
            kept[title] = additions
    return (_splice(markdown, kept) if kept else markdown), kept


def _splice(markdown: str, kept: dict[str, list[str]]) -> str:
    lines = markdown.replace("\r\n", "\n").split("\n")
    out: list[str] = []
    i = 0
    while i < len(lines):
        out.append(lines[i])
        m = re.match(r"^##\s+(.*)$", lines[i].strip())
        title = m.group(1).strip() if m else None
        i += 1
        if title not in kept:
            continue
        while i < len(lines) and not re.match(r"^##\s+", lines[i].strip()):
            out.append(lines[i])
            i += 1
        while out and not out[-1].strip():
            out.pop()
        out += ["", f"_{MARKER}_", ""] + [f"> {line}" for line in kept[title]] + [""]
    return "\n".join(out)
