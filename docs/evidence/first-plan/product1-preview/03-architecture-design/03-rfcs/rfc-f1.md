# RFC — Halaman Penyiapan awal (read-only) berbasis Doctor

<!-- aeos:logical_id=03-rfcs/rfc-f1 renderer=4 -->

| Metadata | Nilai |
|---|---|
| Logical doc ID | `03-rfcs/rfc-f1` |
| Project / domain | DLY AEOS (`dly-aeos`) / PERSONAL |
| Owner | UNASSIGNED (Tech Lead belum ditetapkan) |
| Status (saat render) | DRAFT |
| Source plan / version | plan `c09825d6-657b-4845-a959-154315737e0f` v2 (`sha256:547a873dfd6c24ca`) — PREVIEW, BELUM DISETUJUI |
| External revision / last synced | dicatat oleh AEOS (DocumentRecord/ExternalReference), tidak ditulis ke isi dokumen |
| Last reviewed | dicatat oleh AEOS per content hash (DocumentRecord), tidak ditulis ke isi dokumen |
| Links | requirement: f1 |
| Issue / evidence | dirujuk live di AEOS (Work items, Evidence) — tidak disalin ke dokumen |

RFC ini diperlukan karena dirujuk oleh PRD `02-requirements/prd-f1` (rencana yang disetujui):

- RFC-1

Entri rencana: `RFC-1` — Katalog langkah §07 berversi dan API status penyiapan

## Context / Problem

<!-- aeos:section=context_problem anchor=03-rfcs--rfc-f1--context-problem content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

Urutan bootstrap §07 tidak terwujud dalam produk; Doctor hanya menampilkan tabel datar.

## Proposal

<!-- aeos:section=proposal anchor=03-rfcs--rfc-f1--proposal content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

Menambah katalog langkah berversi (IC2) sebagai file data di repo dan modul backend setup_wizard yang menyusun status dari hasil Doctor dan gate melalui API IC1. Status selalu turunan Doctor. Ambang kesegaran memakai aturan Doctor atau nilai eksplisit ADR yang disetujui manusia.

## Goals / Non-goals

<!-- aeos:section=goals_non_goals anchor=03-rfcs--rfc-f1--goals-non-goals content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

- Tujuan: urutan deterministik, status jujur, VIEWER read-only
- Bukan tujuan: check baru, state progres wizard, eksekusi perintah

## Alternatives

<!-- aeos:section=alternatives anchor=03-rfcs--rfc-f1--alternatives content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

- Menyimpan katalog di DB (ditolak: perlu migrasi, tidak terlihat dalam review PR)
- Menurunkan urutan dari metadata Doctor check (ditolak: mengubah registri Doctor)
- Halaman statis tanpa API (ditolak: tidak dapat menghitung status)

## Architecture Impact

<!-- aeos:section=architecture_impact anchor=03-rfcs--rfc-f1--architecture-impact content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

Modul baru setup_wizard bergantung pada antarmuka publik Doctor, gate dan audit. Entri module-boundaries ditambahkan T1 bila diwajibkan dan tidak terlindungi; bila terlindungi, diajukan sebagai usulan untuk manusia. Registrasi rute dilakukan T11.

## Security Impact

<!-- aeos:section=security_impact anchor=03-rfcs--rfc-f1--security-impact content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

Permukaan baca baru; risiko kebocoran dimitigasi allowlist dan tes pola secret/jalur home.

## Privacy Impact

<!-- aeos:section=privacy_impact anchor=03-rfcs--rfc-f1--privacy-impact content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

UNKNOWN sampai isi detail/remedy Doctor diperiksa T1 (lihat feature_governance F1).

## Operations Impact

<!-- aeos:section=operations_impact anchor=03-rfcs--rfc-f1--operations-impact content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

Katalog perlu dipelihara saat §07 berubah.

## Performance Impact

<!-- aeos:section=performance_impact anchor=03-rfcs--rfc-f1--performance-impact content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

Ringan; tidak menjalankan check pada GET.

## Risks / Dependencies

<!-- aeos:section=risks_dependencies anchor=03-rfcs--rfc-f1--risks-dependencies content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

- RISK-1
- RISK-3
- RISK-6
- Ketergantungan pada format hasil Doctor

## Migration / Rollback

<!-- aeos:section=migration_rollback anchor=03-rfcs--rfc-f1--migration-rollback content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

Tanpa migrasi; rollback dengan menghapus rute/menu.

## Decision / Approval

<!-- aeos:section=decision_approval anchor=03-rfcs--rfc-f1--decision-approval content_type=HUMAN_GOVERNED -->
_Content type: HUMAN_GOVERNED_

_HUMAN_GOVERNED — diisi oleh manusia yang berwenang (keputusan / risk acceptance / approval). AEOS dan model tidak mengisi bagian ini._
