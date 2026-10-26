import { api, ApiError } from "../../api/client";

/**
 * Upload an attachment (.md or other) for a proposal or a research entry through the intake module (parser
 * isolation, §05/U-11); `source` labels the intake surface on the Attachment.
 * Attachments belong to a project in the active domain. The intake route is owned by the intake
 * module; both candidate paths are tried, and failure is reported — never silently skipped.
 */
const CANDIDATE_PATHS = (projectId: string) => [`/api/projects/${projectId}/attachments`, "/api/intake/attachments"];

function extractId(data: unknown): string | null {
  if (!data || typeof data !== "object") return null;
  const d = data as Record<string, unknown>;
  if (typeof d.id === "string") return d.id;
  const nested = (d.attachment ?? (Array.isArray(d.items) ? d.items[0] : undefined)) as Record<string, unknown> | undefined;
  return nested && typeof nested.id === "string" ? nested.id : null;
}

export async function uploadAttachment(projectId: string, file: File, source = "proposal"): Promise<string> {
  let lastError: unknown = null;
  for (const path of CANDIDATE_PATHS(projectId)) {
    const form = new FormData();
    form.append("file", file);
    form.append("project_id", projectId);
    // The intake upload records an optional "source" label on the Attachment (default "upload").
    form.append("source", source);
    try {
      const data = await api<unknown>(path, { method: "POST", body: form });
      const id = extractId(data);
      if (id) return id;
      throw new ApiError(500, "INTAKE_NO_ID", "intake response contained no attachment id");
    } catch (e) {
      lastError = e;
      if (e instanceof ApiError && (e.status === 404 || e.status === 405)) continue;
      throw e;
    }
  }
  throw lastError instanceof Error ? lastError : new ApiError(404, "INTAKE_UNAVAILABLE", "intake upload endpoint not available");
}
