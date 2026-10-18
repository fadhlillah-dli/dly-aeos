# ADR-0021 — Intake: allow-list, archive inspection without extraction, isolated parser

* Status: ACCEPTED
* Date: 2026-10-18
* Deciders: owner (Fadhlillah); implemented by the Intake module

## Context

U-11: every intake surface supports uploads including `.md`. §05 step 5: the system reads type and
size, scans input, isolates the parser and records provenance and classification; archive
traversal, symlinks out of the workspace and active files are never executed. §25: attachments
cannot change goal, policy, permission, memory or routing; parser isolation is a minimum control.

## Decision

1. **Allow-list** `.md .markdown .txt .pdf .png .jpg .jpeg .gif .webp .docx .json .yaml .yml .csv
   .zip`. Any suffix in the active-content list (`.exe .dll .bat .cmd .ps1 .sh .js .msi .app .jar
   .py .html .svg .docm …`, including double extensions) and any native-executable signature (PE,
   ELF, Mach-O) is rejected. Text extensions must decode as UTF-8/UTF-16 without NUL bytes and must
   not carry a strong binary signature or a `#!` interpreter line; binary extensions must match the
   `filetype` signature.
2. **Size**: the request `Content-Length` is checked before the multipart body is parsed (411/413),
   and the file itself against `settings.max_upload_bytes`.
3. **Archives** (`.zip` and the `.docx` container) are inspected from the central directory only:
   absolute/drive paths, `..`, symlink entries (`external_attr`), encrypted, nested-archive and
   active members, more than `settings.max_archive_members`, per-member ratio > 100:1 (≥ 1 MiB) or
   total expansion > max(10 × upload limit, 64 MiB) are rejected. Macro parts (`vbaProject.bin`),
   ActiveX parts (`word/activeX/`) and embedded members with an active suffix reject a `.docx`
   (SVG media inside a `.docx` is ordinary Word media and is accepted; AEOS never renders it).
4. **Storage**: `DataLayout.attachments(domain, project_key)/<uuid>.blob`; the user filename is a
   sanitised, redacted display name only. sha256, uploader, time, source and classification (never
   below the project's) are recorded, and a `context_id` must reference a PlanningContext of the same
   project. Text blobs containing secrets are stored redacted; when the text extracted from a binary
   (pdf/docx/zip) contains a secret, the original binary is replaced by the redacted extracted text
   (`parse_detail.original_discarded`; sha256 still names the original upload). Files written for an
   upload whose recording fails are removed.
5. **Scanning**: `contains_secret` → `FLAGGED` + redaction; instruction-injection markers (English
   and Indonesian) → `UNTRUSTED_INSTRUCTION` findings. Attachments are always `trusted=false`; a
   finding is data for review and never authority. Content that could not be inspected is
   `UNSCANNED`, never `CLEAN`.
6. **Parser isolation**: a fresh `python -I parse_worker.py <in> <out> <kind> <max_chars>` per
   file (the worker imports nothing from `aeos`), private temp cwd, minimal environment (no AEOS or
   credential variables), wall-clock timeout (30 s), process-tree RSS cap polled with psutil on
   every OS plus RLIMIT_AS/CPU/FSIZE on POSIX, output-size cap (8 MiB, also polled while the
   worker runs because Windows has no RLIMIT_FSIZE), process-tree kill on any violation.
   md/txt/json/yaml(safe_load)/csv/docx(python-docx, body + tables + headers/footers)/pdf(pypdf)
   are extracted; images are stored with metadata only (no OCR); zip yields a member listing only.
   Worker error messages and metadata are treated as untrusted (they can quote file content) and
   are redacted before they are persisted.
7. Rejections are audited and committed before the error response. Deleting attachments is not
   exposed (retention policy).

## Consequences

* New dependency `pypdf`. The worker is invoked by file path so it works from an editable install,
  a wheel or a source checkout.
* Python `-I` + psutil limits are process isolation, not a sandbox; a stronger boundary (container
  /VM) for hostile documents remains a future option (§25 "Container standar tidak dianggap
  jaminan isolasi kuat").
