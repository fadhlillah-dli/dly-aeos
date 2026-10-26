import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { MemoryRouter } from "react-router-dom";
import type { GatesResponse, ProviderCaps } from "./api";
import { DeliveryPanel } from "./DeliveryPanel";
import { EvidenceDrawer } from "./EvidenceDrawer";
import { GatesPanel } from "./GatesPanel";
import { ScreenshotsPanel } from "./ScreenshotsPanel";

/**
 * W7b UI lane 1: verifier report (impact checks, new step checks, tamper neutralisation, result seals), UI-state
 * screenshots, provider enforcement test + actions, promotion block. Every value comes from the API fixture below.
 */

const auth = { isAdmin: true, domain: "PERSONAL", projectId: null, session: null };
vi.mock("../../auth/AuthContext", () => ({ useAuth: () => auth }));

type Route = unknown | ((init?: RequestInit) => unknown);
let calls: { url: string; method: string; body: unknown }[] = [];

/** First matching prefix wins: list more specific prefixes first. */
function respond(routes: Record<string, Route>) {
  calls = [];
  globalThis.fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const method = (init?.method ?? "GET").toUpperCase();
    calls.push({ url, method, body: init?.body ? JSON.parse(String(init.body)) : undefined });
    const key = Object.keys(routes).find((k) => url.startsWith(k));
    if (!key) return new Response(JSON.stringify({ error: { code: "NOT_FOUND", message: "nf" } }), { status: 404 });
    const r = routes[key];
    return new Response(JSON.stringify(typeof r === "function" ? (r as (i?: RequestInit) => unknown)(init) : r), { status: 200 });
  }) as typeof fetch;
}

const posts = (prefix: string) => calls.filter((c) => c.method === "POST" && c.url.startsWith(prefix));

function wrap(ui: ReactNode, path = "/evidence") {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[path]}>{ui}</MemoryRouter>
    </QueryClientProvider>,
  );
}

const now = () => new Date().toISOString();

function evidence(over: Record<string, unknown>) {
  return {
    id: "ev-x", project_id: "p1", domain: "PERSONAL", subject_type: "run", subject_id: "run-1", run_id: "run-1", task_id: "task-1",
    claim: "claim", kind: "VERIFICATION_REPORT", origin: "verifier", collector_identity: "aeos-verifier/4", trusted: true,
    content_hash: "sha256:" + "4".repeat(64), content_type: "application/json", classification: "INTERNAL", has_content: true, size_bytes: 10,
    evidence_level: "E2", level_explanation: "Uji terkendali", level_requested: "E2", level_reason: null, environment: {}, created_at: now(),
    mode: "LIVE", ...over,
  };
}

const REPORT = {
  status: "QUARANTINED",
  summary: "VERIFIER_TAMPER: 1 foreign control-plane row(s) neutralised",
  verifier_isolation: "WEAK",
  verifier_isolation_note: "process runner",
  tests_passed: 120, tests_failed: 0, tests_skipped: 3,
  impact_categories: ["api", "frontend"],
  impact_checks: {
    api: { paths: ["backend/app/api.py"], checks: { negative_tests: "PASS", adversarial_pack: "PASS", contract_diff: "FAIL", integration_tests: "NOT_RUN" },
      executed: ["adversarial_pack", "contract_diff", "negative_tests"], status: "EXECUTED" },
    frontend: { paths: ["frontend/src/App.tsx"], checks: { frontend_tests: "NOT_PROVEN", ui_a11y: "NOT_RUN" }, executed: [], status: "NOT_PROVEN" },
  },
  checks: {
    trusted_suite: { status: "PASS", summary: "120 passed", evidence_id: null },
    negative_tests: { status: "PASS", summary: "2 refusal tests", evidence_id: "ev-neg" },
    contract_tests: { status: "PASS", summary: "selected", evidence_id: null },
    dependency_env: { status: "NOT_RUN", summary: "no hash pins", evidence_id: null },
    db_credentials: { status: "NOT_PROVEN", summary: "no migration credential ref", evidence_id: null },
    ui_a11y: { status: "NOT_PROVEN", summary: "render error", evidence_id: "ev-a11y" },
  },
  reasons: [],
  not_proven: ["impact categories without an executed verifier check: frontend (see impact_checks on the report)"],
  // the report's tamper section as the verifier writes it: no status key of its own
  tamper: {
    findings: [{ kind: "db", path: "decisions:d-9", change: "added" }],
    controller: { changed: [], checked_files: 120 },
    trust_tables: { status: "FAIL", foreign: [{ table: "decisions", id: "d-9", change: "added" }] },
    step_containment: { survivors: [] },
    neutralized: {
      actions: [{ table: "decisions", id: "d-9", change: "added", action: "INVALIDATED" }],
      manual: [{ table: "users", id: "u-1", change: "modified", remedy: "review the owner's role and scope" }],
      failed: [],
      not_listed: 0,
      counts: { actions: 1, manual: 1, failed: 0 },
      integrity_hold: {
        admission_paused: true, status: "OK", reason: "VERIFIER_TAMPER integrity hold (run run-1)",
        controller_changes: [{ path: "policy/office-defaults.yaml", change: "modified" }],
        remedies: [{ path: "policy/office-defaults.yaml", remedy: "policy file changed: compare it with git" }],
        resume: "after reconciling: aeos work resume",
      },
    },
  },
  result_seal: {
    trusted_suite: { status: "PASS", problems: [], other_session_lines: 2, key_consumed: true },
    candidate_tests: { status: "MISSING", problems: ["no sealed session end"], other_session_lines: 0, key_consumed: null },
  },
  residual_risks: ["same OS user"],
};

const A11Y = {
  suite: "ui-a11y", status: "NOT_PROVEN", probe: "verifier a11y probe (WCAG subset; not axe-core)", browser: "chromium 140",
  states: [
    { state: "empty", viewport: "mobile", violations: [], page_errors: [], answered_by: "candidate-app" },
    { state: "loading", viewport: "desktop", violations: [], page_errors: ["TypeError: x"], answered_by: "verifier" },
  ],
  violations: [{ state: "error", viewport: "mobile", rule: "image-alt", wcag: "1.1.1", target: "img", detail: "no alt" }],
  unproven_states: ["loading/desktop"],
  not_covered: ["colour contrast"],
};

afterEach(() => {
  auth.isAdmin = true;
  auth.domain = "PERSONAL";
});

describe("verification report view (E1 impact checks, E2 tamper + seals)", () => {
  function routes() {
    return {
      "/api/evidence/ev-a11y": { ...evidence({ id: "ev-a11y", kind: "TEST_REPORT", claim: "UI states + accessibility probe" }), inline: {}, integrity: "VERIFIED", content: "{}", content_json: A11Y, content_note: null },
      "/api/evidence/ev-rep": { ...evidence({ id: "ev-rep", claim: "Independent verification of run run-1" }), inline: {}, integrity: "VERIFIED", content: "{}", content_json: REPORT, content_note: null },
    };
  }

  it("shows impact checks per category with paths, selected checks and what executed", async () => {
    respond(routes());
    wrap(<EvidenceDrawer id="ev-rep" onClose={() => {}} />);
    const report = await screen.findByTestId("verification-report");
    const impact = within(report).getByRole("list", { name: "Cek per kategori dampak" });
    const items = within(impact).getAllByRole("listitem");
    expect(within(items[0]).getByText("API")).toBeInTheDocument();
    expect(within(items[0]).getByText("backend/app/api.py")).toBeInTheDocument();
    expect(within(items[0]).getByText(/Dijalankan \(PASS\/FAIL\): adversarial_pack, contract_diff, negative_tests/)).toBeInTheDocument();
    // a category whose selected checks never executed is stated, never satisfied by the trusted suite
    expect(within(items[1]).getByText("Tidak ada cek terpilih yang dijalankan")).toBeInTheDocument();
    expect(within(items[1]).getByText(/Dijalankan \(PASS\/FAIL\): tidak ada/)).toBeInTheDocument();
  });

  it("labels the new verifier checks in Indonesian with text + icon status, never a pass for NOT_RUN/NOT_PROVEN", async () => {
    respond(routes());
    wrap(<EvidenceDrawer id="ev-rep" onClose={() => {}} />);
    const report = await screen.findByTestId("verification-report");
    for (const label of ["Tes negatif (penolakan/izin)", "Paket adversarial verifier", "Diff kontrak (facade/OpenAPI)", "Tes kontrak terpilih",
      "Tes integrasi (tool nyata)", "Suite di lingkungan dependensi kandidat", "Tes frontend (vitest, typecheck, build)", "State UI & aksesibilitas",
      "Credential migrasi terpisah dari runtime"]) {
      expect(within(report).getAllByText(label).length).toBeGreaterThan(0);
    }
    const dbCheck = within(report).getAllByText("Credential migrasi terpisah dari runtime")[0].closest("li")!;
    expect(within(dbCheck).getByText("Belum terbukti")).toBeInTheDocument();
    expect(within(dbCheck).queryByText("Lulus")).not.toBeInTheDocument();
  });

  it("shows tamper.neutralized: automatic actions, the owner's manual remedies and the integrity hold", async () => {
    respond(routes());
    wrap(<EvidenceDrawer id="ev-rep" onClose={() => {}} />);
    const report = await screen.findByTestId("verification-report");
    const tamper = within(report).getByRole("region", { name: "Tamper & netralisasi (E2)" });
    expect(within(tamper).getByRole("alert")).toHaveTextContent(/admisi run worker baru DIJEDA/);
    expect(within(tamper).getByText("Tamper terdeteksi")).toBeInTheDocument();
    expect(within(tamper).getByText("1 temuan tamper")).toBeInTheDocument();
    expect(within(tamper).getByText("decisions:d-9")).toBeInTheDocument();
    expect(within(tamper).getByText("policy file changed: compare it with git")).toBeInTheDocument();
    expect(within(tamper).getByText("Dinetralkan otomatis (1)")).toBeInTheDocument();
    expect(within(tamper).getByText("INVALIDATED")).toBeInTheDocument();
    expect(within(tamper).getByText("Perlu review pemilik (1)")).toBeInTheDocument();
    expect(within(tamper).getByText("review the owner's role and scope")).toBeInTheDocument();
  });

  it("shows result_seal per step: a missing seal fails closed and the key state is stated", async () => {
    respond(routes());
    wrap(<EvidenceDrawer id="ev-rep" onClose={() => {}} />);
    const seals = await screen.findByRole("list", { name: "Segel hasil per langkah" });
    const [suite, cand] = within(seals).getAllByRole("listitem");
    expect(within(suite).getByText(/kunci terpakai dan dihapus/)).toBeInTheDocument();
    expect(within(suite).getByText(/2 baris sesi lain diabaikan/)).toBeInTheDocument();
    expect(within(cand).getByText("Segel tidak ada (gagal tertutup)")).toBeInTheDocument();
    expect(within(cand).getByText("no sealed session end")).toBeInTheDocument();
  });

  it("opens the linked ui-a11y TEST_REPORT inside the drawer and goes back", async () => {
    respond(routes());
    wrap(<EvidenceDrawer id="ev-rep" onClose={() => {}} />);
    await screen.findByTestId("verification-report");
    await userEvent.click(screen.getByRole("button", { name: /Buka laporan ui-a11y/ }));
    const a11y = await screen.findByTestId("ui-a11y-report");
    expect(within(a11y).getByText("image-alt")).toBeInTheDocument();
    expect(within(a11y).getByText(/bukan bukti state tersebut \(NOT-PROVEN\): loading\/desktop/)).toBeInTheDocument();
    expect(within(a11y).getByText("Error skrip — bukan bukti state")).toBeInTheDocument();
    expect(within(a11y).getByText("colour contrast")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /Kembali/ }));
    expect(await screen.findByTestId("verification-report")).toBeInTheDocument();
  });

  it("derives the tamper status from the report (no finding: write check result, an unevaluable check is not a pass)", async () => {
    const { tamperStatus } = await import("./ReportViews");
    expect(tamperStatus({ findings: [], controller: { changed: [] }, trust_tables: { status: "PASS" } }).status).toBe("PASS");
    expect(tamperStatus({ findings: [], controller: { changed: [] }, trust_tables: { status: "UNKNOWN" } }).status).toBe("NOT_PROVEN");
    expect(tamperStatus({ findings: [], trust_tables: { status: "PASS" } }).status).toBe("NOT_PROVEN"); // controller check missing
    expect(tamperStatus({ findings: [{ kind: "result" }] }).status).toBe("TAMPERED");
    respond({
      "/api/evidence/ev-rep": { ...evidence({ id: "ev-rep" }), inline: {}, integrity: "VERIFIED", content: "{}", content_note: null,
        content_json: { ...REPORT, tamper: { findings: [], controller: { changed: [] }, trust_tables: { status: "UNKNOWN" }, neutralized: {} } } },
    });
    wrap(<EvidenceDrawer id="ev-rep" onClose={() => {}} />);
    const tamper = await screen.findByRole("region", { name: "Tamper & netralisasi (E2)" });
    expect(within(tamper).getByText("Pemeriksaan tamper tidak dapat dievaluasi")).toBeInTheDocument();
    expect(within(tamper).queryByText("Lulus")).not.toBeInTheDocument();
  });

  it("does not render the structured view of an untrusted record (a claim is never shown as the verifier's result)", async () => {
    respond({ "/api/evidence/ev-rep": { ...evidence({ id: "ev-rep", trusted: false, origin: "worker" }), inline: {}, integrity: "VERIFIED", content: "{}", content_json: REPORT, content_note: null } });
    wrap(<EvidenceDrawer id="ev-rep" onClose={() => {}} />);
    expect(await screen.findByText(/bukan dari kolektor tepercaya/)).toBeInTheDocument();
    expect(screen.queryByTestId("verification-report")).not.toBeInTheDocument();
  });

  it("does not render the structured view when the stored content no longer matches its hash", async () => {
    respond({ "/api/evidence/ev-rep": { ...evidence({ id: "ev-rep" }), inline: {}, integrity: "MISMATCH", content: "{}", content_json: REPORT, content_note: null } });
    wrap(<EvidenceDrawer id="ev-rep" onClose={() => {}} />);
    expect(await screen.findByText(/Tampilan terstruktur tidak ditampilkan/)).toBeInTheDocument();
    expect(screen.queryByTestId("verification-report")).not.toBeInTheDocument();
  });
});

describe("UI-state screenshots (Evidence → Tangkapan layar UI)", () => {
  const shots = [
    evidence({ id: "s1", kind: "SCREENSHOT", content_type: "image/png", claim: "UI state 'empty'", environment: { ui_state: "empty", viewport: "mobile", answered_by: "candidate-app", render_error: false } }),
    evidence({ id: "s2", kind: "SCREENSHOT", content_type: "image/png", claim: "UI state 'loading'", environment: { ui_state: "loading", viewport: "desktop", answered_by: "verifier", render_error: true } }),
    evidence({ id: "s3", kind: "SCREENSHOT", content_type: "image/png", run_id: null, subject_type: "commit", collector_identity: "docs-screenshot:staging", evidence_level: "E3",
      environment: { name: "staging", route: "/", commit: "b".repeat(40), viewport: "1280x800" } }),
  ];
  const list = { items: shots, source: "s", checked_at: now(), mode: "LIVE", level_explanations: {}, trusted_collectors: [] };
  const visuals = { screenshot_collector: { available: true, authenticated_capture: { status: "UNDECIDED", note: "Collector tidak pernah login." } } };

  it("states the authenticated-capture decision as UNDECIDED and marks render errors as not evidence of the state", async () => {
    respond({ "/api/docs/visuals/status": visuals, "/api/evidence": list });
    wrap(<ScreenshotsPanel />);
    const notice = await screen.findByTestId("authenticated-capture");
    expect(within(notice).getByText("Belum diputuskan pemilik")).toBeInTheDocument();
    expect(within(notice).getByText("Collector tidak pernah login.")).toBeInTheDocument();
    const row = (await screen.findByText("loading")).closest("tr")!;
    expect(within(row).getByText("Error skrip — bukan bukti state")).toBeInTheDocument();
    expect(within(row).getByText("verifier")).toBeInTheDocument();
    expect(screen.getByText(/1 render memunculkan error skrip/)).toBeInTheDocument();
    // the screenshot collector's target page is listed separately with its route and commit
    const page = screen.getByText("Halaman target (kolektor screenshot)").closest("section")!;
    expect(within(page).getByText("staging")).toBeInTheDocument();
    expect(within(page).getByText("bbbbbbbbbb")).toBeInTheDocument();
  });

  it("links an UNDECIDED capture to the owner preset, and states an ALLOWED decision without claiming a login", async () => {
    respond({ "/api/docs/visuals/status": visuals, "/api/evidence": list });
    const first = wrap(<ScreenshotsPanel />);
    const open = await screen.findByTestId("authenticated-capture");
    expect(within(open).getByRole("link", { name: /Catat keputusan pemilik/ })).toHaveAttribute("href", "/decisions?preset=screenshot_authenticated_capture");
    first.unmount();
    const allowed = {
      screenshot_collector: {
        available: true,
        authenticated_capture: { status: "ALLOWED", decision_id: "dec-7", scope: "rute Control Center", capture_mode: "UNAUTHENTICATED", preset: "screenshot_authenticated_capture", note: "Belum dibangun." },
      },
    };
    respond({ "/api/docs/visuals/status": allowed, "/api/evidence": list });
    wrap(<ScreenshotsPanel />);
    const notice = await screen.findByTestId("authenticated-capture");
    expect(within(notice).getByText(/ALLOWED oleh pemilik \(jalur capture terautentikasi belum dibangun\)/)).toBeInTheDocument();
    expect(within(notice).getByText(/mode kolektor: tanpa login/)).toBeInTheDocument();
    expect(within(notice).getByRole("link", { name: "dec-7" })).toHaveAttribute("href", "/decisions?decision=dec-7");
    expect(within(notice).getByText(/scope: rute Control Center/)).toBeInTheDocument();
    expect(within(notice).getByRole("link", { name: /Ubah keputusan pemilik/ })).toBeInTheDocument();
  });

  it("a state opens from the keyboard; an untrusted SCREENSHOT is a claim, never 'no script error'", async () => {
    const claim = evidence({ id: "s9", kind: "SCREENSHOT", trusted: false, origin: "worker", evidence_level: "E0", claim: "worker says it rendered",
      environment: { ui_state: "error", viewport: "mobile", render_error: false } });
    respond({
      "/api/docs/visuals/status": visuals,
      "/api/evidence/s9": { ...claim, inline: {}, integrity: "VERIFIED", content: null, content_note: null },
      "/api/evidence": { ...list, items: [claim] },
    });
    wrap(<ScreenshotsPanel />);
    const open = await screen.findByRole("button", { name: "Buka detail tangkapan layar error" });
    const row = open.closest("tr")!;
    expect(within(row).getByText("Klaim — tidak dihitung")).toBeInTheDocument();
    expect(within(row).queryByText("Tanpa error skrip")).not.toBeInTheDocument();
    open.focus();
    await userEvent.keyboard("{Enter}");
    const dialog = await screen.findByRole("dialog", { name: "Detail bukti" });
    const facts = await within(dialog).findByTestId("screenshot-facts");
    expect(within(facts).getByText(/Klaim tidak tepercaya/)).toBeInTheDocument();
    expect(within(facts).queryByText(/Render tanpa error skrip/)).not.toBeInTheDocument();
  });

  it("opens a render-error screenshot with a clear 'bukan bukti state' statement", async () => {
    respond({
      "/api/docs/visuals/status": visuals,
      "/api/evidence/s2": { ...shots[1], inline: {}, integrity: "VERIFIED", content: null, content_note: "Isi biner tidak ditampilkan" },
      "/api/evidence": list,
    });
    wrap(<ScreenshotsPanel />);
    await userEvent.click(await screen.findByText("loading"));
    const dialog = await screen.findByRole("dialog", { name: "Detail bukti" });
    const facts = await within(dialog).findByTestId("screenshot-facts");
    expect(within(facts).getByRole("alert")).toHaveTextContent(/Bukan bukti state/);
  });

  it("finds the run's ui-a11y report through the verification report's exact link", async () => {
    respond({
      "/api/docs/visuals/status": visuals,
      "/api/evidence/ev-a11y": { ...evidence({ id: "ev-a11y", kind: "TEST_REPORT" }), inline: {}, integrity: "VERIFIED", content: "{}", content_json: A11Y, content_note: null },
      "/api/evidence/ev-rep": { ...evidence({ id: "ev-rep" }), inline: {}, integrity: "VERIFIED", content: "{}", content_json: REPORT, content_note: null },
      "/api/evidence?kind=VERIFICATION_REPORT": { ...list, items: [evidence({ id: "ev-rep" })] },
      "/api/evidence": list,
    });
    wrap(<ScreenshotsPanel />);
    await userEvent.click(await screen.findByRole("button", { name: "Laporan ui-a11y" }));
    expect(await screen.findByTestId("ui-a11y-report")).toBeInTheDocument();
    // only the verifier's own (trusted) report names the ui-a11y report
    expect(calls.some((c) => c.url.includes("kind=VERIFICATION_REPORT") && c.url.includes("run_id=run-1") && c.url.includes("trusted=true"))).toBe(true);
  });

  it("the Evidence page opens a deep-linked record (?id=) on the ledger tab", async () => {
    respond({
      "/api/evidence/s1": { ...shots[0], inline: {}, integrity: "VERIFIED", content: null, content_note: null },
      "/api/evidence": list,
    });
    const { default: EvidencePage } = await import("./index");
    wrap(<EvidencePage />, "/evidence?id=s1");
    const dialog = await screen.findByRole("dialog", { name: "Detail bukti" });
    expect(await within(dialog).findByTestId("screenshot-facts")).toBeInTheDocument();
  });
});

describe("provider enforcement test and delivery actions (PR & provider)", () => {
  const caps = (over: Partial<ProviderCaps> = {}): ProviderCaps => ({
    provider: "GITHUB", repo: "octo/app", branch: "main", checked_at: now(), credential: "personal/github", visibility: "public", plan: "free",
    checks: [], required_status_checks: [], merge_gate: "CONFIGURED", enforcement_tested: "STALE",
    enforcement_test: { evidence_id: "ev-enf", rulesets: [{ id: 42, updated_at: "2026-10-20T10:00:00Z" }], observed_on_branch: "aeos-probe/x",
      observed_at: "2026-10-20T11:00:00Z", reason: "ruleset changed or is no longer active since the refusal was observed" },
    residual_risks: [], source: "GitHub REST API (GET only)", mode: "LIVE", project_id: "p1", ...over,
  });

  it("shows the bound enforcement test: status, ruleset, branch and the reason it is not VERIFIED", async () => {
    respond({ "/api/delivery/provider-capabilities": caps(), "/api/delivery/prs": { items: [], source: "s", checked_at: null, mode: "LIVE" } });
    wrap(<DeliveryPanel />);
    const facts = await screen.findByTestId("enforcement-test");
    expect(within(facts).getAllByText("Usang — perlu dicek ulang").length).toBeGreaterThan(0);
    expect(within(facts).getByText(/#42/)).toBeInTheDocument();
    expect(within(facts).getByText("aeos-probe/x")).toBeInTheDocument();
    expect(within(facts).getByText(/ruleset changed or is no longer active/)).toBeInTheDocument();
  });

  it("an owner attestation reads CLAIMED, never VERIFIED", async () => {
    respond({
      "/api/delivery/provider-capabilities": caps({ enforcement_tested: "CLAIMED", enforcement_test: { evidence_id: "ev-c", label: "owner attestation (HUMAN_RECORDED claim)" } }),
      "/api/delivery/prs": { items: [], source: "s", checked_at: null, mode: "LIVE" },
    });
    wrap(<DeliveryPanel />);
    const facts = await screen.findByTestId("enforcement-test");
    expect(within(facts).getByText("Diklaim pemilik (bukan observasi provider)")).toBeInTheDocument();
    expect(screen.queryByText("Terverifikasi")).not.toBeInTheDocument();
  });

  it("observe / attest (after confirmation) / record digests post exactly the API fields", async () => {
    respond({
      "/api/delivery/enforcement/observe": { repo: "octo/app", branch: "main", status: "NOT_OBSERVED", checked_at: now(), time_period: "day", refusals: [], rulesets: [], error: null, source: "s", evidence_id: null },
      "/api/delivery/enforcement/attest": { evidence_id: "ev-att", trusted: false, evidence_level: "E0", label: "HUMAN_RECORDED claim (not a provider observation)", enforcement_tested: "CLAIMED", source: "owner attestation", checked_at: now() },
      "/api/delivery/artifacts/record": { repo: "octo/app", sha: "c".repeat(40), status: "RECORDED", runs: [], artifacts: [], checked_at: now(), error: null, truncated: false, source: "s", note: "n", eligible_digests: ["sha256:" + "d".repeat(64)], evidence_id: "ev-art" },
      "/api/delivery/provider-capabilities": caps(),
      "/api/delivery/prs": { items: [], source: "s", checked_at: null, mode: "LIVE" },
    });
    wrap(<DeliveryPanel />);
    await screen.findByTestId("enforcement-test");
    await userEvent.selectOptions(screen.getByLabelText("Periode"), "day");
    await userEvent.click(screen.getByRole("button", { name: /Observasi penegakan/ }));
    await waitFor(() => expect(posts("/api/delivery/enforcement/observe")).toHaveLength(1));
    expect(posts("/api/delivery/enforcement/observe")[0].body).toEqual({ repo: "octo/app", branch: "main", time_period: "day" });
    expect(await screen.findByText("Tidak ada penolakan teramati")).toBeInTheDocument();

    const review = screen.getByRole("button", { name: /Tinjau atestasi/ });
    expect(review).toBeDisabled();
    await userEvent.type(screen.getByLabelText(/Pernyataan/), "push ke main ditolak GH013");
    await userEvent.click(review);
    expect(posts("/api/delivery/enforcement/attest")).toHaveLength(0); // nothing is recorded before the confirmation
    expect(within(screen.getByRole("group", { name: "Konfirmasi atestasi" })).getByText("push ke main ditolak GH013")).toBeInTheDocument();
    // an edit after the review closes the confirmation: what is confirmed is what was reviewed
    await userEvent.type(screen.getByLabelText(/Pernyataan/), "!");
    expect(screen.queryByRole("group", { name: "Konfirmasi atestasi" })).not.toBeInTheDocument();
    await userEvent.type(screen.getByLabelText(/Pernyataan/), "{Backspace}");
    await userEvent.click(screen.getByRole("button", { name: /Tinjau atestasi/ }));
    await userEvent.click(within(screen.getByRole("group", { name: "Konfirmasi atestasi" })).getByRole("button", { name: /Konfirmasi/ }));
    await waitFor(() => expect(posts("/api/delivery/enforcement/attest")).toHaveLength(1));
    expect(posts("/api/delivery/enforcement/attest")[0].body).toEqual({ repo: "octo/app", branch: "main", statement: "push ke main ditolak GH013" });

    const sha = screen.getByLabelText(/Commit penuh/);
    await userEvent.type(sha, "abc");
    expect(screen.getByRole("button", { name: /Catat digest/ })).toBeDisabled();
    await userEvent.clear(sha);
    await userEvent.type(sha, "c".repeat(40));
    await userEvent.click(screen.getByRole("button", { name: /Catat digest/ }));
    await waitFor(() => expect(posts("/api/delivery/artifacts/record")).toHaveLength(1));
    expect(posts("/api/delivery/artifacts/record")[0].body).toEqual({ project_id: "p1", sha: "c".repeat(40), repo: "octo/app" });
    expect(await screen.findByText("sha256:" + "d".repeat(64))).toBeInTheDocument();
  });

  it("hides every delivery action for VIEWER and for Bitbucket", async () => {
    auth.isAdmin = false;
    respond({ "/api/delivery/provider-capabilities": caps(), "/api/delivery/prs": { items: [], source: "s", checked_at: null, mode: "LIVE" } });
    const { unmount } = wrap(<DeliveryPanel />);
    await screen.findByTestId("enforcement-test");
    expect(screen.queryByRole("button", { name: /Observasi penegakan|Tinjau atestasi|Catat digest/ })).not.toBeInTheDocument();
    unmount();
    auth.isAdmin = true;
    respond({ "/api/delivery/provider-capabilities": caps({ provider: "BITBUCKET" }), "/api/delivery/prs": { items: [], source: "s", checked_at: null, mode: "LIVE" } });
    wrap(<DeliveryPanel />);
    await screen.findByTestId("enforcement-test");
    expect(screen.getByText(/hanya tersedia untuk GitHub/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Observasi penegakan/ })).not.toBeInTheDocument();
  });
});

describe("gates promotion block (§31 build once / digest)", () => {
  const sep = {
    worker_reported: { status: "DONE", trust: "UNTRUSTED_CLAIM" }, verifier: { status: "PASS" }, reviewer: { verdict: "APPROVE", status: "COMPLETED" },
    accepted: { status: "ACCEPTED", valid: true }, merged: { status: "MERGED", merged_by: "owner", merged_by_type: "User" },
  };
  const gates = (promotion: GatesResponse["promotion"]): GatesResponse =>
    ({ task_id: "task-1", task_key: "t1", title: "Fitur", project_id: "p1", domain: "PERSONAL", run_id: "run-1", head_commit: "a".repeat(40), gates: [],
      separate_status: sep, acceptance: null, pull_request: null, ready_for_human_merge: false, merge_authority: "HUMAN_ONLY", checked_at: now(), mode: "LIVE",
      promotion }) as unknown as GatesResponse;

  it("states the promotion status, per-target digests and that promotion is a human action", async () => {
    respond({
      "/api/delivery/gates": gates({
        status: "FAIL", authority: "HUMAN_ONLY", reasons: ["Target menjalankan artefak yang bukan hasil build CI merge commit (digest berbeda)"],
        targets: { staging: { integration_id: "i-s", observation_id: "ob-1", reported_digest: "sha256:" + "e".repeat(64), status: "MISMATCH" },
          production: { integration_id: "i-p", observation_id: null, reported_digest: null, status: "NO_OBSERVATION" } },
        merge_commit: "f".repeat(40), recorded_digests: ["sha256:" + "1".repeat(64)], digest_evidence_id: "ev-dg",
      }),
    });
    wrap(<GatesPanel taskId="task-1" />);
    const block = await screen.findByTestId("promotion");
    expect(within(block).getByText("Gagal")).toBeInTheDocument();
    expect(within(block).getByText(/Promosi: tindakan manusia/)).toBeInTheDocument();
    expect(within(block).getByText("Digest berbeda")).toBeInTheDocument();
    expect(within(block).getByText("Belum ada observasi")).toBeInTheDocument();
    expect(within(block).getByText(/digest berbeda\)/)).toBeInTheDocument();
    // the merge commit is offered for the digest record (admin, PERSONAL)
    expect(within(block).getByLabelText(/Commit penuh/)).toHaveValue("f".repeat(40));
  });

  it("VIEWER sees the block without the record action; PENDING before merge is not a pass", async () => {
    auth.isAdmin = false;
    respond({
      "/api/delivery/gates": gates({ status: "PENDING", authority: "HUMAN_ONLY", reasons: ["Belum di-merge"], targets: {}, merge_commit: null, recorded_digests: [], digest_evidence_id: null }),
    });
    wrap(<GatesPanel taskId="task-1" />);
    const block = await screen.findByTestId("promotion");
    expect(within(block).getByText("Tertunda")).toBeInTheDocument();
    expect(within(block).queryByText("Lulus")).not.toBeInTheDocument();
    expect(within(block).queryByRole("button", { name: /Catat digest/ })).not.toBeInTheDocument();
  });
});
