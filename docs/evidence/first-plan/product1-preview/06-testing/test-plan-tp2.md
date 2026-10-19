# Test Plan — Rencana uji keamanan dan negatif

<!-- aeos:logical_id=06-testing/test-plan-tp2 renderer=4 -->

| Metadata | Nilai |
|---|---|
| Logical doc ID | `06-testing/test-plan-tp2` |
| Project / domain | DLY AEOS (`dly-aeos`) / PERSONAL |
| Owner | UNASSIGNED (QA Lead belum ditetapkan) |
| Status (saat render) | DRAFT |
| Source plan / version | plan `c09825d6-657b-4845-a959-154315737e0f` v2 (`sha256:547a873dfd6c24ca`) — PREVIEW, BELUM DISETUJUI |
| External revision / last synced | dicatat oleh AEOS (DocumentRecord/ExternalReference), tidak ditulis ke isi dokumen |
| Last reviewed | dicatat oleh AEOS per content hash (DocumentRecord), tidak ditulis ke isi dokumen |
| Links | requirement: f1, f2, f3, 02-requirements/prd-f1, 02-requirements/prd-f2, 02-requirements/prd-f3 |
| Issue / evidence | dirujuk live di AEOS (Work items, Evidence) — tidak disalin ke dokumen |

## Overview

<!-- aeos:section=overview anchor=06-testing--test-plan-tp2--overview content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

Test Plan untuk fitur **Halaman Penyiapan awal (read-only) berbasis Doctor** (`f1`), **Periksa ulang per langkah** (`f2`), **Langkah keputusan manusia dan penanda OFFICE** (`f3`).

Tes negatif otorisasi, CSRF, kebocoran secret/jalur home, validasi input dan prefill. Tidak mengklaim VA/PT.

## Objectives

<!-- aeos:section=objectives anchor=06-testing--test-plan-tp2--objectives content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

- Membuktikan VIEWER tidak dapat memicu aksi
- Membuktikan tidak ada secret atau jalur home dalam respons/DOM
- Membuktikan input tidak sah dan permintaan tanpa CSRF ditolak

## Scope

<!-- aeos:section=scope anchor=06-testing--test-plan-tp2--scope content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

Endpoint setup baru, halaman wizard, parameter prefill formulir keputusan.

## Test Strategy

<!-- aeos:section=test_strategy anchor=06-testing--test-plan-tp2--test-strategy content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

- Approach: Tes negatif otomatis berbasis threat model TH-1..TH-7; konkurensi diuji deterministik dengan runner tiruan yang diblokir event/semaphore.
- Levels: integration, component, acceptance
- Types: security, negative
- Environments: Verifier tepercaya (CI), Lokal
- Data: Akun VIEWER dan pemilik uji; nilai secret dan jalur home sintetis yang ditanam di store tiruan untuk deteksi kebocoran.
- Tools: pytest, vitest, @testing-library/react

## Approach

<!-- aeos:section=approach anchor=06-testing--test-plan-tp2--approach content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

Tes negatif otomatis berbasis threat model TH-1..TH-7; konkurensi diuji deterministik dengan runner tiruan yang diblokir event/semaphore.

## Test Levels

<!-- aeos:section=test_levels anchor=06-testing--test-plan-tp2--test-levels content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

- integration
- component
- acceptance

## Test Types

<!-- aeos:section=test_types anchor=06-testing--test-plan-tp2--test-types content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

- security
- negative

## Environment

<!-- aeos:section=environment anchor=06-testing--test-plan-tp2--environment content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

- Verifier tepercaya (CI)
- Lokal

## Test Data

<!-- aeos:section=test_data anchor=06-testing--test-plan-tp2--test-data content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

Akun VIEWER dan pemilik uji; nilai secret dan jalur home sintetis yang ditanam di store tiruan untuk deteksi kebocoran.

## Tools

<!-- aeos:section=tools anchor=06-testing--test-plan-tp2--tools content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

- pytest
- vitest
- @testing-library/react

## Scenarios

<!-- aeos:section=scenarios anchor=06-testing--test-plan-tp2--scenarios content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

- **TP2-S1** — POST recheck dari VIEWER (security); langkah: Login VIEWER; POST /api/setup/steps/{step_key}/recheck langsung; expected: 403 dan runner Doctor tidak dipanggil.; AC: F2-AC2
- **TP2-S2** — VIEWER tidak melihat tombol aksi (security); langkah: Render halaman sebagai VIEWER; expected: Tidak ada tombol Periksa ulang atau tautan keputusan.; AC: F1-AC4, F3-AC4
- **TP2-S3** — Pemindaian secret (security); langkah: Tanam nilai secret sintetis di store tiruan dan detail Doctor; Ambil respons API dan DOM; expected: Nilai sintetis dan pola token tidak ditemukan.; AC: F1-AC5
- **TP2-S4** — step_key tidak dikenal (negative); langkah: POST recheck dengan step_key acak; expected: 404 tanpa eksekusi check.; AC: F2-AC3
- **TP2-S5** — Recheck bersamaan (deterministik) (negative); langkah: Runner tiruan diblokir dengan event; Kirim recheck pertama untuk langkah X; Kirim recheck kedua untuk langkah X saat runner masih diblokir; Lepaskan event; expected: Permintaan kedua 409; permintaan pertama selesai 200.; AC: F2-AC4
- **TP2-S6** — Tanpa token CSRF (security); langkah: POST recheck sebagai pemilik tanpa token CSRF/origin sah; expected: Ditolak sesuai mekanisme CSRF yang ada; runner tidak dipanggil.; AC: F2-AC6
- **TP2-S7** — Parameter prefill tidak dikenal (negative); langkah: Buka formulir keputusan dengan parameter di luar allowlist; expected: Parameter diabaikan; tidak ada submit otomatis.; AC: F3-AC1, F3-AC2
- **TP2-S8** — Pemindaian jalur home (security); langkah: Tanam remedy/detail sintetis berisi C:\Users\synthetic-user dan /Users/synthetic-user; Ambil respons API dan DOM; expected: Pola jalur home sintetis tidak ditemukan (atau tersamarkan).; AC: F1-AC5

## Test Cases

<!-- aeos:section=test_cases anchor=06-testing--test-plan-tp2--test-cases content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

| Case | Skenario / criterion | Expected result | AC refs |
|---|---|---|---|
| TP2-S1 | POST recheck dari VIEWER | 403 dan runner Doctor tidak dipanggil. | F2-AC2 |
| TP2-S2 | VIEWER tidak melihat tombol aksi | Tidak ada tombol Periksa ulang atau tautan keputusan. | F1-AC4, F3-AC4 |
| TP2-S3 | Pemindaian secret | Nilai sintetis dan pola token tidak ditemukan. | F1-AC5 |
| TP2-S4 | step_key tidak dikenal | 404 tanpa eksekusi check. | F2-AC3 |
| TP2-S5 | Recheck bersamaan (deterministik) | Permintaan kedua 409; permintaan pertama selesai 200. | F2-AC4 |
| TP2-S6 | Tanpa token CSRF | Ditolak sesuai mekanisme CSRF yang ada; runner tidak dipanggil. | F2-AC6 |
| TP2-S7 | Parameter prefill tidak dikenal | Parameter diabaikan; tidak ada submit otomatis. | F3-AC1, F3-AC2 |
| TP2-S8 | Pemindaian jalur home | Pola jalur home sintetis tidak ditemukan (atau tersamarkan). | F1-AC5 |

## Functional Testing

<!-- aeos:section=functional_testing anchor=06-testing--test-plan-tp2--functional-testing content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

- **TP2-S4** — step_key tidak dikenal (negative); langkah: POST recheck dengan step_key acak; expected: 404 tanpa eksekusi check.; AC: F2-AC3
- **TP2-S5** — Recheck bersamaan (deterministik) (negative); langkah: Runner tiruan diblokir dengan event; Kirim recheck pertama untuk langkah X; Kirim recheck kedua untuk langkah X saat runner masih diblokir; Lepaskan event; expected: Permintaan kedua 409; permintaan pertama selesai 200.; AC: F2-AC4
- **TP2-S7** — Parameter prefill tidak dikenal (negative); langkah: Buka formulir keputusan dengan parameter di luar allowlist; expected: Parameter diabaikan; tidak ada submit otomatis.; AC: F3-AC1, F3-AC2

## Regression Testing

<!-- aeos:section=regression_testing anchor=06-testing--test-plan-tp2--regression-testing content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

_Documentation pending — belum ada di PlanVersion yang disetujui; tidak dikarang oleh AEOS._

## Performance Testing

<!-- aeos:section=performance_testing anchor=06-testing--test-plan-tp2--performance-testing content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

_Documentation pending — belum ada di PlanVersion yang disetujui; tidak dikarang oleh AEOS._

## Security Testing

<!-- aeos:section=security_testing anchor=06-testing--test-plan-tp2--security-testing content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

- **TP2-S1** — POST recheck dari VIEWER (security); langkah: Login VIEWER; POST /api/setup/steps/{step_key}/recheck langsung; expected: 403 dan runner Doctor tidak dipanggil.; AC: F2-AC2
- **TP2-S2** — VIEWER tidak melihat tombol aksi (security); langkah: Render halaman sebagai VIEWER; expected: Tidak ada tombol Periksa ulang atau tautan keputusan.; AC: F1-AC4, F3-AC4
- **TP2-S3** — Pemindaian secret (security); langkah: Tanam nilai secret sintetis di store tiruan dan detail Doctor; Ambil respons API dan DOM; expected: Nilai sintetis dan pola token tidak ditemukan.; AC: F1-AC5
- **TP2-S6** — Tanpa token CSRF (security); langkah: POST recheck sebagai pemilik tanpa token CSRF/origin sah; expected: Ditolak sesuai mekanisme CSRF yang ada; runner tidak dipanggil.; AC: F2-AC6
- **TP2-S8** — Pemindaian jalur home (security); langkah: Tanam remedy/detail sintetis berisi C:\Users\synthetic-user dan /Users/synthetic-user; Ambil respons API dan DOM; expected: Pola jalur home sintetis tidak ditemukan (atau tersamarkan).; AC: F1-AC5

## UAT

<!-- aeos:section=uat anchor=06-testing--test-plan-tp2--uat content_type=DYNAMIC -->
_Content type: DYNAMIC_

> Pelaksanaan dan hasil UAT dicatat oleh manusia yang berwenang dengan evidence; dokumen tidak menyatakan hasil UAT.
>
> Sumber live: Sign-off manusia + EvidenceArtifact. Status dinamis dibaca dari sumber tersebut dan diproyeksikan di AEOS Control Center; dokumen ini tidak menyalin atau menyatakan status.

## Execution / Results

<!-- aeos:section=execution_results anchor=06-testing--test-plan-tp2--execution-results content_type=DYNAMIC -->
_Content type: DYNAMIC_

> Hasil hanya dari verifier independen; dokumen ini dan model tidak menyatakan hasil uji.
>
> Sumber live: AEOS independent verifier (EvidenceArtifact tepercaya). Status dinamis dibaca dari sumber tersebut dan diproyeksikan di AEOS Control Center; dokumen ini tidak menyalin atau menyatakan status.

## Defect Management

<!-- aeos:section=defect_management anchor=06-testing--test-plan-tp2--defect-management content_type=DYNAMIC -->
_Content type: DYNAMIC_

> Proses (rencana): Temuan keamanan dicatat sebagai GitHub Issues berlabel security; penerimaan risiko residual hanya melalui keputusan manusia.
>
> Defect dicatat sebagai issue/work item.
>
> Sumber live: GitHub Issues (fadhlillah-dli/dly-aeos). Status dinamis dibaca dari sumber tersebut dan diproyeksikan di AEOS Control Center; dokumen ini tidak menyalin atau menyatakan status.

## Exit Criteria

<!-- aeos:section=exit_criteria anchor=06-testing--test-plan-tp2--exit-criteria content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

- 0 kebocoran secret/jalur home terdeteksi
- Semua tes negatif lulus
- Review keamanan T8 selesai dengan temuan terdokumentasi

**Acceptance criteria yang harus terbukti**

- Semua skenario TP2 lulus di verifier tepercaya

## Final Assessment

<!-- aeos:section=final_assessment anchor=06-testing--test-plan-tp2--final-assessment content_type=HUMAN_GOVERNED -->
_Content type: HUMAN_GOVERNED_

_HUMAN_GOVERNED — diisi oleh manusia yang berwenang (keputusan / risk acceptance / approval). AEOS dan model tidak mengisi bagian ini._

## Sign-off

<!-- aeos:section=sign_off anchor=06-testing--test-plan-tp2--sign-off content_type=HUMAN_GOVERNED -->
_Content type: HUMAN_GOVERNED_

_HUMAN_GOVERNED — diisi oleh manusia yang berwenang (keputusan / risk acceptance / approval). AEOS dan model tidak mengisi bagian ini._

## Evidence / References

<!-- aeos:section=evidence_references anchor=06-testing--test-plan-tp2--evidence-references content_type=DYNAMIC -->
_Content type: DYNAMIC_

> Evidence yang disyaratkan rencana:
>
> - Laporan verifier tepercaya
> - Laporan review keamanan T8 (docs/security/setup-wizard-review.md)
>
> Referensi evidence ditautkan setelah verifier menjalankan suite.
>
> Sumber live: AEOS Evidence & Decisions. Status dinamis dibaca dari sumber tersebut dan diproyeksikan di AEOS Control Center; dokumen ini tidak menyalin atau menyatakan status.
