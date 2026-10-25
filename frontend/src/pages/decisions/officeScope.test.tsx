import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { MemoryRouter } from "react-router-dom";
import { vi } from "vitest";
import { providerLabel } from "../evidence/DeliveryPanel";
import { DecisionForm, parseIdentities, parseTargets } from "./DecisionForm";
import { OfficePolicyCard } from "./PolicyPanels";

const auth = { domain: "OFFICE", isAdmin: true, projectId: null, session: null, selectContext: vi.fn() };
vi.mock("../../auth/AuthContext", () => ({ useAuth: () => auth }));

/** W6-157/W6-165/W6-151: the company authorisation carries its approved targets, data classes and identities;
 * W6-185: the controlled OFFICE route path has a Control Center control. */

function wrap(ui: ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>{ui}</MemoryRouter>
    </QueryClientProvider>,
  );
}

function stub(get: Record<string, unknown> = {}) {
  const fn = vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
    const body = init?.method === "POST" ? { id: "d1" } : (get[String(url)] ?? {});
    return new Response(JSON.stringify(body), { status: init?.method === "POST" ? 201 : 200, headers: { "Content-Type": "application/json" } });
  });
  vi.stubGlobal("fetch", fn);
  return fn;
}

function posts(fn: ReturnType<typeof stub>) {
  return fn.mock.calls.filter(([, i]) => i?.method === "POST").map(([u, i]) => ({ url: String(u), body: JSON.parse(String(i!.body)) as Record<string, unknown> }));
}

afterEach(() => {
  vi.unstubAllGlobals();
  auth.domain = "OFFICE";
  auth.isAdmin = true;
});

describe("company authorisation scope (DecisionForm, OFFICE)", () => {
  it("sends approved targets, data classes and identities with the authorisation", async () => {
    const fn = stub();
    wrap(<DecisionForm domain="OFFICE" initialKind="POLICY" onDone={() => undefined} />);
    await userEvent.click(screen.getByLabelText(/Ini otorisasi perusahaan untuk OFFICE/));
    fireEvent.change(screen.getByLabelText(/Referensi persetujuan perusahaan/), { target: { value: "CHG-9 signed" } });
    await userEvent.click(screen.getByLabelText("bitbucket"));
    fireEvent.change(screen.getByLabelText("Target yang disetujui perusahaan"), {
      target: { value: "bitbucket:workspace=acme,root_id=acme/app\n\n  confluence:tenant=acme.atlassian.net,space_id=98765 " },
    });
    await userEvent.click(screen.getByLabelText("INTERNAL"));
    fireEvent.change(screen.getByLabelText("Identitas credential yang disetujui"), { target: { value: "office/bitbucket={bb-uuid}" } });
    fireEvent.change(screen.getByLabelText(/Scope eksplisit/), { target: { value: "providers=bitbucket" } });
    fireEvent.change(screen.getByLabelText(/Rationale/), { target: { value: "otorisasi perusahaan tertulis" } });
    await userEvent.click(screen.getByRole("button", { name: "Catat keputusan" }));
    await waitFor(() => expect(posts(fn)).toHaveLength(1));
    const { url, body } = posts(fn)[0];
    expect(url).toBe("/api/decisions");
    expect(body).toMatchObject({
      kind: "POLICY",
      office_authorization: true,
      company_reference: "CHG-9 signed",
      allowed_providers: ["bitbucket"],
      approved_targets: ["bitbucket:workspace=acme,root_id=acme/app", "confluence:tenant=acme.atlassian.net,space_id=98765"],
      approved_data_classes: ["INTERNAL"],
      approved_identities: { "office/bitbucket": "{bb-uuid}" },
    });
  });

  it("an empty scope is sent as empty (UNDECIDED), never invented", async () => {
    const fn = stub();
    wrap(<DecisionForm domain="OFFICE" initialKind="POLICY" onDone={() => undefined} />);
    await userEvent.click(screen.getByLabelText(/Ini otorisasi perusahaan untuk OFFICE/));
    expect(screen.getByText(/belum diputuskan: semua target OFFICE ditolak/)).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText(/Referensi persetujuan perusahaan/), { target: { value: "CHG-9 signed" } });
    await userEvent.click(screen.getByLabelText("jira"));
    fireEvent.change(screen.getByLabelText(/Scope eksplisit/), { target: { value: "providers=jira" } });
    fireEvent.change(screen.getByLabelText(/Rationale/), { target: { value: "otorisasi perusahaan tertulis" } });
    await userEvent.click(screen.getByRole("button", { name: "Catat keputusan" }));
    await waitFor(() => expect(posts(fn)).toHaveLength(1));
    expect(posts(fn)[0].body).toMatchObject({ approved_targets: [], approved_data_classes: [], approved_identities: {} });
  });

  it("parses one entry per line and refuses an identity without '='", () => {
    expect(parseTargets(" a:x=1 \n\nb:y=2")).toEqual(["a:x=1", "b:y=2"]);
    expect(parseIdentities("office/atlassian = me@acme.example\n")).toEqual({ "office/atlassian": "me@acme.example" });
    expect(() => parseIdentities("office/atlassian")).toThrow(/office\/<provider>=/);
  });
});

describe("OFFICE policy card", () => {
  const policy = {
    external_worker_allowed: false,
    approved_worker_routes: ["worker.office"],
    claude_max_allowed_for_office_data: false,
    atlassian_deployment: "CLOUD",
    office_authorization: true,
    authorized_providers: ["bitbucket"],
    source: "policy/office-defaults.yaml",
  };

  it("states an undecided scope as UNKNOWN, never as allowed", async () => {
    stub({ "/api/policy/office": { ...policy, approved_targets: [], approved_data_classes: [], approved_identities: {} } });
    wrap(<OfficePolicyCard />);
    expect(await screen.findByText("Belum diputuskan (semua ditolak)")).toBeInTheDocument();
    expect(screen.getByText(/data OFFICE tidak dikirim ke model/)).toBeInTheDocument();
  });

  it("lists the approved targets and data classes", async () => {
    stub({
      "/api/policy/office": {
        ...policy,
        approved_targets: [{ provider: "bitbucket", workspace: "acme" }],
        approved_data_classes: ["INTERNAL"],
        approved_identities: { "office/bitbucket": "{bb-uuid}" },
      },
    });
    wrap(<OfficePolicyCard />);
    expect(await screen.findByText("bitbucket(workspace=acme)")).toBeInTheDocument();
    expect(screen.getByText("INTERNAL")).toBeInTheDocument();
    expect(screen.getByText("office/bitbucket={bb-uuid}")).toBeInTheDocument();
  });

  it("posts the OFFICE route eligibility with an office/ credential reference (W6-185)", async () => {
    const fn = stub({ "/api/policy/office": { ...policy, approved_targets: [], approved_data_classes: [], approved_identities: {} } });
    wrap(<OfficePolicyCard />);
    const form = await screen.findByRole("form", { name: "Kelayakan route OFFICE" });
    const save = within(form).getByRole("button", { name: "Catat kelayakan OFFICE" });
    fireEvent.change(within(form).getByLabelText("Referensi credential OFFICE"), { target: { value: "personal/openrouter" } });
    fireEvent.change(within(form).getByLabelText("Rationale route OFFICE"), { target: { value: "perusahaan menyetujui rute ini" } });
    expect(save).toBeDisabled();
    fireEvent.change(within(form).getByLabelText("Referensi credential OFFICE"), { target: { value: "office/openrouter" } });
    await userEvent.click(save);
    await waitFor(() => expect(posts(fn)).toHaveLength(1));
    expect(posts(fn)[0]).toEqual({
      url: "/api/policy/office/routes",
      body: { route_key: "worker.office", credential_ref: "office/openrouter", rationale: "perusahaan menyetujui rute ini" },
    });
  });

  it("the route control is admin-only", async () => {
    auth.isAdmin = false;
    stub({ "/api/policy/office": { ...policy, approved_targets: [], approved_data_classes: [], approved_identities: {} } });
    wrap(<OfficePolicyCard />);
    await screen.findByText("Belum diputuskan (semua ditolak)");
    expect(screen.queryByRole("form", { name: "Kelayakan route OFFICE" })).not.toBeInTheDocument();
  });
});

describe("delivery capability card host (W6-196)", () => {
  it("names Bitbucket for OFFICE and GitHub for PERSONAL", () => {
    expect(providerLabel("bitbucket", "OFFICE")).toBe("Bitbucket");
    expect(providerLabel(undefined, "OFFICE")).toBe("Bitbucket");
    expect(providerLabel("GITHUB", "PERSONAL")).toBe("GitHub");
    expect(providerLabel(undefined, "PERSONAL")).toBe("GitHub");
  });
});
