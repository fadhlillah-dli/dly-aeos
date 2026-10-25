# Test Plan — Rencana uji fungsional Penyiapan awal

<!-- aeos:logical_id=06-testing/test-plan-tp1 renderer=4 -->

| Metadata | Nilai |
|---|---|
| Logical doc ID | `06-testing/test-plan-tp1` |
| Project / domain | DLY AEOS (`dly-aeos`) / PERSONAL |
| Owner | UNASSIGNED (QA Lead belum ditetapkan) |
| Status (saat render) | DRAFT |
| Source plan / version | plan `c09825d6-657b-4845-a959-154315737e0f` v2 (`sha256:547a873dfd6c24ca`) — PREVIEW, BELUM DISETUJUI |
| External revision / last synced | dicatat oleh AEOS (DocumentRecord/ExternalReference), tidak ditulis ke isi dokumen |
| Last reviewed | dicatat oleh AEOS per content hash (DocumentRecord), tidak ditulis ke isi dokumen |
| Links | requirement: f1, f2, f3, f4, 02-requirements/prd-f1, 02-requirements/prd-f2, 02-requirements/prd-f3, 02-requirements/prd-f4 |
| Issue / evidence | dirujuk live di AEOS (Work items, Evidence) — tidak disalin ke dokumen |

## Overview

<!-- aeos:section=overview anchor=06-testing--test-plan-tp1--overview content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

Test Plan untuk fitur **Halaman Penyiapan awal (read-only) berbasis Doctor** (`f1`), **Periksa ulang per langkah** (`f2`), **Langkah keputusan manusia dan penanda OFFICE** (`f3`), **Titik masuk dari Ringkasan** (`f4`).

Menguji perilaku end-to-end halaman Penyiapan awal, recheck, tautan keputusan dan banner Ringkasan. Tidak ada hasil eksekusi yang diklaim pada tahap perencanaan.

## Objectives

<!-- aeos:section=objectives anchor=06-testing--test-plan-tp1--objectives content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

- Membuktikan urutan §07 dan status berbasis Doctor
- Membuktikan recheck hanya menjalankan check langkah
- Membuktikan wizard tidak mencatat keputusan
- Membuktikan banner muncul/hilang sesuai gate
- Membuktikan aksesibilitas dan performa dasar

## Scope

<!-- aeos:section=scope anchor=06-testing--test-plan-tp1--scope content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

Backend setup_wizard, API baru, registrasi rute/menu, halaman frontend baru, banner Ringkasan, integrasi tautan dan prefill formulir keputusan.

## Test Strategy

<!-- aeos:section=test_strategy anchor=06-testing--test-plan-tp1--test-strategy content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

- Approach: Berbasis risiko dan acceptance criteria. Lapisan pengujian: (1) unit untuk composer/katalog; (2) integrasi API dengan runner Doctor tiruan untuk spy/konkurensi (T4); (3) Doctor nyata pada data dir sementara hanya untuk check lokal (T9), sementara check adapter.* yang memerlukan jaringan tidak di-mock menjadi VERIFIED dan harus tampil non-VERIFIED; (4) komponen frontend dengan Testing Library; (5) acceptance end-to-end di tests/acceptance.
- Levels: unit, integration, component, acceptance
- Types: functional, regression, integration, negative, edge, accessibility, performance
- Environments: Lokal Windows (PowerShell), Lokal macOS, Verifier tepercaya (CI)
- Data: Data dir sementara kosong; fixture hasil Doctor sintetis (FRESH/STALE/UNKNOWN/MISSING); nilai secret dan jalur home sintetis untuk tes kebocoran; tanpa jaringan.
- Tools: pytest, vitest, @testing-library/react

## Approach

<!-- aeos:section=approach anchor=06-testing--test-plan-tp1--approach content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

Berbasis risiko dan acceptance criteria. Lapisan pengujian: (1) unit untuk composer/katalog; (2) integrasi API dengan runner Doctor tiruan untuk spy/konkurensi (T4); (3) Doctor nyata pada data dir sementara hanya untuk check lokal (T9), sementara check adapter.* yang memerlukan jaringan tidak di-mock menjadi VERIFIED dan harus tampil non-VERIFIED; (4) komponen frontend dengan Testing Library; (5) acceptance end-to-end di tests/acceptance.

## Test Levels

<!-- aeos:section=test_levels anchor=06-testing--test-plan-tp1--test-levels content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

- unit
- integration
- component
- acceptance

## Test Types

<!-- aeos:section=test_types anchor=06-testing--test-plan-tp1--test-types content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

- functional
- regression
- integration
- negative
- edge
- accessibility
- performance

## Environment

<!-- aeos:section=environment anchor=06-testing--test-plan-tp1--environment content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

- Lokal Windows (PowerShell)
- Lokal macOS
- Verifier tepercaya (CI)

## Test Data

<!-- aeos:section=test_data anchor=06-testing--test-plan-tp1--test-data content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

Data dir sementara kosong; fixture hasil Doctor sintetis (FRESH/STALE/UNKNOWN/MISSING); nilai secret dan jalur home sintetis untuk tes kebocoran; tanpa jaringan.

## Tools

<!-- aeos:section=tools anchor=06-testing--test-plan-tp1--tools content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

- pytest
- vitest
- @testing-library/react

## Scenarios

<!-- aeos:section=scenarios anchor=06-testing--test-plan-tp1--scenarios content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

- **TP1-S1** — Data dir kosong (functional); langkah: Siapkan data dir kosong; Buka GET /api/setup/steps dan halaman Penyiapan awal melalui rute terdaftar; expected: 10 langkah dalam urutan §07, semua MISSING (atau UNKNOWN 'tidak ada check') dengan remedy.; AC: F1-AC1
- **TP1-S2** — Admin dibuat lalu Doctor dijalankan (integration); langkah: Jalankan aeos admin create pada data dir uji; Jalankan Doctor nyata (check lokal); Muat ulang halaman; expected: Langkah 1 VERIFIED dengan checked_at; langkah adapter tanpa jaringan tidak VERIFIED.; AC: F1-AC2
- **TP1-S3** — Hasil kedaluwarsa (edge); langkah: Fixture hasil VERIFIED dengan checked_at melewati freshness_threshold_seconds yang berlaku; Muat halaman; expected: Langkah STALE, tidak dihitung dalam progres.; AC: F1-AC3
- **TP1-S4** — Recheck satu langkah (runner tiruan, dimiliki T4) (integration); langkah: Login sebagai pemilik; POST recheck pada langkah OpenBao dengan runner Doctor tiruan ber-spy; Amati daftar check_id yang dipanggil; expected: Hanya check langkah itu dijalankan; checked_at/freshness diperbarui.; AC: F2-AC1
- **TP1-S5** — Tautan keputusan terisi (functional); langkah: Klik tautan langkah aktivasi rute worker; Amati formulir dan request jaringan; expected: Formulir yang ada terbuka dengan target terisi, tanpa submit otomatis dan tanpa POST keputusan.; AC: F3-AC1, F3-AC2
- **TP1-S6** — OFFICE tetap BLOCKED (functional); langkah: Buka halaman; expected: Bagian OFFICE BLOCKED (konstanta) dengan alasan, tanpa aksi.; AC: F3-AC3
- **TP1-S7** — Banner Ringkasan (integration); langkah: Gate CAN_* PERSONAL BLOCKED -> buka Ringkasan; Gate UNKNOWN -> buka Ringkasan; Ubah fixture agar semua gate tidak BLOCKED/UNKNOWN -> buka Ringkasan; Simulasikan error API setup; expected: Banner tampil (termasuk label UNKNOWN), lalu hilang, lalu pesan degradasi tanpa merusak halaman.; AC: F4-AC1, F4-AC2, F4-AC3
- **TP1-S8** — Aksesibilitas status dan tombol salin (accessibility); langkah: Render halaman; Periksa label teks status dan label aksesibel tombol; expected: Status punya label teks; tombol salin punya nama aksesibel.; AC: F1-AC6
- **TP1-S9** — Performa GET (performance); langkah: Ukur p95 GET /api/setup/steps pada 50 permintaan lokal dengan fixture; expected: p95 < 300 ms tanpa menjalankan check.; AC: F1-AC7

## Test Cases

<!-- aeos:section=test_cases anchor=06-testing--test-plan-tp1--test-cases content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

| Case | Skenario / criterion | Expected result | AC refs |
|---|---|---|---|
| TP1-S1 | Data dir kosong | 10 langkah dalam urutan §07, semua MISSING (atau UNKNOWN 'tidak ada check') dengan remedy. | F1-AC1 |
| TP1-S2 | Admin dibuat lalu Doctor dijalankan | Langkah 1 VERIFIED dengan checked_at; langkah adapter tanpa jaringan tidak VERIFIED. | F1-AC2 |
| TP1-S3 | Hasil kedaluwarsa | Langkah STALE, tidak dihitung dalam progres. | F1-AC3 |
| TP1-S4 | Recheck satu langkah (runner tiruan, dimiliki T4) | Hanya check langkah itu dijalankan; checked_at/freshness diperbarui. | F2-AC1 |
| TP1-S5 | Tautan keputusan terisi | Formulir yang ada terbuka dengan target terisi, tanpa submit otomatis dan tanpa POST keputusan. | F3-AC1, F3-AC2 |
| TP1-S6 | OFFICE tetap BLOCKED | Bagian OFFICE BLOCKED (konstanta) dengan alasan, tanpa aksi. | F3-AC3 |
| TP1-S7 | Banner Ringkasan | Banner tampil (termasuk label UNKNOWN), lalu hilang, lalu pesan degradasi tanpa merusak halaman. | F4-AC1, F4-AC2, F4-AC3 |
| TP1-S8 | Aksesibilitas status dan tombol salin | Status punya label teks; tombol salin punya nama aksesibel. | F1-AC6 |
| TP1-S9 | Performa GET | p95 < 300 ms tanpa menjalankan check. | F1-AC7 |

## Functional Testing

<!-- aeos:section=functional_testing anchor=06-testing--test-plan-tp1--functional-testing content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

- **TP1-S1** — Data dir kosong (functional); langkah: Siapkan data dir kosong; Buka GET /api/setup/steps dan halaman Penyiapan awal melalui rute terdaftar; expected: 10 langkah dalam urutan §07, semua MISSING (atau UNKNOWN 'tidak ada check') dengan remedy.; AC: F1-AC1
- **TP1-S2** — Admin dibuat lalu Doctor dijalankan (integration); langkah: Jalankan aeos admin create pada data dir uji; Jalankan Doctor nyata (check lokal); Muat ulang halaman; expected: Langkah 1 VERIFIED dengan checked_at; langkah adapter tanpa jaringan tidak VERIFIED.; AC: F1-AC2
- **TP1-S3** — Hasil kedaluwarsa (edge); langkah: Fixture hasil VERIFIED dengan checked_at melewati freshness_threshold_seconds yang berlaku; Muat halaman; expected: Langkah STALE, tidak dihitung dalam progres.; AC: F1-AC3
- **TP1-S4** — Recheck satu langkah (runner tiruan, dimiliki T4) (integration); langkah: Login sebagai pemilik; POST recheck pada langkah OpenBao dengan runner Doctor tiruan ber-spy; Amati daftar check_id yang dipanggil; expected: Hanya check langkah itu dijalankan; checked_at/freshness diperbarui.; AC: F2-AC1
- **TP1-S5** — Tautan keputusan terisi (functional); langkah: Klik tautan langkah aktivasi rute worker; Amati formulir dan request jaringan; expected: Formulir yang ada terbuka dengan target terisi, tanpa submit otomatis dan tanpa POST keputusan.; AC: F3-AC1, F3-AC2
- **TP1-S6** — OFFICE tetap BLOCKED (functional); langkah: Buka halaman; expected: Bagian OFFICE BLOCKED (konstanta) dengan alasan, tanpa aksi.; AC: F3-AC3
- **TP1-S7** — Banner Ringkasan (integration); langkah: Gate CAN_* PERSONAL BLOCKED -> buka Ringkasan; Gate UNKNOWN -> buka Ringkasan; Ubah fixture agar semua gate tidak BLOCKED/UNKNOWN -> buka Ringkasan; Simulasikan error API setup; expected: Banner tampil (termasuk label UNKNOWN), lalu hilang, lalu pesan degradasi tanpa merusak halaman.; AC: F4-AC1, F4-AC2, F4-AC3
- **TP1-S8** — Aksesibilitas status dan tombol salin (accessibility); langkah: Render halaman; Periksa label teks status dan label aksesibel tombol; expected: Status punya label teks; tombol salin punya nama aksesibel.; AC: F1-AC6

## Regression Testing

<!-- aeos:section=regression_testing anchor=06-testing--test-plan-tp1--regression-testing content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

_Documentation pending — belum ada di PlanVersion yang disetujui; tidak dikarang oleh AEOS._

## Performance Testing

<!-- aeos:section=performance_testing anchor=06-testing--test-plan-tp1--performance-testing content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

- **TP1-S9** — Performa GET (performance); langkah: Ukur p95 GET /api/setup/steps pada 50 permintaan lokal dengan fixture; expected: p95 < 300 ms tanpa menjalankan check.; AC: F1-AC7

## Security Testing

<!-- aeos:section=security_testing anchor=06-testing--test-plan-tp1--security-testing content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

_Documentation pending — belum ada di PlanVersion yang disetujui; tidak dikarang oleh AEOS._

## UAT

<!-- aeos:section=uat anchor=06-testing--test-plan-tp1--uat content_type=DYNAMIC -->
_Content type: DYNAMIC_

> Pelaksanaan dan hasil UAT dicatat oleh manusia yang berwenang dengan evidence; dokumen tidak menyatakan hasil UAT.
>
> Sumber live: Sign-off manusia + EvidenceArtifact. Status dinamis dibaca dari sumber tersebut dan diproyeksikan di AEOS Control Center; dokumen ini tidak menyalin atau menyatakan status.

## Execution / Results

<!-- aeos:section=execution_results anchor=06-testing--test-plan-tp1--execution-results content_type=DYNAMIC -->
_Content type: DYNAMIC_

> Hasil hanya dari verifier independen; dokumen ini dan model tidak menyatakan hasil uji.
>
> Sumber live: AEOS independent verifier (EvidenceArtifact tepercaya). Status dinamis dibaca dari sumber tersebut dan diproyeksikan di AEOS Control Center; dokumen ini tidak menyalin atau menyatakan status.

## Defect Management

<!-- aeos:section=defect_management anchor=06-testing--test-plan-tp1--defect-management content_type=DYNAMIC -->
_Content type: DYNAMIC_

> Proses (rencana): Defect dicatat sebagai GitHub Issues di fadhlillah-dli/dly-aeos (integrasi belum terverifikasi) dengan label slice-1 dan tautan ke acceptance criteria.
>
> Defect dicatat sebagai issue/work item.
>
> Sumber live: GitHub Issues (fadhlillah-dli/dly-aeos). Status dinamis dibaca dari sumber tersebut dan diproyeksikan di AEOS Control Center; dokumen ini tidak menyalin atau menyatakan status.

## Exit Criteria

<!-- aeos:section=exit_criteria anchor=06-testing--test-plan-tp1--exit-criteria content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

- Semua acceptance criteria F1-F4 memiliki tes yang lulus di verifier tepercaya
- Tidak ada defect terbuka berprioritas P0/P1

**Acceptance criteria yang harus terbukti**

- Semua skenario TP1 lulus di verifier tepercaya
- Tidak ada regresi pada suite readiness/overview/decisions yang ada dibandingkan bukti baseline

## Final Assessment

<!-- aeos:section=final_assessment anchor=06-testing--test-plan-tp1--final-assessment content_type=HUMAN_GOVERNED -->
_Content type: HUMAN_GOVERNED_

_HUMAN_GOVERNED — diisi oleh manusia yang berwenang (keputusan / risk acceptance / approval). AEOS dan model tidak mengisi bagian ini._

## Sign-off

<!-- aeos:section=sign_off anchor=06-testing--test-plan-tp1--sign-off content_type=HUMAN_GOVERNED -->
_Content type: HUMAN_GOVERNED_

_HUMAN_GOVERNED — diisi oleh manusia yang berwenang (keputusan / risk acceptance / approval). AEOS dan model tidak mengisi bagian ini._

## Evidence / References

<!-- aeos:section=evidence_references anchor=06-testing--test-plan-tp1--evidence-references content_type=DYNAMIC -->
_Content type: DYNAMIC_

> Evidence yang disyaratkan rencana:
>
> - Bukti run baseline oleh verifier tepercaya
> - Laporan verifier tepercaya untuk commit kandidat
> - Daftar tes yang ditambahkan dan pemetaannya ke acceptance criteria
> - Tangkapan UI (opsional) di docs/evidence/ui sebagai bukti visual, bukan bukti runtime
>
> Referensi evidence ditautkan setelah verifier menjalankan suite.
>
> Sumber live: AEOS Evidence & Decisions. Status dinamis dibaca dari sumber tersebut dan diproyeksikan di AEOS Control Center; dokumen ini tidak menyalin atau menyatakan status.
