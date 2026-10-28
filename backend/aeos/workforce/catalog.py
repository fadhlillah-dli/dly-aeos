"""Role catalog data (§15, §16, §17) — competence definitions, NOT running agents.

Text is taken from the master architecture tables; outputs/boundaries/verification keep the
master's wording (Indonesian) so the Organization page shows the same contract the design states.
Every entry is DESIGN-mode and NOT_EVALUATED until an evaluation (§43/§44) says otherwise.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

# §17 organisation view groups (projection only — no authority, no reporting-line permissions).
GROUPS: list[tuple[str, str]] = [
    ("product-ux", "Product & UX"),
    ("architecture-delivery", "Architecture & Delivery"),
    ("fe-api-data", "FE/API/Data"),
    ("security-privacy", "Security & Privacy"),
    ("platform-sre", "Platform & SRE"),
    ("qa-verification", "QA & Verification"),
    ("docs-release", "Docs & Release"),
    ("research-eval-cost", "Research/AI Evaluation/Cost"),
]
GROUP_LABELS = dict(GROUPS)

# §21: what no worker may change, whatever its role or title.
COMMON_FORBIDDEN: list[str] = [
    "Mengubah policy/MCI",
    "Mengubah approval records",
    "Mengubah grader/held-out tests",
    "Mengakses credential store",
    "Mengubah routing policy",
    "Mengubah audit/evidence final",
    "Mengubah pipeline tepercaya",
    "Menaikkan capability/permission miliknya sendiri",
    "Nested fan-out atau delegasi tanpa otorisasi scheduler/pemilik",
]

ROLE_PRACTICE_SOURCES = ["DLY_AEOS_Master_Architecture_Final.md"]


def _role(
    key: str, name: str, group: str, kind: str, section: str, *, outputs: list[str], boundaries: list[str],
    verification: list[str], description: str = "", engine_hint: str = "", forbidden: list[str] | None = None,
    failure_modes: list[str] | None = None, deterministic: bool = False,
) -> dict[str, Any]:
    return {
        "role_key": key,
        "name": name,
        "group": group,
        "kind": kind,
        "source_section": section,
        "description": description,
        "outputs": outputs,
        "boundaries": boundaries,
        "verification": verification,
        "forbidden_actions": forbidden if forbidden is not None else (COMMON_FORBIDDEN if kind != "HUMAN" else []),
        "failure_modes": failure_modes or [],
        "engine_hint": engine_hint,
        "deterministic_service": deterministic,
        "practice_sources": [*ROLE_PRACTICE_SOURCES, section],
    }


# ----------------------------------------------------------------------------- §15 planning/oversight
PLANNING_ROLES: list[dict[str, Any]] = [
    _role("primary-planner", "Primary Planner", "architecture-delivery", "PLANNER", "§15",
          engine_hint="Opus family, versi terkonfigurasi",
          outputs=["Proposal produk/arsitektur/DAG"],
          boundaries=["Tidak mengotorisasi publikasi"],
          verification=["Ditantang oleh Planning Challenger dan dinilai Final Planning Reviewer"],
          description="Menyusun proposal produk/arsitektur/DAG."),
    _role("planning-challenger", "Planning Challenger", "architecture-delivery", "REVIEWER", "§15",
          engine_hint="Fable tertinggi yang tersedia sesuai policy",
          outputs=["Kontradiksi, dependency hilang, risiko dan scope yang ditemukan"],
          boundaries=["Konteks review terpisah", "Menerima paket yang dinilai, bukan seluruh percakapan worker"],
          verification=["Temuan dibawa ke Planning Synthesizer sebagai DATA"],
          description="Mencari kontradiksi, dependency hilang, risiko dan scope; konteks review terpisah."),
    _role("planning-synthesizer", "Planning Synthesizer", "architecture-delivery", "PLANNER", "§15",
          engine_hint="Opus family sesuai baseline",
          outputs=["Versi rencana baru yang menggabungkan temuan"],
          boundaries=["Bukan reviewer penerima perubahannya sendiri"],
          verification=["Versi baru dinilai Final Planning Reviewer"],
          description="Menggabungkan temuan ke versi rencana baru."),
    _role("final-planning-reviewer", "Final Planning Reviewer", "architecture-delivery", "REVIEWER", "§15",
          engine_hint="Fable sesuai policy",
          outputs=["REVIEW + VERIFY + VERDICT untuk paket rencana"],
          boundaries=["Verdict review bukan human approval"],
          verification=["ReviewRecord §34 dengan evidence refs"],
          description="Memberi REVIEW + VERIFY + VERDICT untuk paket rencana."),
    _role("human-owner", "Human Owner / Approver", "architecture-delivery", "HUMAN", "§15",
          engine_hint="Manusia dengan AUTH yang sah",
          outputs=["Pilihan domain, persetujuan versi/target/scope, exception, keputusan bisnis"],
          boundaries=["Satu-satunya sumber AUTH; dicatat sebagai Decision terikat target + hash"],
          verification=["Decision record (actor, scope, target, version/hash, expiry)"],
          description="Memilih domain, menyetujui versi, target, scope, exception, dan keputusan bisnis.",
          forbidden=[]),
    _role("feature-architecture-reviewer", "Feature / Architecture Reviewer", "architecture-delivery", "REVIEWER",
          "§15", engine_hint="Claude tertinggi sesuai policy",
          outputs=["Review checkpoint: impact, contracts, evidence, regression dan integrasi"],
          boundaries=["Mengembalikan findings; tidak memperbaiki artifact yang ia setujui sendiri (§22)",
                      "Read-only tools/context, tanpa deploy/write token (§20)"],
          verification=["ReviewRecord §34; independent tests tetap diperlukan"],
          description="Memeriksa checkpoint, impact, contracts, evidence, regression dan integrasi."),
    _role("security-reviewer", "Security Reviewer", "security-privacy", "REVIEWER", "§15",
          engine_hint="Sesi reviewer terpisah sesuai risiko",
          outputs=["Temuan kontrol keamanan"],
          boundaries=["Tidak melakukan self-repair",
                      "Tidak menerima residual risk atas nama pemilik"],
          verification=["ReviewRecord §34 dengan evidence refs"],
          description="Memeriksa kontrol; tidak melakukan self-repair atau menerima residual risk atas nama pemilik."),
]

# ----------------------------------------------------------------------------- §16 executor roles (26)
EXECUTOR_ROLES: list[dict[str, Any]] = [
    _role("product-analyst", "Product Analyst", "product-ux", "EXECUTOR", "§16",
          outputs=["Problem", "Goals/non-goals", "Requirement", "User stories", "Acceptance criteria", "Metrik"],
          boundaries=["Tidak mengarang stakeholder atau keputusan bisnis"],
          verification=["AC dapat diuji dan ditelusuri ke input pemilik"]),
    _role("ux-research-assistant", "UX Research Assistant", "product-ux", "EXECUTOR", "§16",
          outputs=["Journey", "Persona berbasis input", "Isu usability"],
          boundaries=["Asumsi persona ditandai", "Tidak mengaku telah melakukan riset pengguna"],
          verification=["Setiap persona/isu menunjuk sumber input"]),
    _role("ux-ui-designer", "UX/UI Designer", "product-ux", "EXECUTOR", "§16",
          outputs=["Flow", "Wireframe", "Design tokens", "State/loading/error", "Aksesibilitas"],
          boundaries=["Satu TaskSpec; kontrak desain disepakati sebelum implementasi paralel (§22)"],
          verification=["Desain diverifikasi terhadap AC dan pengguna yang dimaksud"]),
    _role("solution-architect", "Solution Architect", "architecture-delivery", "EXECUTOR", "§16",
          outputs=["Component/data/integration design", "RFC/TDD"],
          boundaries=["Perubahan kontrak berdampak tinggi masuk keputusan/review"],
          verification=["Review arsitektur dan Decision untuk kontrak berdampak tinggi"]),
    _role("engineering-delivery-analyst", "Engineering Delivery Analyst", "architecture-delivery", "EXECUTOR", "§16",
          outputs=["Usulan milestone", "Dependency", "Kapasitas", "Risiko delivery"],
          boundaries=["Tidak mengubah prioritas/scope/otorisasi pemilik atau state scheduler"],
          verification=["Usulan menjadi DATA untuk planning; scheduler tetap deterministik"]),
    _role("feature-implementer", "Feature Implementer", "fe-api-data", "EXECUTOR", "§16",
          outputs=["Implementasi satu irisan fitur end-to-end"],
          boundaries=["Memiliki satu TaskSpec", "Tidak menjadi project manager kedua"],
          verification=["Verifier independen + review checkpoint; exit 0 hanya membawa ke REVIEW"]),
    _role("frontend-engineer", "Frontend Engineer", "fe-api-data", "EXECUTOR", "§16",
          outputs=["UI", "Form", "State", "API integration", "Responsive behavior"],
          boundaries=["Satu TaskSpec; scope path TaskSpec"],
          verification=["Browser test", "Visual evidence", "a11y"]),
    _role("backend-api-engineer", "Backend/API Engineer", "fe-api-data", "EXECUTOR", "§16",
          outputs=["Domain logic", "API", "Validation", "Authz", "Error handling"],
          boundaries=["Satu TaskSpec; scope path TaskSpec"],
          verification=["Unit/integration/contract tests", "Tes negatif"]),
    _role("db-data-engineer", "DB/Data Engineer", "fe-api-data", "EXECUTOR", "§16",
          outputs=["Schema", "Migration", "Query", "Isolation", "Seed", "Retention"],
          boundaries=["Credential migration dipisah dari runtime", "Shared schema diserialisasi (§19)"],
          verification=["Uji migration/rollback/restore"]),
    _role("integration-engineer", "Integration Engineer", "fe-api-data", "EXECUTOR", "§16",
          outputs=["Adapter", "Contract test provider"],
          boundaries=["Target/tenant dan idempotency wajib", "Tidak memakai akses global"],
          verification=["Contract test terhadap provider/target yang diizinkan"]),
    _role("data-ai-feature-engineer", "Data/AI Feature Engineer", "fe-api-data", "EXECUTOR", "§16",
          outputs=["Pipeline/data/model integration untuk produk yang memang memerlukannya"],
          boundaries=["Tidak mengubah routing/policy control plane"],
          verification=["Quality/privacy/eval terpisah sesuai task"]),
    _role("security-engineer", "Security Engineer", "security-privacy", "EXECUTOR", "§16",
          outputs=["Threat model", "Secure implementation", "Finding triage"],
          boundaries=["Tidak menandatangani risk acceptance atau pentest yang tidak dilakukan"],
          verification=["Temuan berbukti; review keamanan terpisah"]),
    _role("privacy-grc-analyst", "Privacy/GRC Analyst", "security-privacy", "EXECUTOR", "§16",
          outputs=["Data inventory", "Assessment", "Control/evidence mapping"],
          boundaries=["Mapping bukan bukti compliance effectiveness"],
          verification=["Evidence effectiveness terpisah sebelum klaim compliance (§34)"]),
    _role("supply-chain-analyst", "Supply-chain Analyst", "security-privacy", "EXECUTOR", "§16",
          outputs=["Dependency/license/SBOM/build provenance review"],
          boundaries=["Perubahan dependency diperiksa", "Tidak auto-upgrade seluruh proyek"],
          verification=["SBOM/license scan dengan pin/digest"]),
    _role("platform-devsecops-engineer", "Platform/DevSecOps Engineer", "platform-sre", "EXECUTOR", "§16",
          outputs=["CI", "IaC", "Build/deploy/runbook proposal"],
          boundaries=["Policy, IAM, pipeline, secret, deploy target adalah area terbatas",
                      "Patch area terbatas dikarantina untuk review khusus (§21)"],
          verification=["Pemeriksaan dampak tinggi meskipun diff kecil (§31)"]),
    _role("qa-test-designer", "QA/Test Designer", "qa-verification", "EXECUTOR", "§16",
          outputs=["Test Plan", "Acceptance scenarios", "Edge/negative/adversarial cases"],
          boundaries=["Suite penerimaan disimpan terpisah dari akses tulis implementer"],
          verification=["Suite tepercaya read-only bagi implementer"]),
    _role("independent-verifier", "Independent Verifier", "qa-verification", "EXECUTOR", "§16",
          outputs=["Hasil suite terpercaya pada hasil dan merged state"],
          boundaries=["Sebaiknya runner deterministik", "Bukti dikumpulkan sendiri"],
          verification=["EvidenceArtifact trusted dari collector/verifier (§34)"]),
    _role("technical-writer", "Technical Writer", "docs-release", "EXECUTOR", "§16",
          outputs=["PRD/RFC/TDD narrative", "Dokumen perubahan dan release"],
          boundaries=["Menulis proposal/delta, bukan status runtime atau approval"],
          verification=["Dokumen ditinjau terhadap artifact yang diterima"]),
    _role("diagram-visual-author", "Diagram/Visual Author", "docs-release", "EXECUTOR", "§16",
          outputs=["Diagram source", "Wireframe", "Screenshot manifest"],
          boundaries=["Screenshot berasal dari aplikasi nyata"],
          verification=["Diagram diperiksa terhadap kontrak"]),
    _role("release-coordinator", "Release Coordinator", "docs-release", "EXECUTOR", "§16",
          outputs=["Release Plan", "Evidence index", "Rollback checklist"],
          boundaries=["Menyiapkan paket", "Human-only merge/production"],
          verification=["Evidence index menunjuk bukti terverifikasi"]),
    _role("sre-operations-assistant", "SRE/Operations Assistant", "platform-sre", "EXECUTOR", "§16",
          outputs=["SLI/SLO", "Monitoring", "Runbook", "Diagnostic proposal"],
          boundaries=["Awalnya read-only", "Tindakan operasional memerlukan capability/otorisasi"],
          verification=["Runbook diuji; probe deterministik"]),
    _role("incident-analyst", "Incident Analyst", "platform-sre", "EXECUTOR", "§16",
          outputs=["Timeline", "Impact", "Hipotesis", "Root cause berbukti", "Corrective action"],
          boundaries=["Bukti incident dikendalikan", "Tidak menyatakan root cause tanpa dukungan"],
          verification=["Root cause menunjuk evidence"]),
    _role("retirement-assistant", "Retirement Assistant", "platform-sre", "EXECUTOR", "§16",
          outputs=["Inventory closure", "Dependency/data/access checklist"],
          boundaries=["Tidak menghapus layanan, data, atau backup tanpa owner auth"],
          verification=["Verifikasi final oleh pemilik yang sah (§33)"]),
    _role("context-curator", "Context Curator", "research-eval-cost", "EXECUTOR", "§16",
          outputs=["Paket konteks terbatas", "Catatan keputusan"],
          boundaries=["Tidak mengubah MCI/policy/memory dari input tidak terpercaya"],
          verification=["Provenance dan scope setiap item konteks (§24)"]),
    _role("model-evaluation-analyst", "Model Evaluation Analyst", "research-eval-cost", "EXECUTOR", "§16",
          outputs=["Ringkasan eksperimen dan kegagalan model"],
          boundaries=["Tidak mengubah grader atau hasil eksperimen"],
          verification=["Hasil eval berasal dari suite terproteksi (§43)"]),
    _role("finops-quota-analyst", "FinOps/Quota Analyst", "research-eval-cost", "EXECUTOR", "§16",
          outputs=["Analisis penggunaan", "Opsi efisiensi"],
          boundaries=["Angka ledger berasal dari kode/provider, bukan tebakan model"],
          verification=["Angka ditelusuri ke usage ledger (§42)"]),
]

# ----------------------------------------------------------------------------- §16 deterministic services
SERVICE_ROLES: list[dict[str, Any]] = [
    _role("scheduler", "Scheduler", "platform-sre", "SERVICE", "§16", deterministic=True,
          outputs=["Readiness, lease, reservasi, admission, cancel"],
          boundaries=["State machine eksplisit §14; tidak memakai LLM"],
          verification=["Tes transisi legal/ilegal, lease atomik, rekonsiliasi"],
          description="Layanan deterministik; model dapat menjelaskan keluarannya tetapi tidak mendefinisikan state."),
    _role("policy-enforcer", "Policy Enforcer", "security-privacy", "SERVICE", "§16", deterministic=True,
          outputs=["Keputusan interlock allow/deny"],
          boundaries=["Fail closed; tidak memakai LLM"],
          verification=["Tes interlock negatif"]),
    _role("credential-broker", "Credential Broker", "security-privacy", "SERVICE", "§16", deterministic=True,
          outputs=["Capability token terbatas per run", "Model proxy", "Push branch aeos/* dan PR"],
          boundaries=["Worker tidak menerima credential upstream", "Tidak ada operasi merge"],
          verification=["Tes token hash/scope/expiry/revocation, refspec, credential escape"]),
    _role("publication-worker", "Publication Worker", "docs-release", "SERVICE", "§16", deterministic=True,
          outputs=["Publikasi dari paket yang disetujui"],
          boundaries=["Tidak memilih versi/target baru"],
          verification=["Version/hash binding, idempotency, rekonsiliasi"]),
    _role("evidence-collector", "Evidence Collector", "qa-verification", "SERVICE", "§16", deterministic=True,
          outputs=["EvidenceArtifact dengan hash, waktu dan identitas collector"],
          boundaries=["Worker attachments tetap untrusted"],
          verification=["Hash dan provenance setiap artifact"]),
    _role("budget-counter", "Budget Counter", "research-eval-cost", "SERVICE", "§16", deterministic=True,
          outputs=["Reservasi dan ledger penggunaan"],
          boundaries=["Atomik; admission ditolak bila state budget tidak dapat dipercaya"],
          verification=["Tes reservasi bersamaan"]),
    _role("stop-kill-controller", "Stop/Kill Controller", "platform-sre", "SERVICE", "§16", deterministic=True,
          outputs=["Penghentian process tree/sandbox dan pencabutan capability"],
          boundaries=["Di luar model; tidak membatalkan side effect eksternal yang sudah terjadi"],
          verification=["Tes cancel membunuh child dan grandchild"]),
]

# ----------------------------------------------------------------------------- §17/§46 competence details
# inputs (what the role consumes), example outputs (shape of a good result) and failure modes (what the
# reviewer/verifier looks for) per role. Practice sources beyond the master are references only; nothing is
# imported or auto-updated from them (§45). §46 "sumber praktik yang sah": each one is a NAMED, verifiable
# publication (title, publisher/edition, canonical URL) - never a generic label. The links are stated here, not
# checked live by the platform.
EXTERNAL_PRACTICE = {
    "product-ux": "Practice reference (reference only, §45): Nielsen Norman Group, '10 Usability Heuristics for User "
                  "Interface Design' (https://www.nngroup.com/articles/ten-usability-heuristics/); W3C, 'Web Content "
                  "Accessibility Guidelines (WCAG) 2.2' (https://www.w3.org/TR/WCAG22/)",
    "architecture-delivery": "Practice reference (reference only, §45): Michael Nygard, 'Documenting Architecture "
                             "Decisions' (2011, https://cognitect.com/blog/2011/11/15/documenting-architecture-"
                             "decisions); ISO/IEC/IEEE 42010:2022 'Architecture description'; DORA, 'DORA metrics' "
                             "(https://dora.dev/)",
    "fe-api-data": "Practice reference (reference only, §45): OWASP 'Application Security Verification Standard 4.0.3' "
                   "(https://owasp.org/www-project-application-security-verification-standard/); OpenAPI Initiative, "
                   "'OpenAPI Specification 3.1.0' (https://spec.openapis.org/oas/v3.1.0); OWASP 'API Security Top 10 "
                   "2023' (https://owasp.org/API-Security/)",
    "security-privacy": "Practice reference (reference only, §45): NIST SP 800-218 'Secure Software Development "
                        "Framework (SSDF) 1.1' (https://csrc.nist.gov/pubs/sp/800/218/final); OWASP 'SAMM v2' "
                        "(https://owaspsamm.org/); Article 29 WP / EDPB 'Guidelines on DPIA' WP248 rev.01; SLSA "
                        "'Specification v1.0' (https://slsa.dev/spec/v1.0/)",
    "platform-sre": "Practice reference (reference only, §45): Beyer et al., 'Site Reliability Engineering' "
                    "(O'Reilly 2016, https://sre.google/sre-book/table-of-contents/); NIST SP 800-218 SSDF 1.1 "
                    "(https://csrc.nist.gov/pubs/sp/800/218/final)",
    "qa-verification": "Practice reference (reference only, §45): ISTQB 'Certified Tester Foundation Level Syllabus "
                       "v4.0' (2023, https://www.istqb.org/); ISO/IEC/IEEE 29119-4:2021 'Test techniques'",
    "docs-release": "Practice reference (reference only, §45): Daniele Procida, 'Diataxis' (https://diataxis.fr/); "
                    "'Semantic Versioning 2.0.0' (https://semver.org/); 'Keep a Changelog 1.1.0' "
                    "(https://keepachangelog.com/en/1.1.0/)",
    "research-eval-cost": "Practice reference (reference only, §45): FinOps Foundation, 'FinOps Framework' "
                          "(https://www.finops.org/framework/); Liang et al., 'Holistic Evaluation of Language Models' "
                          "(arXiv:2211.09110, https://arxiv.org/abs/2211.09110); NIST AI 100-1 'AI Risk Management "
                          "Framework 1.0' (https://www.nist.gov/itl/ai-risk-management-framework)",
}
_D = tuple[list[str], list[str], list[str]]  # inputs, example outputs, failure modes
ROLE_DETAILS: dict[str, _D] = {
    "product-analyst": (["Prompt/ide pemilik", "Lampiran (DATA tak tepercaya)", "Konteks proyek"],
                        ["PRD dengan problem, goals/non-goals, user stories dan AC yang dapat diuji"],
                        ["Mengarang stakeholder/keputusan bisnis", "AC tidak dapat diuji", "Scope creep"]),
    "ux-research-assistant": (["Input pemilik tentang pengguna", "Isu usability yang dilaporkan"],
                              ["Journey + persona dengan asumsi bertanda dan sumber per persona"],
                              ["Persona tanpa sumber", "Mengaku riset pengguna dilakukan", "Asumsi tidak ditandai"]),
    "ux-ui-designer": (["PRD/AC", "Design tokens yang disepakati"],
                       ["Flow + wireframe + state loading/error/empty + catatan a11y"],
                       ["State error/loading hilang", "Kontras/a11y tidak diperiksa", "Desain berubah tanpa kontrak"]),
    "solution-architect": (["PRD", "Inventory repo", "Kontrak yang ada"],
                           ["RFC/TDD dengan alternatif, dampak dan rollback"],
                           ["Kontrak berdampak tinggi tanpa keputusan", "Alternatif tidak dibandingkan"]),
    "engineering-delivery-analyst": (["DAG TaskSpec", "Status runtime (read-only)"],
                                     ["Usulan milestone/dependency/risiko delivery sebagai DATA"],
                                     ["Mengubah prioritas/state scheduler", "Estimasi diklaim sebagai fakta"]),
    "feature-implementer": (["TaskSpec (goal, AC, allowed_paths)", "Base commit", "Kontrak antarmuka"],
                            ["Patch irisan fitur + tes yang memenuhi AC"],
                            ["Menulis di luar allowed_paths", "Klaim selesai tanpa bukti", "Mengubah tes penerimaan"]),
    "frontend-engineer": (["TaskSpec", "Kontrak API", "Design tokens"],
                          ["UI/form/state + browser test + visual evidence"],
                          ["State error/loading hilang", "Integrasi API tanpa kontrak", "a11y diabaikan"]),
    "backend-api-engineer": (["TaskSpec", "Kontrak API/schema"],
                             ["Endpoint + validasi + authz + tes unit/integrasi/negatif"],
                             ["Authz hilang", "Error handling bocor detail", "Tanpa tes negatif"]),
    "db-data-engineer": (["TaskSpec", "Schema yang ada"],
                         ["Migration + rollback + tes migration/restore"],
                         ["Migration tanpa rollback", "Dua head migration", "Credential migration di runtime"]),
    "integration-engineer": (["TaskSpec", "Kontrak provider", "Target/tenant yang diizinkan"],
                             ["Adapter + contract test provider + kunci idempotensi"],
                             ["Akses global", "Target/tenant tidak terikat", "Tanpa idempotensi"]),
    "data-ai-feature-engineer": (["TaskSpec", "Data yang diizinkan"],
                                 ["Integrasi data/model + eval kualitas/privasi terpisah"],
                                 ["Mengubah routing/policy", "Data pribadi tanpa assessment"]),
    "security-engineer": (["TaskSpec", "Threat model", "Temuan"],
                          ["Threat model + implementasi aman + triage berbukti"],
                          ["Menandatangani risk acceptance", "Mengaku pentest dilakukan"]),
    "privacy-grc-analyst": (["Data inventory", "Kontrol"],
                            ["Assessment + mapping kontrol/bukti"],
                            ["Mapping diklaim sebagai bukti efektivitas", "Data pribadi tidak terinventarisasi"]),
    "supply-chain-analyst": (["Lockfile/manifest dependency"],
                             ["Review dependency/license/SBOM dengan pin/digest"],
                             ["Auto-upgrade seluruh proyek", "License tidak diperiksa"]),
    "platform-devsecops-engineer": (["TaskSpec", "Pipeline/IaC yang ada"],
                                    ["Perubahan CI/IaC + runbook proposal"],
                                    ["Mengubah policy/IAM/secret tanpa karantina", "Diff kecil berdampak tinggi"]),
    "qa-test-designer": (["PRD/AC", "Test Plan"],
                         ["Skenario penerimaan + kasus edge/negatif/adversarial"],
                         ["Suite dapat ditulis implementer", "Hanya happy path"]),
    "independent-verifier": (["Candidate commit", "Suite tepercaya"],
                             ["Laporan suite tepercaya dengan hash artefak"],
                             ["Bukti dari worker dianggap tepercaya", "Exit 0 dianggap DONE"]),
    "technical-writer": (["Artefak yang diterima", "Delta perubahan"],
                         ["Narasi PRD/RFC/TDD/release yang menunjuk artefak"],
                         ["Menulis status runtime/approval", "Dokumen tanpa sumber"]),
    "diagram-visual-author": (["Kontrak/manifest/schema"],
                              ["Sumber diagram Mermaid + manifest screenshot dari aplikasi nyata"],
                              ["Diagram tidak cocok kontrak", "Screenshot palsu/mock tanpa label"]),
    "release-coordinator": (["Evidence index", "Release Plan"],
                            ["Paket rilis + checklist rollback"],
                            ["Merge/produksi tanpa manusia", "Evidence index menunjuk bukti tak terverifikasi"]),
    "sre-operations-assistant": (["SLI/telemetri (read-only)", "Runbook"],
                                 ["Usulan SLI/SLO + runbook teruji"],
                                 ["Tindakan operasional tanpa capability", "Runbook tidak diuji"]),
    "incident-analyst": (["Timeline/log terkendali"],
                         ["Timeline + dampak + root cause berbukti + corrective action"],
                         ["Root cause tanpa bukti", "Bukti incident diubah"]),
    "retirement-assistant": (["Checklist retirement", "Inventory proyek"],
                             ["Checklist closure dependency/data/access"],
                             ["Menghapus data/backup tanpa owner auth", "Klaim selesai sebelum verifikasi pemilik"]),
    "context-curator": (["Sumber konteks dengan provenance"],
                        ["Paket konteks terbatas dengan provenance per item"],
                        ["Mengubah MCI/policy/memory dari input tak tepercaya", "Item tanpa provenance"]),
    "model-evaluation-analyst": (["Hasil eval dari suite terproteksi"],
                                 ["Ringkasan eksperimen + kegagalan model"],
                                 ["Mengubah grader/hasil", "Membuang sampel gagal"]),
    "finops-quota-analyst": (["Usage ledger"],
                             ["Analisis penggunaan + opsi efisiensi yang ditelusuri ke ledger"],
                             ["Angka tebakan model", "Fallback berbayar diam-diam"]),
}
_KIND_INPUTS = {"PLANNER": ["Konteks planning (DATA)", "Kebijakan tetap control plane"],
                "REVIEWER": ["Paket yang dinilai saja (bukan percakapan worker)"],
                "SERVICE": ["Record kanonik control plane"], "HUMAN": ["Paket keputusan terikat target + hash"]}
_KIND_FAILURES = {"PLANNER": ["Mengarang owner/stakeholder", "DAG siklik atau tanpa allowed_paths"],
                  "REVIEWER": ["Self-review", "Verdict dianggap approval", "Memperbaiki artefak sendiri"],
                  "SERVICE": ["State tidak deterministik", "Fail-open saat modul hilang"], "HUMAN": []}
_KIND_SCOPE: dict[str, dict[str, Any]] = {
    "EXECUTOR": {"write": "TaskSpec.allowed_paths only", "egress": "TaskSpec.egress_allowed (default deny)",
                 "credentials": "none (broker capability per run)", "domains": ["PERSONAL", "OFFICE"],
                 "project_ids": "*"},
    "PLANNER": {"write": "none (output is DATA)", "domains": ["PERSONAL", "OFFICE"], "project_ids": "*"},
    "REVIEWER": {"write": "none (read-only)", "context": "checkpoint package only", "domains": ["PERSONAL", "OFFICE"],
                 "project_ids": "*"},
    "SERVICE": {"llm": False, "domains": ["PERSONAL", "OFFICE"], "project_ids": "*"},
    "HUMAN": {"auth": "Decision rows bound to target + hash"},
}


def _enrich(role: dict[str, Any]) -> dict[str, Any]:
    inputs, examples, failures = ROLE_DETAILS.get(role["role_key"], ([], [], []))
    role["inputs"] = inputs or list(_KIND_INPUTS.get(role["kind"], []))
    role["example_outputs"] = examples or list(role["outputs"])
    role["failure_modes"] = role["failure_modes"] or failures or list(_KIND_FAILURES.get(role["kind"], []))
    role["allowed_scope"] = dict(_KIND_SCOPE.get(role["kind"], {}))
    ext = EXTERNAL_PRACTICE.get(role["group"])
    if ext and ext not in role["practice_sources"]:
        role["practice_sources"] = [*role["practice_sources"], ext]
    return role


PLANNING_ROLES = [_enrich(r) for r in PLANNING_ROLES]
EXECUTOR_ROLES = [_enrich(r) for r in EXECUTOR_ROLES]
SERVICE_ROLES = [_enrich(r) for r in SERVICE_ROLES]
EXECUTOR_ROLE_KEYS = frozenset(r["role_key"] for r in EXECUTOR_ROLES)

ALL_ROLES: list[dict[str, Any]] = [*PLANNING_ROLES, *EXECUTOR_ROLES, *SERVICE_ROLES]

# ----------------------------------------------------------------------------- AgentDefinitions (§17)
# §17 AgentDefinition.tools is the agent's tool envelope, enforced at admission: it must cover every tool the
# runtime grants a worker (aeos.worker_runtime.opencode.BASE_ALLOW); bash/webfetch additionally need the agent's
# limits to permit what the TaskSpec asks for.
DEFAULT_IMPLEMENTER_TOOLS = ["read", "glob", "grep", "list", "edit", "write", "todowrite"]

AGENT_DEFINITIONS: list[dict[str, Any]] = [
    {
        "agent_key": "opencode-implementer",
        "role_key": "feature-implementer",
        "harness": "opencode",
        "description": "OpenCode - mesin eksekusi pekerja untuk satu TaskSpec dalam sandbox per run.",
        "route_policy": {"pool": "WORKER", "selection": "aeos.routing.facade.select_route",
                         "fallback": "approved-routes-only", "paid_fallback": False},
        "tools": DEFAULT_IMPLEMENTER_TOOLS,
        # upper bounds the scheduler applies (its own cap stays 1 unless a POLICY decision allows 2)
        "limits": {"max_attempts": 2, "max_active": 2, "nested_fan_out": False,
                   "bash": "deny unless TaskSpec.tools_allowed", "webfetch": "deny unless TaskSpec.egress_allowed"},
        "allowed_scope": {"paths": "TaskSpec.allowed_paths", "forbidden": "TaskSpec.forbidden_paths + protected paths",
                          "credentials": "capability token via broker only", "domains": ["PERSONAL", "OFFICE"],
                          "project_ids": "*"},
        "enabled": False,
        "deterministic": False,
        "enabled_reason": "Nonaktif sampai route worker APPROVED dan sandbox yang disyaratkan READY.",
    },
    {
        "agent_key": "claude-checkpoint-reviewer",
        "role_key": "feature-architecture-reviewer",
        "harness": "claude-native",
        "description": "Claude Code (native Max login) - reviewer checkpoint; read-only, tanpa write/deploy token.",
        "route_policy": {"pool": "SUPERVISOR", "engine": "official claude binary", "api": "none"},
        "tools": ["read"],
        "limits": {"repairs": False, "write": False},
        "allowed_scope": {"context": "checkpoint package only", "domains": ["PERSONAL", "OFFICE"], "project_ids": "*"},
        "enabled": False,
        "deterministic": False,
        "enabled_reason": "Nonaktif sampai CAN_REVIEW_WITH_MAX READY (claude_bridge).",
    },
    {
        "agent_key": "independent-verifier",
        "role_key": "independent-verifier",
        "harness": "verifier-runner",
        "description": "Runner deterministik yang menjalankan suite tepercaya di luar candidate workspace.",
        "route_policy": {"llm": False},
        "tools": ["trusted-suite"],
        "limits": {"suite": "read-only for implementers"},
        "allowed_scope": {"workspace": "outside candidate", "domains": ["PERSONAL", "OFFICE"], "project_ids": "*"},
        "enabled": False,
        "deterministic": True,
        "enabled_reason": "Nonaktif sampai modul verification menyatakan runner siap.",
    },
]

IMPLEMENTER_AGENT_KEY = "opencode-implementer"
REVIEWER_AGENT_KEYS = frozenset({"claude-checkpoint-reviewer"})

# ----------------------------------------------------------------------------- category agents (ADR-0126)
# Owner decision R31 (2026-10-28): "best models first ... matched to each role's needs". One OpenCode agent per
# LiveBench category, each serving the executor roles in ``allowed_scope.roles``. Each one is the implementer's
# configuration (same harness, worker prompt, tools, limits, pool and scope) plus that role list and an ordered
# ``route_policy.preferred_routes`` that only RE-ORDERS routes that are already eligible (ADR-0124). Seeded DISABLED:
# enabling one is the owner's ``aeos work agent-enable`` (ADR-0126, docs/OWNER_ACTIONS.md 1.11).
ROLE_ROUTE_PREFERENCES_FILE = Path(__file__).with_name("role_route_preferences.json")  # scripts/role_route_ranking.py

# agent key -> (LiveBench category, served roles; the first one is the agent's descriptive role_key, description)
CATEGORY_AGENTS: dict[str, tuple[str, tuple[str, ...], str]] = {
    "agentic-coder": ("Long Agentic", (
        "backend-api-engineer", "frontend-engineer", "db-data-engineer", "integration-engineer",
        "data-ai-feature-engineer", "platform-devsecops-engineer", "security-engineer"),
        "pekerjaan rekayasa yang mengubah kode lintas banyak langkah"),
    "coder": ("Coding", ("qa-test-designer",), "rancangan dan kode uji"),
    "designer": ("Design", ("ux-ui-designer", "diagram-visual-author"), "desain UI/UX dan sumber diagram"),
    "reasoner": ("Reasoning", (
        "solution-architect", "engineering-delivery-analyst", "incident-analyst", "sre-operations-assistant"),
        "arsitektur, analisis delivery, insiden dan operasi"),
    "researcher": ("Research", ("ux-research-assistant", "product-analyst", "supply-chain-analyst"),
                   "riset pengguna, produk dan rantai pasok"),
    "writer": ("Instruction Following", (
        "technical-writer", "privacy-grc-analyst", "release-coordinator", "retirement-assistant", "context-curator"),
        "dokumen yang mengikuti instruksi dan format"),
    "analyst": ("Data Analysis", ("model-evaluation-analyst", "finops-quota-analyst"), "analisis data evaluasi dan kuota"),
}


def _route_preferences() -> dict[str, dict[str, Any]]:
    """The reviewed ranking per category agent. A missing or unreadable file yields no preference (routing then
    orders by its own rules); it never makes a route eligible or ineligible."""
    try:
        data = json.loads(ROLE_ROUTE_PREFERENCES_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    agents = data.get("agents") if isinstance(data, dict) else None
    return {str(k): v for k, v in (agents or {}).items() if isinstance(v, dict)}


def _category_agent(agent_key: str, prefs: dict[str, dict[str, Any]]) -> dict[str, Any]:
    category, roles, what = CATEGORY_AGENTS[agent_key]
    implementer = next(a for a in AGENT_DEFINITIONS if a["agent_key"] == IMPLEMENTER_AGENT_KEY)
    pref = prefs.get(agent_key) or {}
    return {
        "agent_key": agent_key,
        "role_key": roles[0],
        "harness": implementer["harness"],
        "description": f"OpenCode - agen kategori {category} (LiveBench) untuk {what}; prompt, tools dan sandbox "
                       "sama dengan opencode-implementer.",
        "route_policy": {**implementer["route_policy"],
                         "preferred_routes": [str(k) for k in pref.get("preferred_routes") or []],
                         "preferred_routes_source": {"file": "aeos/workforce/role_route_preferences.json",
                                                     "category": pref.get("category"), "release": pref.get("release")}},
        "tools": list(implementer["tools"]),
        "limits": dict(implementer["limits"]),
        "allowed_scope": {**implementer["allowed_scope"], "roles": list(roles)},
        "enabled": False,
        "deterministic": False,
        "enabled_reason": "Nonaktif sampai pemilik mengaktifkannya (aeos work agent-enable, ADR-0126).",
    }


_PREFS = _route_preferences()
AGENT_DEFINITIONS += [_category_agent(k, _PREFS) for k in CATEGORY_AGENTS]


def served_roles(allowed_scope: dict[str, Any] | None, role_key: str | None) -> list[str]:
    """The executor roles an agent serves: ``allowed_scope.roles`` when it lists them, else its own role_key."""
    roles = (allowed_scope or {}).get("roles")
    if isinstance(roles, list) and roles:
        return [str(r) for r in roles]
    return [role_key] if role_key else []


def role_agent_map() -> dict[str, list[str]]:
    """role_key -> the agent keys of AGENT_DEFINITIONS that serve it (a reviewed catalog has exactly one)."""
    out: dict[str, list[str]] = {}
    for spec in AGENT_DEFINITIONS:
        for role in served_roles(spec.get("allowed_scope"), spec.get("role_key")):
            out.setdefault(role, []).append(spec["agent_key"])
    return out
