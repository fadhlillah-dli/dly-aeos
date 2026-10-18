import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { MemoryRouter } from "react-router-dom";
import { vi } from "vitest";
import { DocumentTreeView, ReviewBox } from "./DocumentTree";
import { buildTree, needsAttention, safeHref, trackerStatusCode } from "./labels";
import { MarkdownPreview } from "./MarkdownPreview";
import type { DocItem, PublicationView } from "./types";

const auth = { isAdmin: false, domain: "PERSONAL", projectId: "p1", session: null };
vi.mock("../../auth/AuthContext", () => ({ useAuth: () => auth }));

function doc(over: Partial<DocItem>): DocItem {
  return {
    id: over.logical_doc_id ?? "x",
    logical_doc_id: "x",
    node: "00",
    title: "X",
    kind: "DOC",
    doc_type: "PRD",
    parent_logical_id: null,
    owner: "Owner",
    status: "APPROVED",
    status_reason: "",
    content_type: "AI_MAINTAINED",
    timing: "PROJECT_CREATION",
    template_key: null,
    source_plan_version_id: null,
    content_hash: "sha256:abc",
    external_revision: null,
    last_reviewed_at: null,
    last_synced_at: null,
    rendered_at: null,
    links: {},
    pending_sections: [],
    documentation_pending: false,
    sections: [],
    order_index: 0,
    mode: "LIVE",
    external: [],
    freshness: { rendered_at: null, last_synced_at: null, external_stale: false },
    open_revision_inputs: 0,
    ...over,
  };
}

const ITEMS: DocItem[] = [
  doc({ logical_doc_id: "root", title: "Contoh App", kind: "FOLDER", doc_type: "FOLDER", order_index: 1 }),
  doc({ logical_doc_id: "00-product-home", title: "00. Product Home", parent_logical_id: "root", order_index: 2,
    status: "PUBLISHED", external: [{ provider: "google-drive", external_id: "D1", url: "https://docs.google.com/document/d/D1/edit", revision: "r1", sync_status: "SYNCED", last_synced_at: null }] }),
  doc({ logical_doc_id: "02-requirements", title: "02. Requirements", kind: "FOLDER", doc_type: "FOLDER", parent_logical_id: "root", order_index: 3 }),
  doc({ logical_doc_id: "02-requirements/prd-a", title: "PRD — A", parent_logical_id: "02-requirements", order_index: 4,
    pending_sections: ["Problem", "Risks"], documentation_pending: true, open_revision_inputs: 1 }),
  doc({ logical_doc_id: "08-monitoring", title: "Monitoring", parent_logical_id: "root", order_index: 5, status: "NOT_STARTED" }),
];

describe("publications helpers", () => {
  it("builds the folder → document tree from parent ids", () => {
    const tree = buildTree(ITEMS);
    expect(tree).toHaveLength(1);
    expect(tree[0].children.map((c) => c.doc.logical_doc_id)).toEqual(["00-product-home", "02-requirements", "08-monitoring"]);
    expect(tree[0].children[1].children[0].doc.title).toBe("PRD — A");
  });

  it("flags PARTIAL / CONFLICT / UNKNOWN / FAILED for attention, never PUBLISHED", () => {
    for (const s of ["PARTIAL", "CONFLICT", "UNKNOWN", "FAILED"]) expect(needsAttention(s)).toBe(true);
    expect(needsAttention("PUBLISHED")).toBe(false);
    expect(needsAttention("SKIPPED")).toBe(false);
  });
});

describe("DocumentTreeView", () => {
  it("shows status text, pending sections, human-edit conflicts and external links", () => {
    render(<DocumentTreeView items={ITEMS} selected={null} onSelect={() => {}} />);
    expect(screen.getByText("Terpublikasi")).toBeInTheDocument();
    expect(screen.getByText("Belum dimulai")).toBeInTheDocument();
    expect(screen.getByText("google-drive").closest("a")).toHaveAttribute("href", "https://docs.google.com/document/d/D1/edit");
  });

  it("never turns a non-http provider URL into a link", () => {
    const evil = doc({ logical_doc_id: "x1", title: "Evil", status: "PUBLISHED",
      external: [{ provider: "confluence", external_id: "E", url: "javascript:alert(1)", revision: "1", sync_status: "SYNCED", last_synced_at: null }] });
    const { container } = render(<DocumentTreeView items={[evil]} selected={null} onSelect={() => {}} />);
    expect(container.querySelector("a")).toBeNull();
    expect(safeHref("javascript:alert(1)")).toBeUndefined();
    expect(safeHref(" https://example.org/x ")).toBe("https://example.org/x");
  });
});

describe("tracker projection", () => {
  it("never shows a closed/Done issue as AEOS DONE", () => {
    for (const s of ["closed", "closed:completed", "Done", "Selesai", "Resolved"]) expect(trackerStatusCode(s)).toBe("TRACKER_CLOSED");
    expect(trackerStatusCode("open")).toBe("QUEUED");
    expect(trackerStatusCode("")).toBe("UNKNOWN");
  });
});

describe("ReviewBox", () => {
  const stale = doc({ id: "d9", logical_doc_id: "d9", content_hash: "sha256:new", last_reviewed_at: new Date().toISOString(),
    review: { reviewed_at: new Date().toISOString(), reviewed_by: "u1", content_hash: "sha256:old", status: "REVIEW_STALE" } });

  it("shows a stale review honestly and hides the action for VIEWER", () => {
    render(wrap(<ReviewBox doc={stale} />));
    expect(screen.getByText("Review untuk konten lama")).toBeInTheDocument();
    expect(screen.getByText(/bukan persetujuan/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Tandai sudah direview/ })).toBeNull();
  });

  it("lets an admin record a review bound to the exact content hash", async () => {
    auth.isAdmin = true;
    const fetchMock = vi.fn(async () => new Response(JSON.stringify({ id: "d9", status: "REVIEWED" }), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    try {
      render(wrap(<ReviewBox doc={stale} />));
      screen.getByRole("button", { name: /Tandai sudah direview/ }).click();
      await waitFor(() => expect(fetchMock).toHaveBeenCalled());
      const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
      expect(url).toBe("/api/documents/d9/review");
      expect(init.method).toBe("POST");
      expect(JSON.parse(String(init.body))).toMatchObject({ content_hash: "sha256:new" });
    } finally {
      auth.isAdmin = false;
      vi.unstubAllGlobals();
    }
  });
});

describe("MarkdownPreview", () => {
  it("renders structure without injecting HTML", () => {
    const md = "# Judul\n<!-- aeos:section=x -->\n\n| A | B |\n|---|---|\n| 1 | 2 |\n\n<script>alert(1)</script>\n\n[x](javascript:alert(1)) [ok](https://example.org)";
    const { container } = render(<MarkdownPreview markdown={md} />);
    expect(screen.getByRole("heading", { name: "Judul" })).toBeInTheDocument();
    expect(container.querySelector("script")).toBeNull();
    expect(container.textContent).toContain("<script>alert(1)</script>");
    expect(container.textContent).not.toContain("aeos:section");
    const links = Array.from(container.querySelectorAll("a")).map((a) => a.getAttribute("href"));
    expect(links).toEqual(["https://example.org"]);
  });
});

function wrap(children: ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return (
    <QueryClientProvider client={qc}>
      <MemoryRouter>{children}</MemoryRouter>
    </QueryClientProvider>
  );
}

const PUB: PublicationView = {
  id: "pub1",
  project_id: "p1",
  domain: "PERSONAL",
  plan_id: "plan1",
  plan_version_id: "v1",
  plan_version_number: 1,
  approved_hash: "sha256:aaa",
  preview_hash: "sha256:bbb",
  decision_id: "d1",
  publication_decision_id: null,
  status: "PREVIEW",
  status_reason: "",
  targets: [],
  blocked_targets: [],
  mode: "LIVE",
  created_at: new Date().toISOString(),
  updated_at: new Date().toISOString(),
  started_at: null,
  finished_at: null,
  counts: { PENDING: 1 },
  preview: { notes: [] },
  status_dimensions: {
    APPROVED: { status: "APPROVED", detail: "Decision d1", source: "Decision registry" },
    IMPLEMENTED: { status: "UNKNOWN", detail: "-", source: "workforce" },
    ACTIVE: { status: "UNKNOWN", detail: "-", source: "operations" },
    VERIFIED: { status: "NOT_PROVEN", detail: "-", source: "verification" },
    ARCHITECTURE_SOURCE_UPDATED: { status: "NOT_RECORDED", detail: "-", source: "decisions" },
    DOCS_PUBLISHED: { status: "NOT_PUBLISHED", detail: "-", source: "publisher" },
  },
  approval_statement: "Satu persetujuan mencakup 1 operasi konkret pada publication ini (U-10)",
  attention: [],
  operations: [
    { id: "o1", order_index: 1, provider: "google-drive", operation: "create_folder", logical_id: "root", title: "Contoh", status: "UNKNOWN",
      attempts: 1, max_attempts: 3, external_id: null, external_url: null, external_revision: null, error: "timeout — outcome unknown",
      next_attempt_at: null, last_attempt_at: null, reconciled_at: null, depends_on: [], idempotency_key: "sha256:k", payload_hash: "sha256:p" },
  ],
};

describe("PublicationPanel", () => {
  it("shows separate status dimensions and hides Approve & Publish for VIEWER", async () => {
    const fetchMock = vi.fn(async (url: string) => {
      const body = url.includes("publication-candidates")
        ? { items: [{ plan_id: "plan1", title: "Rencana", state: "APPROVED", current_version_id: "v2", approval_status: "APPROVED",
            approved: { plan_version_id: "v1", version: 1, approved_hash: "sha256:aaa", is_latest: false } }], checked_at: null }
        : url.startsWith("/api/publications/pub1")
          ? { ...PUB, checked_at: new Date().toISOString() }
          : { items: [PUB], checked_at: null };
      return new Response(JSON.stringify(body), { status: 200 });
    });
    vi.stubGlobal("fetch", fetchMock);
    const { PublicationPanel } = await import("./PublicationPanel");
    render(wrap(<PublicationPanel projectId="p1" />));
    await waitFor(() => expect(screen.getByText(/versi lebih baru yang belum disetujui/)).toBeInTheDocument());
    expect(screen.queryByRole("button", { name: /Preview publikasi/ })).toBeNull(); // VIEWER: no write actions
    (await screen.findAllByText("Preview (belum disetujui)"))[0].closest("tr")!.click();
    await waitFor(() => expect(screen.getByText("Dokumen terpublikasi")).toBeInTheDocument());
    expect(screen.getByText("Aktif (runtime)")).toBeInTheDocument();
    expect(screen.getByText(/operasi butuh perhatian/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Approve & Publish/ })).toBeNull();
    expect(screen.getByText(/Hanya SUPER_ADMIN/)).toBeInTheDocument();
    vi.unstubAllGlobals();
  });

  it("offers the owner Approve & Publish, Reject and Request Revision for a preview (§05 step 9)", async () => {
    auth.isAdmin = true;
    const fetchMock = vi.fn(async (url: string) => {
      const body = url.includes("publication-candidates")
        ? { items: [], checked_at: null }
        : url.startsWith("/api/publications/pub1")
          ? { ...PUB, checked_at: new Date().toISOString() }
          : { items: [PUB], checked_at: null };
      return new Response(JSON.stringify(body), { status: 200 });
    });
    vi.stubGlobal("fetch", fetchMock);
    try {
      const { PublicationPanel } = await import("./PublicationPanel");
      render(wrap(<PublicationPanel projectId="p1" />));
      (await screen.findAllByText("Preview (belum disetujui)"))[0].closest("tr")!.click();
      const approve = await screen.findByRole("button", { name: /Approve & Publish/ });
      expect(approve).toBeDisabled(); // needs the explicit confirmation checkbox first
      expect(screen.getByRole("button", { name: /Tolak preview/ })).toBeInTheDocument();
      expect(screen.getByRole("link", { name: /Minta revisi rencana/ })).toHaveAttribute("href", "/plans/plan1");
      expect(screen.getByText(PUB.approval_statement)).toBeInTheDocument();
    } finally {
      auth.isAdmin = false;
      vi.unstubAllGlobals();
    }
  });
});
