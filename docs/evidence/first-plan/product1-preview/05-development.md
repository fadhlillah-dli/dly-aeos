# 05. Development

<!-- aeos:logical_id=05-development renderer=4 -->

| Metadata | Nilai |
|---|---|
| Logical doc ID | `05-development` |
| Project / domain | DLY AEOS (`dly-aeos`) / PERSONAL |
| Owner | UNASSIGNED (Engineering Lead belum ditetapkan) |
| Status (saat render) | DRAFT |
| Source plan / version | plan `c09825d6-657b-4845-a959-154315737e0f` v2 (`sha256:547a873dfd6c24ca`) — PREVIEW, BELUM DISETUJUI |
| External revision / last synced | dicatat oleh AEOS (DocumentRecord/ExternalReference), tidak ditulis ke isi dokumen |
| Last reviewed | dicatat oleh AEOS per content hash (DocumentRecord), tidak ditulis ke isi dokumen |
| Links | — |
| Issue / evidence | dirujuk live di AEOS (Work items, Evidence) — tidak disalin ke dokumen |

## Development Guidelines

<!-- aeos:section=development_guidelines anchor=05-development--development-guidelines content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

- Ikuti AGENTS.md dan CLAUDE.md di repo
- Jangan membuat ulang scaffold yang sudah ada; tambahkan modul baru secara aditif
- Laporan discovery T1 dan kontrak (IC1-IC3) diterima manusia sebelum task konsumen dimulai
- Patuhi daftar path terlindungi proyek self-build (§09) sebagaimana dilaporkan T1. Path terlindungi tidak diubah worker tanpa keputusan manusia.
- Patuhi contracts/architecture/module-boundaries.json; modul setup_wizard hanya membaca Doctor/gate/audit melalui antarmuka publiknya
- Teks UI Bahasa Indonesia; identifier kode dalam bahasa Inggris
- Task frontend yang berbagi frontend/src/pages/setup/** berjalan berurutan (T3 -> T5 -> T6) dan menambah aksi melalui slot StepActions

## Coding Standards

<!-- aeos:section=coding_standards anchor=05-development--coding-standards content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

- Python: gaya dan linter sesuai konfigurasi pyproject.toml yang ada; type hints pada API publik
- TypeScript: konfigurasi lint/tsconfig frontend yang ada; komponen mengikuti pola halaman di frontend/src/pages
- Tes berdampingan: pytest di tests/, Vitest di file *.test.tsx
- Pakai komponen yang ada (StatusBadge, RemedyPanel) bila memungkinkan
- Tes konkurensi memakai sinkronisasi deterministik (event/semaphore), bukan sleep/timing

## Secure Coding

<!-- aeos:section=secure_coding anchor=05-development--secure-coding content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

- Respons API dari allowlist field, tanpa meneruskan objek Doctor mentah
- Tidak ada eksekusi shell/subprocess di modul setup_wizard
- Validasi step_key terhadap katalog; tolak input lain
- Tidak pernah log atau render nilai secret; gunakan nilai sintetis di tes
- Tidak menggunakan dangerouslySetInnerHTML untuk perintah/remedy
- Clipboard hanya menyalin teks perintah statis
- Prefill formulir keputusan hanya dari allowlist IC3; tanpa auto-submit

## Branching

<!-- aeos:section=branching anchor=05-development--branching content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

Branch kandidat per task dari branch integrasi yang disetujui (inventaris saat ini integration/wave3); delivery melalui PR GitHub oleh broker tepercaya; merge ke main hanya oleh manusia.

## PR / Review

<!-- aeos:section=pr_review anchor=05-development--pr-review content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

Setiap PR memerlukan hasil verifier tepercaya dan review manusia. Task berisiko R3 (T4 recheck/otorisasi, T12 prefill keputusan) memerlukan review keamanan tambahan.

## Dependency / Open-source Management

<!-- aeos:section=dependency_management anchor=05-development--dependency-management content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

Tidak ada dependensi baru yang diharapkan. Bila diperlukan, wajib melalui lockfile (requirements.lock, frontend/package-lock.json) sesuai ADR-0093 dan disetujui manusia. Worker tidak mengubah lockfile dalam slice ini.

## Dev Environment

<!-- aeos:section=dev_environment anchor=05-development--dev-environment content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

Windows (PowerShell) dan macOS sesuai docs/SETUP_WINDOWS.md dan docs/SETUP_MACOS.md; tes dijalankan lokal oleh developer dan secara resmi oleh verifier tepercaya.

## Secrets

<!-- aeos:section=secrets anchor=05-development--secrets content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

Tidak ada secret di kode, fixture atau output; fixture memakai nilai sintetis yang jelas palsu; secret nyata hanya di OpenBao.

## CI/CD Requirements

<!-- aeos:section=ci_cd_requirements anchor=05-development--ci-cd-requirements content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

- Verifier tepercaya menjalankan suite penuh pada commit baseline sebelum T2 dimulai dan mencatat hasilnya sebagai bukti baseline (saat ini NOT_RUN)
- CI GitHub Actions yang ada (.github/workflows/ci.yml) harus hijau; tidak ada perubahan workflow dalam slice ini
- Tes baru backend (pytest) dan frontend (Vitest) masuk suite yang ada
- Validasi kontrak: tes validasi skema IC1/IC2 ditambahkan T2 di tests/setup_wizard/ bila suite contract-schema-validation belum ada (dikonfirmasi T1)
