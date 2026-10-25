import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { MemoryRouter } from "react-router-dom";
import { vi } from "vitest";
import type { IntegrationView, ProjectView } from "./types";

const auth = { domain: "OFFICE", isAdmin: true, projectId: null, session: null, selectContext: vi.fn() };
vi.mock("../../auth/AuthContext", () => ({ useAuth: () => auth }));

/**
 * W6-003/W6-011/W6-012/W6-103/W6-124/W6-133/W6-196: the owner can switch the OFFICE Bitbucket code adapter on and set
 * the Pipelines gate map from the Control Center; W6-099: PERSONAL staging/production deployment targets;
 * every re-save keeps the stored config (the generic form used to send none and wipe it).
 */

const OFFICE_ALLOWED = { bitbucket: ["code"], "bitbucket-pipelines": ["ci"], confluence: ["docs"], jira: ["issues", "roadmap"] };
const PERSONAL_ALLOWED = {
  "deployment-target": ["production", "staging"],
  "github-actions": ["ci"],
  "github-issues": ["issues"],
  "google-drive": ["docs"],
};

function wrap(ui: ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>{ui}</MemoryRouter>
    </QueryClientProvider>,
  );
}

function integration(over: Partial<IntegrationView>): IntegrationView {
  return {
    id: `i-${over.provider}-${over.purpose}`,
    project_id: "p1",
    domain: "OFFICE",
    provider: "bitbucket",
    purpose: "code",
    tenant_id: null,
    root_id: null,
    root_id_name: null,
    credential_ref: null,
    config: {},
    capability_status: { configuration: "CONFIGURED", credential: { status: "MISSING" } },
    last_verified_at: null,
    updated_at: "2026-10-25T08:00:00Z",
    target_url: null,
    ...over,
  } as IntegrationView;
}

function mockApi(items: IntegrationView[], allowed: Record<string, string[]>) {
  const fn = vi.fn(async (_url: RequestInfo | URL, init?: RequestInit) => {
    const body = init?.method === "PUT" ? { integration: JSON.parse(String(init.body)) } : { items, allowed_providers: allowed, checked_at: new Date().toISOString(), mode: "LIVE" };
    return new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
  });
  vi.stubGlobal("fetch", fn);
  return fn;
}

function putBodies(fn: ReturnType<typeof mockApi>): Record<string, unknown>[] {
  return fn.mock.calls.filter(([, i]) => i?.method === "PUT").map(([, i]) => JSON.parse(String(i!.body)) as Record<string, unknown>);
}

async function panel(domain: "OFFICE" | "PERSONAL", items: IntegrationView[] = []) {
  auth.domain = domain;
  const fn = mockApi(items, domain === "OFFICE" ? OFFICE_ALLOWED : PERSONAL_ALLOWED);
  const { IntegrationsPanel } = await import("./IntegrationsPanel");
  const project = { id: "p1", domain, repositories: [] } as unknown as ProjectView;
  wrap(<IntegrationsPanel project={project} isAdmin />);
  return fn;
}

afterEach(() => {
  vi.unstubAllGlobals();
  auth.domain = "OFFICE";
});

describe("OFFICE Kode — Bitbucket", () => {
  it("enables the adapter with config.enabled and sends no deployment field", async () => {
    const fn = await panel("OFFICE");
    const form = await screen.findByRole("form", { name: /Kode — Bitbucket/ });
    const save = within(form).getByRole("button", { name: /Simpan Bitbucket/ });
    expect(save).toBeDisabled(); // the exact repository is required
    expect(within(form).queryByLabelText(/Deployment/)).not.toBeInTheDocument();
    expect(within(form).getByText(/atlassian_deployment/)).toBeInTheDocument(); // points to the office_policy override
    fireEvent.change(within(form).getByLabelText(/Repository Bitbucket/), { target: { value: "acme/app" } });
    await userEvent.click(within(form).getByLabelText(/Aktifkan adapter/));
    await userEvent.click(save);
    await waitFor(() => expect(putBodies(fn)).toHaveLength(1));
    expect(putBodies(fn)[0]).toEqual({
      provider: "bitbucket",
      purpose: "code",
      root_id: "acme/app",
      credential_ref: "office/bitbucket",
      config: { enabled: true },
    });
    expect(JSON.stringify(putBodies(fn)[0])).not.toMatch(/deployment|token|password/i);
  });

  it("re-saving keeps config keys the section does not edit and turns the adapter off explicitly", async () => {
    const fn = await panel("OFFICE", [
      integration({ provider: "bitbucket", purpose: "code", root_id: "acme/app", credential_ref: "office/bitbucket", config: { enabled: true, note_ref: "CHG-1" } }),
    ]);
    const form = await screen.findByRole("form", { name: /Kode — Bitbucket/ });
    expect(within(form).getByDisplayValue("acme/app")).toBeInTheDocument();
    const box = within(form).getByLabelText(/Aktifkan adapter/) as HTMLInputElement;
    expect(box.checked).toBe(true);
    await userEvent.click(box);
    await userEvent.click(within(form).getByRole("button", { name: /Simpan Bitbucket/ }));
    await waitFor(() => expect(putBodies(fn)).toHaveLength(1));
    expect(putBodies(fn)[0]).toEqual({
      provider: "bitbucket",
      purpose: "code",
      root_id: "acme/app",
      credential_ref: "office/bitbucket",
      config: { enabled: false, note_ref: "CHG-1" },
    });
  });
});

describe("OFFICE CI — Bitbucket Pipelines", () => {
  it("sends the exact step / commit-status names per gate as config.gate_G*", async () => {
    const fn = await panel("OFFICE");
    const form = await screen.findByRole("form", { name: /CI — Bitbucket Pipelines/ });
    expect(within(form).getAllByRole("textbox")).toHaveLength(4); // G1, G2, G3, G5
    fireEvent.change(within(form).getByLabelText(/Gate G1/), { target: { value: " Lint " } });
    fireEvent.change(within(form).getByLabelText(/Gate G3/), { target: { value: "Unit tests, Integration tests" } });
    await userEvent.click(within(form).getByRole("button", { name: /Simpan Bitbucket Pipelines/ }));
    await waitFor(() => expect(putBodies(fn)).toHaveLength(1));
    expect(putBodies(fn)[0]).toEqual({
      provider: "bitbucket-pipelines",
      purpose: "ci",
      config: { gate_G1: "Lint", gate_G3: "Unit tests, Integration tests" },
    });
  });

  it("loads the stored map, and clearing a gate removes only that key", async () => {
    const fn = await panel("OFFICE", [
      integration({ provider: "bitbucket-pipelines", purpose: "ci", config: { gate_G1: "Lint", gate_G2: "Unit", gate_G5: "Secret scan" } }),
    ]);
    const form = await screen.findByRole("form", { name: /CI — Bitbucket Pipelines/ });
    expect(within(form).getByDisplayValue("Secret scan")).toBeInTheDocument();
    fireEvent.change(within(form).getByLabelText(/Gate G2/), { target: { value: "" } });
    await userEvent.click(within(form).getByRole("button", { name: /Simpan Bitbucket Pipelines/ }));
    await waitFor(() => expect(putBodies(fn)).toHaveLength(1));
    expect(putBodies(fn)[0]).toEqual({ provider: "bitbucket-pipelines", purpose: "ci", config: { gate_G1: "Lint", gate_G5: "Secret scan" } });
  });
});

describe("OFFICE Confluence space id", () => {
  it("asks for the numeric REST v2 space id, not a space key", async () => {
    await panel("OFFICE");
    const conf = await screen.findByRole("form", { name: /Dokumentasi — Confluence/ });
    expect(within(conf).getByPlaceholderText("98765")).toBeInTheDocument();
    expect(within(conf).getByText(/bukan kunci space/)).toBeInTheDocument();
    expect(within(conf).queryByPlaceholderText("ENG")).not.toBeInTheDocument();
  });

  it("re-saving Jira keeps a config key set elsewhere (issue_type)", async () => {
    const fn = await panel("OFFICE", [
      integration({ provider: "jira", purpose: "issues", tenant_id: "acme.atlassian.net", root_id: "ENG", credential_ref: "office/atlassian", config: { deployment: "CLOUD", enabled: true, issue_type: "Bug" } }),
    ]);
    const form = await screen.findByRole("form", { name: /Tracking kerja — Jira/ });
    await userEvent.click(within(form).getByRole("button", { name: /Simpan Jira/ }));
    await waitFor(() => expect(putBodies(fn)).toHaveLength(1));
    expect(putBodies(fn)[0]).toEqual({
      provider: "jira",
      purpose: "issues",
      tenant_id: "acme.atlassian.net",
      root_id: "ENG",
      credential_ref: "office/atlassian",
      config: { deployment: "CLOUD", enabled: true, issue_type: "Bug" },
    });
  });
});

describe("generic form 'Atur integrasi lain'", () => {
  it("prefills the stored integration and sends its config back instead of wiping it", async () => {
    const fn = await panel("OFFICE", [
      integration({ provider: "bitbucket", purpose: "code", root_id: "acme/app", credential_ref: "office/bitbucket", config: { enabled: true } }),
    ]);
    await screen.findByText("Atur integrasi lain");
    const form = screen.getByRole("button", { name: "Simpan konfigurasi" }).closest("form") as HTMLFormElement;
    // switch away and back: the stored integration of the selected provider + purpose is loaded each time
    fireEvent.change(within(form).getByLabelText(/Provider/), { target: { value: "jira" } });
    await waitFor(() => expect(within(form).queryByDisplayValue("acme/app")).not.toBeInTheDocument());
    fireEvent.change(within(form).getByLabelText(/Provider/), { target: { value: "bitbucket" } });
    await waitFor(() => expect(within(form).getByDisplayValue("acme/app")).toBeInTheDocument());
    expect(within(form).getByText(/dipertahankan/)).toBeInTheDocument();
    await userEvent.click(within(form).getByRole("button", { name: "Simpan konfigurasi" }));
    await waitFor(() => expect(putBodies(fn)).toHaveLength(1));
    expect(putBodies(fn)[0]).toEqual({
      provider: "bitbucket",
      purpose: "code",
      root_id: "acme/app",
      tenant_id: null,
      credential_ref: "office/bitbucket",
      config: { enabled: true },
    });
  });
});

describe("PERSONAL deployment target (W6-099) and GitHub Actions pipeline reference", () => {
  it("registers the staging target with its https base URL and optional health path", async () => {
    const fn = await panel("PERSONAL");
    const form = await screen.findByRole("form", { name: /Staging — Deployment target/ });
    expect(within(form).getByText(/tidak pernah men-deploy/)).toBeInTheDocument();
    fireEvent.change(within(form).getByLabelText(/URL dasar target/), { target: { value: "https://staging.contoh.id" } });
    fireEvent.change(within(form).getByLabelText(/Path health/), { target: { value: "/api/health" } });
    await userEvent.click(within(form).getByRole("button", { name: /Simpan Deployment target \(Staging\)/ }));
    await waitFor(() => expect(putBodies(fn)).toHaveLength(1));
    expect(putBodies(fn)[0]).toEqual({ provider: "deployment-target", purpose: "staging", root_id: "https://staging.contoh.id", config: { health_path: "/api/health" } });
  });

  it("production is its own target; an empty health path sends no key and an invalid one cannot be saved", async () => {
    const fn = await panel("PERSONAL");
    const form = await screen.findByRole("form", { name: /Produksi — Deployment target/ });
    const save = within(form).getByRole("button", { name: /Simpan Deployment target \(Produksi\)/ });
    fireEvent.change(within(form).getByLabelText(/URL dasar target/), { target: { value: "http://evil.example" } });
    expect(save).toBeDisabled();
    expect(within(form).getByText(/Format tidak valid/)).toBeInTheDocument();
    fireEvent.change(within(form).getByLabelText(/URL dasar target/), { target: { value: "https://app.contoh.id" } });
    fireEvent.change(within(form).getByLabelText(/Path health/), { target: { value: "health" } });
    expect(save).toBeDisabled();
    fireEvent.change(within(form).getByLabelText(/Path health/), { target: { value: "" } });
    await userEvent.click(save);
    await waitFor(() => expect(putBodies(fn)).toHaveLength(1));
    expect(putBodies(fn)[0]).toEqual({ provider: "deployment-target", purpose: "production", root_id: "https://app.contoh.id", config: {} });
  });

  it("sends the GitHub Actions job names per gate", async () => {
    const fn = await panel("PERSONAL");
    const form = await screen.findByRole("form", { name: /CI — GitHub Actions/ });
    fireEvent.change(within(form).getByLabelText(/Gate G2/), { target: { value: "backend (windows-latest), backend (macos-latest)" } });
    await userEvent.click(within(form).getByRole("button", { name: /Simpan GitHub Actions/ }));
    await waitFor(() => expect(putBodies(fn)).toHaveLength(1));
    expect(putBodies(fn)[0]).toEqual({ provider: "github-actions", purpose: "ci", config: { gate_G2: "backend (windows-latest), backend (macos-latest)" } });
  });

  it("OFFICE has no deployment target section (no OFFICE environment in this build)", async () => {
    await panel("OFFICE");
    await screen.findByRole("form", { name: /Kode — Bitbucket/ });
    expect(screen.queryByRole("form", { name: /Deployment target/ })).not.toBeInTheDocument();
  });
});

describe("legacy and malformed values", () => {
  it("drops a legacy target alias (config.repo) on re-save instead of failing with TARGET_IN_CONFIG", async () => {
    const fn = await panel("PERSONAL", [
      integration({
        domain: "PERSONAL",
        provider: "github-issues",
        purpose: "issues",
        root_id: "owner/app",
        credential_ref: "personal/github",
        config: { repo: "owner/app", issue_granularity: "feature", milestone_labels: true },
      }),
    ]);
    const form = await screen.findByRole("form", { name: /Tracking kerja — GitHub Issues/ });
    await userEvent.click(within(form).getByRole("button", { name: /Simpan GitHub Issues/ }));
    await waitFor(() => expect(putBodies(fn)).toHaveLength(1));
    expect(putBodies(fn)[0]).toEqual({
      provider: "github-issues",
      purpose: "issues",
      root_id: "owner/app",
      credential_ref: "personal/github",
      config: { issue_granularity: "feature", milestone_labels: true },
    });
  });

  it("editableConfig keeps only scalar, non-target, non-proof keys", async () => {
    const { editableConfig } = await import("./IntegrationsPanel");
    expect(
      editableConfig({ enabled: true, gate_G1: "Lint", repo: "o/r", parent_id: "1", project_key: "ENG", _verification: {}, root_id_name: "X", nested: { a: 1 } }),
    ).toEqual({ enabled: true, gate_G1: "Lint" });
  });

  it("a Bitbucket root that is not workspace/repo_slug cannot be saved and is marked invalid", async () => {
    const fn = await panel("OFFICE");
    const form = await screen.findByRole("form", { name: /Kode — Bitbucket/ });
    const root = within(form).getByLabelText(/Repository Bitbucket/);
    fireEvent.change(root, { target: { value: "acme" } });
    expect(within(form).getByRole("button", { name: /Simpan Bitbucket/ })).toBeDisabled();
    expect(root).toHaveAttribute("aria-invalid", "true");
    expect(within(form).getByText(/Format tidak valid/)).toBeInTheDocument();
    fireEvent.change(root, { target: { value: "acme/app" } });
    expect(root).not.toHaveAttribute("aria-invalid");
    expect(within(form).getByRole("button", { name: /Simpan Bitbucket/ })).toBeEnabled();
    expect(putBodies(fn)).toHaveLength(0);
  });
});

describe("integrationBody", () => {
  it("never sends controller-written proof back and removes emptied keys", async () => {
    const { integrationBody, sections } = await import("./IntegrationsPanel");
    const spec = sections("OFFICE", "").find((s) => s.provider === "bitbucket-pipelines")!;
    const body = integrationBody(spec, { "config.gate_G1": "", "config.gate_G2": "Unit", "config.gate_G3": "", "config.gate_G5": "" }, {
      gate_G1: "Lint",
      _verification: { connectivity: "CONNECTED" },
      root_id_name: "X",
    });
    expect(body).toEqual({ provider: "bitbucket-pipelines", purpose: "ci", config: { gate_G2: "Unit" } });
  });
});
