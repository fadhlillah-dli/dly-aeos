# RFC — Langkah keputusan manusia dan penanda OFFICE

<!-- aeos:logical_id=03-rfcs/rfc-f3 renderer=4 -->

| Metadata | Nilai |
|---|---|
| Logical doc ID | `03-rfcs/rfc-f3` |
| Project / domain | DLY AEOS (`dly-aeos`) / PERSONAL |
| Owner | UNASSIGNED (Tech Lead belum ditetapkan) |
| Status (saat render) | DRAFT |
| Source plan / version | plan `c09825d6-657b-4845-a959-154315737e0f` v2 (`sha256:547a873dfd6c24ca`) — PREVIEW, BELUM DISETUJUI |
| External revision / last synced | dicatat oleh AEOS (DocumentRecord/ExternalReference), tidak ditulis ke isi dokumen |
| Last reviewed | dicatat oleh AEOS per content hash (DocumentRecord), tidak ditulis ke isi dokumen |
| Links | requirement: f3 |
| Issue / evidence | dirujuk live di AEOS (Work items, Evidence) — tidak disalin ke dokumen |

RFC ini diperlukan karena dirujuk oleh PRD `02-requirements/prd-f3` (rencana yang disetujui):

- RFC-2

Entri rencana: `RFC-2` — Prefill formulir keputusan dari wizard

## Context / Problem

<!-- aeos:section=context_problem anchor=03-rfcs--rfc-f3--context-problem content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

Pemilik perlu membuka formulir keputusan yang tepat dengan bentuk target yang benar, tanpa wizard mencatat keputusan.

## Proposal

<!-- aeos:section=proposal anchor=03-rfcs--rfc-f3--proposal content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

Tautan berbasis query parameter allowlist (IC3) yang hanya mengisi field formulir yang ada. Formulir menampilkan semua field dan memerlukan submit manual; binding keputusan ADR-0031 tidak berubah. Implementasi diisolasi di T12 (R3) dan baru dimulai setelah RFC ini disetujui manusia.

## Goals / Non-goals

<!-- aeos:section=goals_non_goals anchor=03-rfcs--rfc-f3--goals-non-goals content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

- Tujuan: mengurangi kesalahan target
- Bukan tujuan: auto-submit, endpoint keputusan baru

## Alternatives

<!-- aeos:section=alternatives anchor=03-rfcs--rfc-f3--alternatives content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

- Hanya tautan tanpa prefill (lebih aman, tetapi rawan salah isi)
- Draft keputusan disimpan server (ditolak: wizard akan menulis state keputusan)

## Architecture Impact

<!-- aeos:section=architecture_impact anchor=03-rfcs--rfc-f3--architecture-impact content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

Perubahan kecil pada pembacaan parameter di frontend/src/pages/decisions (T12).

## Security Impact

<!-- aeos:section=security_impact anchor=03-rfcs--rfc-f3--security-impact content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

Risiko social engineering via tautan (RISK-5); mitigasi allowlist dan tanpa auto-submit.

## Privacy Impact

<!-- aeos:section=privacy_impact anchor=03-rfcs--rfc-f3--privacy-impact content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

Tidak ada.

## Operations Impact

<!-- aeos:section=operations_impact anchor=03-rfcs--rfc-f3--operations-impact content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

Tidak ada.

## Performance Impact

<!-- aeos:section=performance_impact anchor=03-rfcs--rfc-f3--performance-impact content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

Tidak ada.

## Risks / Dependencies

<!-- aeos:section=risks_dependencies anchor=03-rfcs--rfc-f3--risks-dependencies content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

- RISK-5
- Dukungan prefill formulir saat ini (diverifikasi T1)

## Migration / Rollback

<!-- aeos:section=migration_rollback anchor=03-rfcs--rfc-f3--migration-rollback content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

Tanpa migrasi; rollback dengan menonaktifkan pembacaan prefill.

## Decision / Approval

<!-- aeos:section=decision_approval anchor=03-rfcs--rfc-f3--decision-approval content_type=HUMAN_GOVERNED -->
_Content type: HUMAN_GOVERNED_

_HUMAN_GOVERNED — diisi oleh manusia yang berwenang (keputusan / risk acceptance / approval). AEOS dan model tidak mengisi bagian ini._
