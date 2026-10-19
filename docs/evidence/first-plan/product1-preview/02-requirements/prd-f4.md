# PRD — Titik masuk dari Ringkasan

<!-- aeos:logical_id=02-requirements/prd-f4 renderer=4 -->

| Metadata | Nilai |
|---|---|
| Logical doc ID | `02-requirements/prd-f4` |
| Project / domain | DLY AEOS (`dly-aeos`) / PERSONAL |
| Owner | UNASSIGNED (Product Owner belum ditetapkan) |
| Status (saat render) | DRAFT |
| Source plan / version | plan `c09825d6-657b-4845-a959-154315737e0f` v2 (`sha256:547a873dfd6c24ca`) — PREVIEW, BELUM DISETUJUI |
| External revision / last synced | dicatat oleh AEOS (DocumentRecord/ExternalReference), tidak ditulis ke isi dokumen |
| Last reviewed | dicatat oleh AEOS per content hash (DocumentRecord), tidak ditulis ke isi dokumen |
| Links | related: 06-testing/test-plan-tp1; requirement: f4 |
| Issue / evidence | dirujuk live di AEOS (Work items, Evidence) — tidak disalin ke dokumen |

## Overview

<!-- aeos:section=overview anchor=02-requirements--prd-f4--overview content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

Banner di halaman Ringkasan yang mengarah ke Penyiapan awal selama ada gate CAN_* yang dibutuhkan untuk kerja PERSONAL berstatus BLOCKED.

## Problem

<!-- aeos:section=problem anchor=02-requirements--prd-f4--problem content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

Pemilik yang mendarat di Ringkasan tidak tahu bahwa penyiapan belum selesai.

## Background

<!-- aeos:section=background anchor=02-requirements--prd-f4--background content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

Status gate sudah tersedia; daftar gate yang dibutuhkan PERSONAL didefinisikan di katalog langkah (unlocks). entry_point dihitung backend di T2.

## Goals / Non-goals

<!-- aeos:section=goals_non_goals anchor=02-requirements--prd-f4--goals-non-goals content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

**Goals**

- Menampilkan titik masuk hanya saat relevan

**Non-goals**

- Mengubah logika gate

## Business Requirements

<!-- aeos:section=business_requirements anchor=02-requirements--prd-f4--business-requirements content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

- Banner hilang ketika semua gate PERSONAL yang dibutuhkan tidak BLOCKED

## Functional Requirements

<!-- aeos:section=functional_requirements anchor=02-requirements--prd-f4--functional-requirements content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

**Requirement (rencana):** Banner di Ringkasan selama ada gate CAN_* PERSONAL yang BLOCKED atau UNKNOWN.

1. FR1: Respons IC1 memuat entry_point {show, blocked_gates[]} yang dihitung backend (T2) dari unlocks katalog dan status gate.
2. FR2: Ringkasan menampilkan banner dengan daftar gate BLOCKED dan tautan ke Penyiapan awal ketika show=true.
3. FR3: Jika status gate UNKNOWN, show=true dan banner tampil dengan label UNKNOWN (tidak disembunyikan diam-diam).
4. FR4: Banner terlihat juga oleh VIEWER (hanya tautan baca).

## Non-functional Requirements

<!-- aeos:section=non_functional_requirements anchor=02-requirements--prd-f4--non-functional-requirements content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

- NFR1: Banner tidak menambah lebih dari satu request API pada pemuatan Ringkasan.
- NFR2: Kegagalan endpoint setup tidak memecahkan halaman Ringkasan (degradasi dengan pesan).

## User Stories

<!-- aeos:section=user_stories anchor=02-requirements--prd-f4--user-stories content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

- Sebagai pemilik, saat membuka Ringkasan pada instalasi baru saya ingin diarahkan ke Penyiapan awal.

## Acceptance Criteria

<!-- aeos:section=acceptance_criteria anchor=02-requirements--prd-f4--acceptance-criteria content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

1. AC: F4-AC1
2. AC: F4-AC2
3. AC: F4-AC3
4. AC: F4-AC1: Dengan gate CAN_* PERSONAL BLOCKED (atau UNKNOWN), Ringkasan menampilkan banner dengan tautan ke Penyiapan awal.
5. AC: F4-AC2: Ketika tidak ada gate PERSONAL yang BLOCKED atau UNKNOWN, banner tidak tampil.
6. AC: F4-AC3: Error API setup menampilkan pesan degradasi tanpa merusak Ringkasan.

## User / Customer Impact

<!-- aeos:section=user_customer_impact anchor=02-requirements--prd-f4--user-customer-impact content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

Penemuan wizard tanpa perlu dokumentasi.

## Dependencies

<!-- aeos:section=dependencies anchor=02-requirements--prd-f4--dependencies content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

- Fitur `F1`
- F1
- Kontrak IC1
- T2 (perhitungan entry_point)

## Assumptions

<!-- aeos:section=assumptions anchor=02-requirements--prd-f4--assumptions content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

- Daftar gate CAN_* yang dibutuhkan PERSONAL dapat diturunkan dari field unlocks katalog.

## Risks

<!-- aeos:section=risks anchor=02-requirements--prd-f4--risks content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

- Daftar gate PERSONAL tidak lengkap sehingga banner hilang terlalu dini

## Success Metrics

<!-- aeos:section=success_metrics anchor=02-requirements--prd-f4--success-metrics content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

- Tes F4-AC1..AC3 lulus di verifier tepercaya (belum dijalankan)

## Rollout Considerations

<!-- aeos:section=rollout_considerations anchor=02-requirements--prd-f4--rollout-considerations content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

Aditif; tidak ada migrasi.

## Related Work Items / RFC / TDD

<!-- aeos:section=related_work anchor=02-requirements--prd-f4--related-work content_type=DYNAMIC -->
_Content type: DYNAMIC_

- Kontrak api `IC1` — API Penyiapan awal (penyedia `T1`; konsumen `T2`, `T3`, `T4`, `T5`, `T6`, `T7`, `T8`, `T9`, `T11`; `contracts/api/setup-wizard.json`, `contracts/api/routes.json`)
  - Definisi: GET /api/setup/steps -> {catalog_version, host_os, freshness_threshold_seconds, progress, steps[], entry_point{show, blocked_gates[]}, office{status: BLOCKED konstanta, reason}} (VIEWER+). POST /api/setup/steps/{step_key}/recheck -> {step} (peran persis sesuai enumerasi terverifikasi T1; 401/403/404/409/429; CSRF wajib). Field respons allowlist; tidak ada field nilai secret.
  - Kompatibilitas: Versi v1; perubahan hanya aditif; penghapusan field memerlukan versi baru. Jalur positif office di masa depan = penambahan aditif.
- Kontrak schema `IC2` — Skema dan katalog langkah §07 (penyedia `T1`; konsumen `T2`, `T4`, `T6`; `contracts/setup/setup-steps.schema.json`, `contracts/setup/setup-steps.v1.json`)
  - Definisi: Step {step_key, order, title, why, unlocks[], check_ids[], kind, remedy{windows_powershell, macos}, decision_link?}; katalog v1 berisi 10 langkah §07; langkah tanpa check ditandai eksplisit.
  - Kompatibilitas: catalog_version semver; penambahan langkah = minor; perubahan urutan/penghapusan = major.
- Test Plan `06-testing/test-plan-tp1`
- Rencana: IC1
- Rencana: T2
- Rencana: T7
- Rencana: ADR-0011

> Issue/work item per TaskSpec dibuat publisher dan ditautkan sebagai ExternalReference.
>
> Sumber live: GitHub Issues (fadhlillah-dli/dly-aeos). Status dinamis dibaca dari sumber tersebut dan diproyeksikan di AEOS Control Center; dokumen ini tidak menyalin atau menyatakan status.
