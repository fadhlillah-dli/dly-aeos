# PRD — Periksa ulang per langkah

<!-- aeos:logical_id=02-requirements/prd-f2 renderer=4 -->

| Metadata | Nilai |
|---|---|
| Logical doc ID | `02-requirements/prd-f2` |
| Project / domain | DLY AEOS (`dly-aeos`) / PERSONAL |
| Owner | UNASSIGNED (Product Owner belum ditetapkan) |
| Status (saat render) | DRAFT |
| Source plan / version | plan `c09825d6-657b-4845-a959-154315737e0f` v2 (`sha256:547a873dfd6c24ca`) — PREVIEW, BELUM DISETUJUI |
| External revision / last synced | dicatat oleh AEOS (DocumentRecord/ExternalReference), tidak ditulis ke isi dokumen |
| Last reviewed | dicatat oleh AEOS per content hash (DocumentRecord), tidak ditulis ke isi dokumen |
| Links | related: 03-tdds/tdd-f2, 06-testing/test-plan-tp1, 06-testing/test-plan-tp2; requirement: f2 |
| Issue / evidence | dirujuk live di AEOS (Work items, Evidence) — tidak disalin ke dokumen |

## Overview

<!-- aeos:section=overview anchor=02-requirements--prd-f2--overview content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

Tombol 'Periksa ulang' per langkah yang menjalankan ulang hanya check milik langkah tersebut melalui runner Doctor yang ada dan memperbarui kesegaran.

## Problem

<!-- aeos:section=problem anchor=02-requirements--prd-f2--problem content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

Setelah menjalankan satu perintah, pemilik harus menjalankan seluruh Doctor untuk melihat hasilnya.

## Background

<!-- aeos:section=background anchor=02-requirements--prd-f2--background content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

Doctor sudah menjalankan check; slice ini memerlukan eksekusi subset check per langkah. Dukungan subset diverifikasi T1. Bila tidak didukung, manusia memilih fallback melalui pertanyaan material (blocking untuk M2).

## Goals / Non-goals

<!-- aeos:section=goals_non_goals anchor=02-requirements--prd-f2--goals-non-goals content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

**Goals**

- Umpan balik cepat per langkah
- Hanya peran berwenang yang dapat memicu

**Non-goals**

- Menjalankan remedy/perintah instalasi
- Menjalankan check di luar langkah yang dipilih

## Business Requirements

<!-- aeos:section=business_requirements anchor=02-requirements--prd-f2--business-requirements content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

- Aksi tulis dari UI harus tetap dalam batas otorisasi yang ada; VIEWER ditolak

## Functional Requirements

<!-- aeos:section=functional_requirements anchor=02-requirements--prd-f2--functional-requirements content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

**Requirement (rencana):** Menjalankan ulang hanya check milik satu langkah melalui runner Doctor, terbatas pada peran berwenang, dengan CSRF, audit, lock dan batas laju.

1. FR1: POST /api/setup/steps/{step_key}/recheck menjalankan tepat check_ids milik langkah melalui runner Doctor yang ada dan mengembalikan status langkah terbaru.
2. FR2: step_key divalidasi terhadap katalog; nilai tidak dikenal -> 404 tanpa menjalankan check.
3. FR3: Permintaan dari peran di luar peran berwenang (nama peran persis sesuai enumerasi yang diverifikasi T1 dan ditulis di IC1) -> 403; tanpa sesi -> 401. Perlindungan CSRF/same-origin yang ada diterapkan.
4. FR4: Satu recheck per langkah pada satu waktu; permintaan bersamaan -> 409. Batas laju per sesi -> 429. Nilai batas laju adalah konfigurasi dengan default yang ditetapkan di ADR dan disetujui manusia.
5. FR5: Setiap recheck tercatat sebagai event audit (aktor, step_key, check_ids, hasil status, tanpa detail sensitif).
6. FR6: Body request tidak menerima parameter selain step_key di path (tidak ada check_id bebas dari klien).

## Non-functional Requirements

<!-- aeos:section=non_functional_requirements anchor=02-requirements--prd-f2--non-functional-requirements content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

- NFR1: Timeout recheck per langkah adalah konfigurasi (default usulan 60 detik, disetujui manusia di ADR). Hasil timeout ditampilkan sebagai UNKNOWN, bukan VERIFIED.
- NFR2: Tes membuktikan check di luar langkah tidak dijalankan (spy/counter pada runner tiruan).
- NFR3: 100% permintaan VIEWER ke endpoint recheck ditolak 403 dalam tes negatif.
- NFR4: Tidak ada egress baru selain yang sudah dilakukan check yang bersangkutan.
- NFR5: Tes 409 deterministik menggunakan runner tiruan yang diblokir dengan event/semaphore, bukan berbasis waktu.

## User Stories

<!-- aeos:section=user_stories anchor=02-requirements--prd-f2--user-stories content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

- Sebagai pemilik, setelah menjalankan perintah OpenBao saya ingin memeriksa ulang langkah itu saja dan melihat waktu pemeriksaan terbaru.

## Acceptance Criteria

<!-- aeos:section=acceptance_criteria anchor=02-requirements--prd-f2--acceptance-criteria content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

1. AC: F2-AC1
2. AC: F2-AC2
3. AC: F2-AC3
4. AC: F2-AC4
5. AC: F2-AC5
6. AC: F2-AC6
7. AC: F2-AC1: Periksa ulang satu langkah hanya menjalankan check langkah itu dan memperbarui checked_at/freshness.
8. AC: F2-AC2: POST langsung dari VIEWER ditolak 403 dan tidak menjalankan check.
9. AC: F2-AC3: step_key tidak dikenal -> 404 tanpa eksekusi.
10. AC: F2-AC4: Recheck bersamaan untuk langkah yang sama -> 409.
11. AC: F2-AC5: Event audit tercatat untuk setiap recheck yang diterima.
12. AC: F2-AC6: POST recheck dari pemilik tanpa token CSRF/origin yang sah ditolak sesuai mekanisme yang ada dan tidak menjalankan check.

## User / Customer Impact

<!-- aeos:section=user_customer_impact anchor=02-requirements--prd-f2--user-customer-impact content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

Siklus perbaikan-verifikasi lebih cepat tanpa memperluas hak akses.

## Dependencies

<!-- aeos:section=dependencies anchor=02-requirements--prd-f2--dependencies content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

- Fitur `F1`
- F1
- Runner Doctor yang ada
- Kontrak IC1
- Jawaban manusia atas pertanyaan material dukungan subset run

## Assumptions

<!-- aeos:section=assumptions anchor=02-requirements--prd-f2--assumptions content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

- Runner Doctor dapat menjalankan subset check berdasarkan daftar check_id (diverifikasi T1). Jika tidak, berlaku fallback yang dipilih manusia.

## Risks

<!-- aeos:section=risks anchor=02-requirements--prd-f2--risks content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

- Penyalahgunaan recheck untuk memicu operasi mahal/berulang (RISK-2)
- Eskalasi hak akses jika gate peran salah (RISK-4)
- Kelayakan bergantung pada dukungan subset runner (RISK-7)

## Success Metrics

<!-- aeos:section=success_metrics anchor=02-requirements--prd-f2--success-metrics content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

- Tes negatif 401/403/404/409/429/CSRF lulus di verifier tepercaya (belum dijalankan)

## Rollout Considerations

<!-- aeos:section=rollout_considerations anchor=02-requirements--prd-f2--rollout-considerations content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

Endpoint baru di belakang otorisasi yang ada; dapat dinonaktifkan dengan menyembunyikan tombol dan menghapus rute.

## Related Work Items / RFC / TDD

<!-- aeos:section=related_work anchor=02-requirements--prd-f2--related-work content_type=DYNAMIC -->
_Content type: DYNAMIC_

- TDD `03-tdds/tdd-f2`
- Kontrak api `IC1` — API Penyiapan awal (penyedia `T1`; konsumen `T2`, `T3`, `T4`, `T5`, `T6`, `T7`, `T8`, `T9`, `T11`; `contracts/api/setup-wizard.json`, `contracts/api/routes.json`)
  - Definisi: GET /api/setup/steps -> {catalog_version, host_os, freshness_threshold_seconds, progress, steps[], entry_point{show, blocked_gates[]}, office{status: BLOCKED konstanta, reason}} (VIEWER+). POST /api/setup/steps/{step_key}/recheck -> {step} (peran persis sesuai enumerasi terverifikasi T1; 401/403/404/409/429; CSRF wajib). Field respons allowlist; tidak ada field nilai secret.
  - Kompatibilitas: Versi v1; perubahan hanya aditif; penghapusan field memerlukan versi baru. Jalur positif office di masa depan = penambahan aditif.
- Test Plan `06-testing/test-plan-tp1`
- Test Plan `06-testing/test-plan-tp2`
- Rencana: TDD-2
- Rencana: IC1
- Rencana: T4
- Rencana: T5
- Rencana: ADR-0011
- Rencana: ADR-0053

> Issue/work item per TaskSpec dibuat publisher dan ditautkan sebagai ExternalReference.
>
> Sumber live: GitHub Issues (fadhlillah-dli/dly-aeos). Status dinamis dibaca dari sumber tersebut dan diproyeksikan di AEOS Control Center; dokumen ini tidak menyalin atau menyatakan status.
