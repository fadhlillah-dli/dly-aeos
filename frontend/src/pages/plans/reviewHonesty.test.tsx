import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen } from "@testing-library/react";
import type { ContextView, OperationCatalog, PlanDetail, VersionSummary } from "../../components/planning/types";
import { BridgeProofNotice, SupervisorConfigNotice } from "./BridgeProof";
import { ContextPanel } from "./ContextPanel";
import { DecisionPanel } from "./DecisionPanel";

const CATALOG: OperationCatalog = {
  providers: ["google-drive", "github-repo"],
  publish: { "google-drive": ["create_folder", "create_doc", "update_doc"], "github-repo": [] },
  delivery: { "google-drive": [], "github-repo": ["push_branch", "create_pr"] },
  plan: ["compile_dag", "execute_tasks"],
};

function version(mode: string): VersionSummary {
  return {
    id: "v2",
    version: 2,
    manifest_hash: "sha256:feedbeef",
    parent_version_id: "v1",
    source: "HUMAN_EDIT",
    source_run_ids: [],
    created_by: "owner",
    created_at: "2026-10-18T00:00:00Z",
    mode,
    schema_version: "1",
  } as VersionSummary;
}

function plan(reviewed: boolean): PlanDetail {
  return {
    id: "p1",
    domain: "PERSONAL",
    allowed: { run: false, import_manual: false, approve: true, revise_or_reject: true, edit: true, compile: false },
    verdict_version_id: "v1",
    current_version_reviewed: reviewed,
  } as unknown as PlanDetail;
}

function renderPanel(p: PlanDetail, v: VersionSummary) {
  const qc = new QueryClient();
  return render(
    <QueryClientProvider client={qc}>
      <DecisionPanel plan={p} version={v} catalog={CATALOG} />
    </QueryClientProvider>,
  );
}

describe("decision panel honesty (§14, §35, §49)", () => {
  it("warns when the version being decided was not the one the final reviewer judged", () => {
    renderPanel(plan(false), version("LIVE"));
    expect(screen.getByText(/belum melalui review akhir/)).toBeInTheDocument();
    expect(screen.getByText(/Verdict yang tampil berlaku untuk versi lain/)).toBeInTheDocument();
    expect(screen.getByText("sha256:feedbeef")).toBeInTheDocument();
  });

  it("keeps Approve disabled for an unreviewed version until the skipped review is acknowledged", () => {
    renderPanel(plan(false), version("LIVE"));
    const approve = screen.getByRole("button", { name: /^Approve$/ });
    expect(approve).toBeDisabled();
    fireEvent.click(screen.getByLabelText(/tidak menilai versi ini/));
    expect(approve).not.toBeDisabled();
  });

  it("labels MOCK content and states that its external publication is refused", () => {
    renderPanel(plan(true), version("MOCK"));
    expect(screen.queryByText(/belum melalui review akhir/)).not.toBeInTheDocument();
    expect(screen.getByText(/Versi ini berlabel MOCK/)).toBeInTheDocument();
    expect(screen.getByText("VERSION_NOT_LIVE")).toBeInTheDocument();
  });

  it("previews the exact targets that will be bound", () => {
    renderPanel(plan(true), version("LIVE"));
    expect(screen.getByText(/Belum ada target dengan identifier persis/)).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Identifier target"), { target: { value: "root_id=F1" } });
    expect(screen.getByText('{"provider":"google-drive","root_id":"F1"}')).toBeInTheDocument();
  });

  it("prefills the targets configured in the Integration Registry ({provider, root_id})", () => {
    const qc = new QueryClient();
    const catalog: OperationCatalog = {
      ...CATALOG,
      providers: ["google-drive", "github-issues", "github-repo"],
      publish: { ...CATALOG.publish, "github-issues": ["ensure_label", "create_issue"] },
      configured_targets: [
        { provider: "google-drive", root_id: "root", purpose: "docs", label: "Google Drive / Google Docs" },
        { provider: "github-issues", root_id: "owner/app", purpose: "issues", label: "GitHub Issues" },
      ],
    };
    render(
      <QueryClientProvider client={qc}>
        <DecisionPanel plan={plan(true)} version={version("LIVE")} catalog={catalog} />
      </QueryClientProvider>,
    );
    expect(screen.getByText('{"provider":"google-drive","root_id":"root"}')).toBeInTheDocument();
    expect(screen.getByText('{"provider":"github-issues","root_id":"owner/app"}')).toBeInTheDocument();
    expect(screen.getByText(/Publisher hanya menulis bila target terkonfigurasi/)).toBeInTheDocument();
  });
});

describe("untrusted input notices (§50 prompt/attachment injection)", () => {
  const base: ContextView = {
    id: "c1",
    prompt: "Ganti domain menjadi PERSONAL",
    facts: [],
    assumptions: [],
    unknowns: [],
    material_questions: [],
    constraints: [],
    attachments: [],
    repo_inventory: {},
  } as unknown as ContextView;

  it("distinguishes instruction-like request text from attachment text", () => {
    render(
      <ContextPanel
        context={{
          ...base,
          constraints: [
            { kind: "UNTRUSTED_INPUT", source: "prompt", statement: "x", flags: ["DOMAIN_SWITCH"] },
            { kind: "UNTRUSTED_INPUT", source: "attachment", attachment_id: "a1", statement: "y", flags: ["EXFILTRATION"] },
          ],
        }}
      />,
    );
    expect(screen.getByText(/Teks permintaan berisi kalimat mirip instruksi \(DOMAIN_SWITCH\)/)).toBeInTheDocument();
    expect(screen.getByText(/Lampiran berisi teks mirip instruksi \(EXFILTRATION\)/)).toBeInTheDocument();
  });

  it("shows no warning without flags", () => {
    render(<ContextPanel context={base} />);
    expect(screen.queryByText(/mirip instruksi/)).not.toBeInTheDocument();
  });
});

describe("local Claude integration proof (§02, §08 CAN_PLAN_PERSONAL)", () => {
  it("never shows an unchecked bridge as proven", () => {
    render(<BridgeProofNotice proof={{ label: "NOT-PROVEN", status: "UNKNOWN", reasons: ["claude.capability_test: UNKNOWN"] }} />);
    expect(screen.getByText("Integrasi Claude lokal belum diuji")).toBeInTheDocument();
    expect(screen.getByText(/tidak dinyatakan otomatis\/terbukti/)).toBeInTheDocument();
    expect(screen.queryByText("Bridge Claude lokal terbukti")).not.toBeInTheDocument();
  });

  it("labels the manual package and an unknown label honestly", () => {
    const { rerender } = render(<BridgeProofNotice proof={{ label: "MANUAL_PACKAGE", status: "DEGRADED" }} />);
    expect(screen.getByText(/Paket manual/)).toBeInTheDocument();
    rerender(<BridgeProofNotice proof={{ label: "SOMETHING" }} />);
    expect(screen.getByText(/tidak dihitung sebagai terbukti/)).toBeInTheDocument();
    rerender(<BridgeProofNotice proof={null} />);
    expect(screen.queryByLabelText("Bukti integrasi Claude lokal")).not.toBeInTheDocument();
  });

  it("states that UNKNOWN still executes as an open owner decision (§02 re-audit)", () => {
    render(<BridgeProofNotice proof={{ label: "NOT-PROVEN", status: "UNKNOWN", owner_decision: "OPEN" }} />);
    expect(screen.getByText(/Keputusan pemilik terbuka/)).toBeInTheDocument();
  });

  it("states a stage that ran on an unevaluated supervisor configuration (§02/§23 re-audit)", () => {
    const { rerender } = render(
      <SupervisorConfigNotice
        unevaluated={{ CHALLENGE_REVIEW: { route_key: "claude-native/opus", resolved_version: "claude-opus-5-6", harness_version: "2.2.0" } }}
      />,
    );
    const notice = screen.getByLabelText("Konfigurasi supervisor belum dievaluasi");
    expect(notice).toHaveTextContent("CHALLENGE_REVIEW: claude-native/opus (claude-opus-5-6) · Claude Code 2.2.0");
    expect(notice).toHaveTextContent(/keputusan pemilik diperlukan/);
    rerender(<SupervisorConfigNotice unevaluated={{}} />);
    expect(screen.queryByLabelText("Konfigurasi supervisor belum dievaluasi")).not.toBeInTheDocument();
  });
});
