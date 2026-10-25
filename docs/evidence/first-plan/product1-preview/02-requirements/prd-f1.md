# PRD — Halaman Penyiapan awal (read-only) berbasis Doctor

<!-- aeos:logical_id=02-requirements/prd-f1 renderer=4 -->

| Metadata | Nilai |
|---|---|
| Logical doc ID | `02-requirements/prd-f1` |
| Project / domain | DLY AEOS (`dly-aeos`) / PERSONAL |
| Owner | UNASSIGNED (Product Owner belum ditetapkan) |
| Status (saat render) | DRAFT |
| Source plan / version | plan `c09825d6-657b-4845-a959-154315737e0f` v2 (`sha256:547a873dfd6c24ca`) — PREVIEW, BELUM DISETUJUI |
| External revision / last synced | dicatat oleh AEOS (DocumentRecord/ExternalReference), tidak ditulis ke isi dokumen |
| Last reviewed | dicatat oleh AEOS per content hash (DocumentRecord), tidak ditulis ke isi dokumen |
| Links | related: 03-rfcs/rfc-f1, 03-tdds/tdd-f1, 06-testing/test-plan-tp1, 06-testing/test-plan-tp2; requirement: f1 |
| Issue / evidence | dirujuk live di AEOS (Work items, Evidence) — tidak disalin ke dokumen |

## Overview

<!-- aeos:section=overview anchor=02-requirements--prd-f1--overview content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

Halaman baru di Control Center yang menampilkan langkah bootstrap §07 secara berurutan dengan status Doctor, kesegaran, capability yang dibuka dan perintah per OS.

## Problem

<!-- aeos:section=problem anchor=02-requirements--prd-f1--problem content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

Pemilik harus menyimpulkan sendiri urutan langkah §07 dari tabel Doctor dan README.

## Background

<!-- aeos:section=background anchor=02-requirements--prd-f1--background content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

Permintaan menyatakan setiap langkah §07 sudah memiliki Doctor check dan remedy yang dapat disalin (ADR-0011). Input baru memperlihatkan dua check (adapter.github_token, adapter.google_oauth); cakupan sisanya diverifikasi T1. Yang belum ada adalah penyusunan berurutan dan penjelasan apa yang dibuka setiap langkah.

## Goals / Non-goals

<!-- aeos:section=goals_non_goals anchor=02-requirements--prd-f1--goals-non-goals content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

**Goals**

- Menyajikan 10 langkah §07 dalam urutan: akun admin, cek login Claude Code, OpenBao install/init/unseal, kredensial worker, OpenCode, consent Google OAuth, token GitHub, aktivasi rute worker, keputusan sandbox, backup pertama + restore test
- Status dan progres hanya dari hasil Doctor
- Dapat diakses VIEWER secara read-only

**Non-goals**

- Menjalankan perintah
- Menyimpan state progres wizard
- Menambah Doctor check baru

## Business Requirements

<!-- aeos:section=business_requirements anchor=02-requirements--prd-f1--business-requirements content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

- Pemilik dapat mengetahui langkah berikutnya tanpa membaca README
- Status tidak boleh mengklaim lebih dari yang dibuktikan Doctor

## Functional Requirements

<!-- aeos:section=functional_requirements anchor=02-requirements--prd-f1--functional-requirements content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

**Requirement (rencana):** Menampilkan langkah bootstrap §07 berurutan dengan status Doctor, kesegaran, capability yang dibuka dan perintah per OS; dapat dicapai lewat rute/menu; read-only untuk VIEWER; tanpa secret.

1. FR1: Katalog langkah berversi (step_key, order, judul, alasan, unlocks[CAN_*], check_ids[], kind COMMAND|DECISION, remedy per OS) disimpan sebagai data di repo dan divalidasi terhadap skema kontrak.
2. FR2: GET /api/setup/steps mengembalikan langkah terurut dengan status agregat per langkah, status per check, checked_at, freshness (FRESH|STALE|UNKNOWN), freshness_threshold_seconds dan progress {verified,total}.
3. FR3: Status langkah = VERIFIED hanya jika semua check langkah VERIFIED dan FRESH. Check yang belum pernah dijalankan -> MISSING/UNKNOWN sesuai status Doctor. STALE ditampilkan sebagai STALE dan tidak dihitung dalam progres. Ambang kesegaran memakai aturan Doctor bila ada (dikonfirmasi T1); bila tidak ada, memakai nilai eksplisit yang ditetapkan di ADR slice ini dan disetujui manusia. Nilai yang dipakai selalu dikembalikan di respons.
4. FR4: Langkah yang tidak memiliki Doctor check yang ada ditampilkan UNKNOWN dengan keterangan 'tidak ada check', bukan VERIFIED.
5. FR5: Perintah remedy ditampilkan untuk OS host (Windows PowerShell atau macOS) dengan tab untuk OS lain; tombol salin hanya menyalin teks perintah.
6. FR6: Halaman tersedia untuk VIEWER; elemen aksi tidak dirender untuk VIEWER.
7. FR7: Respons API dibangun dari daftar field yang diizinkan (allowlist); detail mentah Doctor tidak diteruskan.
8. FR8: Halaman dapat dicapai melalui rute dan menu Control Center (registrasi oleh T11).

## Non-functional Requirements

<!-- aeos:section=non_functional_requirements anchor=02-requirements--prd-f1--non-functional-requirements content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

- NFR1: GET /api/setup/steps p95 < 300 ms pada data lokal tanpa menjalankan check (diukur dengan tes performa lokal, F1-AC7).
- NFR2: Respons dan DOM tidak mengandung nilai yang cocok dengan pola secret maupun pola jalur home (C:\Users\<nama>, /Users/<nama>). Dibuktikan tes otomatis dengan nilai sintetis.
- NFR3: Teks UI dalam Bahasa Indonesia. Aksesibilitas: status tidak hanya dikodekan warna (ada label teks), dan tombol salin punya label aksesibel (F1-AC6).
- NFR4: Urutan langkah deterministik dan sama dengan §07 (tes snapshot katalog).
- NFR5: Tidak ada egress jaringan baru dari backend untuk endpoint baca.

## User Stories

<!-- aeos:section=user_stories anchor=02-requirements--prd-f1--user-stories content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

- Sebagai pemilik, saya ingin melihat langkah penyiapan berikutnya beserta perintah yang bisa disalin agar saya tidak perlu membaca README.
- Sebagai VIEWER, saya ingin melihat status penyiapan tanpa bisa mengubah apa pun.

## Acceptance Criteria

<!-- aeos:section=acceptance_criteria anchor=02-requirements--prd-f1--acceptance-criteria content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

1. AC: F1-AC1
2. AC: F1-AC2
3. AC: F1-AC3
4. AC: F1-AC4
5. AC: F1-AC5
6. AC: F1-AC6
7. AC: F1-AC7
8. AC: F1-AC1: Dengan data dir kosong, setiap langkah tampil MISSING (atau UNKNOWN 'tidak ada check' untuk langkah tanpa check) beserta remedy-nya dalam urutan §07.
9. AC: F1-AC2: Setelah 'aeos admin create' dan Doctor dijalankan ulang, langkah 1 tampil VERIFIED dengan waktu pemeriksaan.
10. AC: F1-AC3: Hasil Doctor yang melewati ambang kesegaran yang berlaku (freshness_threshold_seconds) tampil STALE dan tidak dihitung dalam progres.
11. AC: F1-AC4: VIEWER dapat membuka halaman dan tidak melihat tombol aksi.
12. AC: F1-AC5: Tidak ada nilai secret, token, kata sandi atau jalur home sintetis dalam respons API maupun DOM.
13. AC: F1-AC6: Setiap status memiliki label teks dan tombol salin memiliki nama aksesibel.
14. AC: F1-AC7: p95 GET /api/setup/steps < 300 ms pada 50 permintaan lokal dengan fixture, tanpa menjalankan check.

## User / Customer Impact

<!-- aeos:section=user_customer_impact anchor=02-requirements--prd-f1--user-customer-impact content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

Waktu dan kebingungan saat bootstrap berkurang; status yang ditampilkan jujur terhadap bukti Doctor.

## Dependencies

<!-- aeos:section=dependencies anchor=02-requirements--prd-f1--dependencies content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

- Readiness Doctor dan registri check yang ada (ADR-0011)
- Capability gates CAN_*
- Model peran/otorisasi Control Center yang ada
- Kontrak IC1 dan IC2
- Laporan discovery T1

## Assumptions

<!-- aeos:section=assumptions anchor=02-requirements--prd-f1--assumptions content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

- Doctor menyimpan hasil terakhir per check beserta waktu pemeriksaan yang dapat dibaca tanpa menjalankan ulang check (diverifikasi T1).
- Aturan kesegaran Doctor mungkin ada atau tidak ada; bila tidak ada, ambang ditetapkan eksplisit di ADR dan disetujui manusia (diverifikasi T1).
- Backend dapat mengetahui OS host tempat AEOS berjalan.

## Risks

<!-- aeos:section=risks anchor=02-requirements--prd-f1--risks content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

- Pemetaan langkah ke check_id bisa salah atau ada langkah tanpa check (RISK-3)
- Remedy Doctor bisa mengandung jalur lokal/nilai sensitif (RISK-1)

## Success Metrics

<!-- aeos:section=success_metrics anchor=02-requirements--prd-f1--success-metrics content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

- Seluruh 10 langkah §07 terpetakan ke check yang ada atau ditandai eksplisit tanpa check
- 0 temuan pola secret/jalur home dalam tes keamanan
- Tes F1-AC1..AC7 lulus di verifier tepercaya (belum dijalankan)

## Rollout Considerations

<!-- aeos:section=rollout_considerations anchor=02-requirements--prd-f1--rollout-considerations content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

Rute dan menu baru bersifat aditif; tidak ada migrasi data. Dapat dinonaktifkan dengan menghapus entri menu/rute (T11) pada rilis berikutnya.

## Related Work Items / RFC / TDD

<!-- aeos:section=related_work anchor=02-requirements--prd-f1--related-work content_type=DYNAMIC -->
_Content type: DYNAMIC_

- RFC `03-rfcs/rfc-f1`
- TDD `03-tdds/tdd-f1`
- Kontrak api `IC1` — API Penyiapan awal (penyedia `T1`; konsumen `T2`, `T3`, `T4`, `T5`, `T6`, `T7`, `T8`, `T9`, `T11`; `contracts/api/setup-wizard.json`, `contracts/api/routes.json`)
  - Definisi: GET /api/setup/steps -> {catalog_version, host_os, freshness_threshold_seconds, progress, steps[], entry_point{show, blocked_gates[]}, office{status: BLOCKED konstanta, reason}} (VIEWER+). POST /api/setup/steps/{step_key}/recheck -> {step} (peran persis sesuai enumerasi terverifikasi T1; 401/403/404/409/429; CSRF wajib). Field respons allowlist; tidak ada field nilai secret.
  - Kompatibilitas: Versi v1; perubahan hanya aditif; penghapusan field memerlukan versi baru. Jalur positif office di masa depan = penambahan aditif.
- Kontrak schema `IC2` — Skema dan katalog langkah §07 (penyedia `T1`; konsumen `T2`, `T4`, `T6`; `contracts/setup/setup-steps.schema.json`, `contracts/setup/setup-steps.v1.json`)
  - Definisi: Step {step_key, order, title, why, unlocks[], check_ids[], kind, remedy{windows_powershell, macos}, decision_link?}; katalog v1 berisi 10 langkah §07; langkah tanpa check ditandai eksplisit.
  - Kompatibilitas: catalog_version semver; penambahan langkah = minor; perubahan urutan/penghapusan = major.
- Test Plan `06-testing/test-plan-tp1`
- Test Plan `06-testing/test-plan-tp2`
- Rencana: RFC-1
- Rencana: TDD-1
- Rencana: IC1
- Rencana: IC2
- Rencana: T1
- Rencana: T11
- Rencana: ADR-0011
- Rencana: ADR-0004

> Issue/work item per TaskSpec dibuat publisher dan ditautkan sebagai ExternalReference.
>
> Sumber live: GitHub Issues (fadhlillah-dli/dly-aeos). Status dinamis dibaca dari sumber tersebut dan diproyeksikan di AEOS Control Center; dokumen ini tidak menyalin atau menyatakan status.
