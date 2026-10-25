# Release Plan — UNKNOWN (usulan: minor bump dari versi controller saat ini) (Slice 1: Penyiapan awal)

<!-- aeos:logical_id=07-release-deployment/release-plan-unknown-usulan-minor-bump-dari-versi-con renderer=4 -->

| Metadata | Nilai |
|---|---|
| Logical doc ID | `07-release-deployment/release-plan-unknown-usulan-minor-bump-dari-versi-con` |
| Project / domain | DLY AEOS (`dly-aeos`) / PERSONAL |
| Owner | UNASSIGNED (Release Manager belum ditetapkan) |
| Status (saat render) | DRAFT |
| Source plan / version | plan `c09825d6-657b-4845-a959-154315737e0f` v2 (`sha256:547a873dfd6c24ca`) — PREVIEW, BELUM DISETUJUI |
| External revision / last synced | dicatat oleh AEOS (DocumentRecord/ExternalReference), tidak ditulis ke isi dokumen |
| Last reviewed | dicatat oleh AEOS per content hash (DocumentRecord), tidak ditulis ke isi dokumen |
| Links | related: 06-testing/test-plan-tp1, 06-testing/test-plan-tp2; release: UNKNOWN (usulan: minor bump dari versi controller saat ini), REL-1; requirement: f1, f2, f3, f4 |
| Issue / evidence | dirujuk live di AEOS (Work items, Evidence) — tidak disalin ke dokumen |

## Overview

<!-- aeos:section=overview anchor=07-release-deployment--release-plan-unknown-usulan-minor-bump-dari-versi-con--overview content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

Rilis aditif yang menambah halaman Penyiapan awal, endpoint setup, registrasi rute/menu, pembacaan prefill formulir keputusan dan banner Ringkasan.

## Objectives

<!-- aeos:section=objectives anchor=07-release-deployment--release-plan-unknown-usulan-minor-bump-dari-versi-con--objectives content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

_Documentation pending — belum ada di PlanVersion yang disetujui; tidak dikarang oleh AEOS._

## Scope

<!-- aeos:section=scope anchor=07-release-deployment--release-plan-unknown-usulan-minor-bump-dari-versi-con--scope content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

F1-F4

## Features / Enhancements / Fixes / Breaking Changes

<!-- aeos:section=changes anchor=07-release-deployment--release-plan-unknown-usulan-minor-bump-dari-versi-con--changes content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

**Features**

- Halaman Penyiapan awal (read-only) berbasis Doctor (`f1`)
- Periksa ulang per langkah (`f2`)
- Langkah keputusan manusia dan penanda OFFICE (`f3`)
- Titik masuk dari Ringkasan (`f4`)

**Enhancements**

- Tautan dari Ringkasan ke Penyiapan awal
- Dokumentasi setup diperbarui menunjuk ke wizard

## Related Requirements / Architecture

<!-- aeos:section=related_requirements_architecture anchor=07-release-deployment--release-plan-unknown-usulan-minor-bump-dari-versi-con--related-requirements-architecture content_type=DYNAMIC -->
_Content type: DYNAMIC_

> - PRD `02-requirements/prd-f1`
> - PRD `02-requirements/prd-f2`
> - PRD `02-requirements/prd-f3`
> - PRD `02-requirements/prd-f4`
> - `03-architecture-overview`
>
> Requirement terkait (rencana):
>
> - F1-AC1..AC7
> - F2-AC1..AC6
> - F3-AC1..AC4
> - F4-AC1..AC3
>
> Sumber live: AEOS DocumentRecord (Product 1). Status dinamis dibaca dari sumber tersebut dan diproyeksikan di AEOS Control Center; dokumen ini tidak menyalin atau menyatakan status.

## Testing Status

<!-- aeos:section=testing_status anchor=07-release-deployment--release-plan-unknown-usulan-minor-bump-dari-versi-con--testing-status content_type=DYNAMIC -->
_Content type: DYNAMIC_

> Test Plan: `06-testing/test-plan-tp1`, `06-testing/test-plan-tp2`
>
> Sumber live: AEOS independent verifier + CI. Status dinamis dibaca dari sumber tersebut dan diproyeksikan di AEOS Control Center; dokumen ini tidak menyalin atau menyatakan status.

## Security Status

<!-- aeos:section=security_status anchor=07-release-deployment--release-plan-unknown-usulan-minor-bump-dari-versi-con--security-status content_type=DYNAMIC -->
_Content type: DYNAMIC_

> Temuan dan status security per perubahan dibaca dari review/evidence nyata.
>
> Sumber live: AEOS security review / scanner (EvidenceArtifact tepercaya). Status dinamis dibaca dari sumber tersebut dan diproyeksikan di AEOS Control Center; dokumen ini tidak menyalin atau menyatakan status.

## UAT Status

<!-- aeos:section=uat_status anchor=07-release-deployment--release-plan-unknown-usulan-minor-bump-dari-versi-con--uat-status content_type=DYNAMIC -->
_Content type: DYNAMIC_

> UAT hanya dari pelaksanaan nyata dengan evidence; dokumen tidak menyatakan hasil.
>
> Sumber live: Sign-off manusia + EvidenceArtifact. Status dinamis dibaca dari sumber tersebut dan diproyeksikan di AEOS Control Center; dokumen ini tidak menyalin atau menyatakan status.

## Known Issues / Risks

<!-- aeos:section=known_issues_risks anchor=07-release-deployment--release-plan-unknown-usulan-minor-bump-dari-versi-con--known-issues-risks content_type=DYNAMIC -->
_Content type: DYNAMIC_

> Known issues tercatat di rencana:
>
> - Langkah tanpa Doctor check yang ada akan tampil UNKNOWN (daftar final ditentukan T1)
> - Ambang kesegaran mengikuti Doctor atau nilai ADR yang disetujui; nilainya UNKNOWN pada saat perencanaan
> - OFFICE selalu tampil BLOCKED pada slice ini (tanpa jalur positif)
> - Bentuk F2 bergantung pada keputusan manusia atas dukungan subset runner
>
> Risiko tercatat di rencana:
>
> - RISK-1
> - RISK-4
> - RISK-5
> - RISK-7
>
> Issue terbuka dibaca live dari tracker.
>
> Sumber live: GitHub Issues (fadhlillah-dli/dly-aeos). Status dinamis dibaca dari sumber tersebut dan diproyeksikan di AEOS Control Center; dokumen ini tidak menyalin atau menyatakan status.

## Release Notes

<!-- aeos:section=release_notes anchor=07-release-deployment--release-plan-unknown-usulan-minor-bump-dari-versi-con--release-notes content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

Menambahkan halaman 'Penyiapan awal' yang memandu urutan bootstrap §07 berdasarkan hasil Doctor.

**Highlights**

- Langkah berurutan dengan status, kesegaran dan perintah per OS
- Periksa ulang per langkah
- Tautan ke formulir keputusan dengan target terisi (tanpa mencatat keputusan)
- Banner di Ringkasan selama gate PERSONAL BLOCKED

**Upgrade notes:** Tidak ada migrasi database. Setelah upgrade jalankan Doctor sekali agar status terkini tampil.

**Known limitations**

- Tidak ada auto-install
- Tidak ada input secret di browser
- OFFICE hanya ditampilkan BLOCKED
- Tidak ada metrik/alert baru dalam rilis ini

## Deployment

<!-- aeos:section=deployment anchor=07-release-deployment--release-plan-unknown-usulan-minor-bump-dari-versi-con--deployment content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

Promosi rilis controller sesuai ADR-0091 dan runbook controller-upgrade-rollback; diputuskan dan dijalankan oleh manusia.

## Migration

<!-- aeos:section=migration anchor=07-release-deployment--release-plan-unknown-usulan-minor-bump-dari-versi-con--migration content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

Tidak ada migrasi data/skema.

## Rollback

<!-- aeos:section=rollback anchor=07-release-deployment--release-plan-unknown-usulan-minor-bump-dari-versi-con--rollback content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

Rollback ke rilis controller sebelumnya sesuai docs/runbooks/controller-upgrade-rollback.md; tidak ada data baru yang perlu dibersihkan.

## Production Readiness

<!-- aeos:section=production_readiness anchor=07-release-deployment--release-plan-unknown-usulan-minor-bump-dari-versi-con--production-readiness content_type=DYNAMIC -->
_Content type: DYNAMIC_

> Kriteria kesiapan (rencana; pemenuhannya dibaca live):
>
> - Bukti baseline dan verifier tepercaya lulus untuk commit rilis
> - RFC-2 disetujui manusia sebelum T12
> - Review keamanan T8 terdokumentasi
> - Registrasi rute/menu (T11) terverifikasi
> - Dokumentasi setup diperbarui (T10)
> - Persetujuan merge dan rilis oleh manusia
>
> Kesiapan produksi hanya dari evidence; dokumen tidak menyatakan siap/tidak siap.
>
> Sumber live: AEOS gates / Operations. Status dinamis dibaca dari sumber tersebut dan diproyeksikan di AEOS Control Center; dokumen ini tidak menyalin atau menyatakan status.

## Sign-off

<!-- aeos:section=sign_off anchor=07-release-deployment--release-plan-unknown-usulan-minor-bump-dari-versi-con--sign-off content_type=HUMAN_GOVERNED -->
_Content type: HUMAN_GOVERNED_

_HUMAN_GOVERNED — diisi oleh manusia yang berwenang (keputusan / risk acceptance / approval). AEOS dan model tidak mengisi bagian ini._

Sign-off rilis hanya oleh manusia yang berwenang (Decision).

## Post-release Validation

<!-- aeos:section=post_release_validation anchor=07-release-deployment--release-plan-unknown-usulan-minor-bump-dari-versi-con--post-release-validation content_type=DYNAMIC -->
_Content type: DYNAMIC_

> Pemeriksaan pasca-rilis yang direncanakan:
>
> - Buka Penyiapan awal pada instalasi pemilik dan bandingkan status dengan tabel Doctor
> - Uji recheck pada langkah lokal (akun admin) dan cek event audit; hindari langkah adapter yang memerlukan jaringan/kredensial
> - Login VIEWER dan pastikan tidak ada tombol aksi
>
> Hasil validasi hanya dari evidence runtime/production.
>
> Sumber live: AEOS Operations (production evidence). Status dinamis dibaca dari sumber tersebut dan diproyeksikan di AEOS Control Center; dokumen ini tidak menyalin atau menyatakan status.

## Evidence

<!-- aeos:section=evidence anchor=07-release-deployment--release-plan-unknown-usulan-minor-bump-dari-versi-con--evidence content_type=DYNAMIC -->
_Content type: DYNAMIC_

> Snapshot release candidate (commit + content hash dokumen) dicatat AEOS sebagai DocumentRevision RELEASE_CANDIDATE; evidence verifier ditautkan di sana.
>
> Sumber live: AEOS Evidence & Decisions. Status dinamis dibaca dari sumber tersebut dan diproyeksikan di AEOS Control Center; dokumen ini tidak menyalin atau menyatakan status.
