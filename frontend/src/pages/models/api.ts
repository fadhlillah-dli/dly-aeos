import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { get, post } from "../../api/client";
import type { ActivatePayload, CapabilityTestView, ClaudeStatus, EndpointPayload, RouteDetail, RouteList } from "./types";

const enc = (key: string) => key.split("/").map(encodeURIComponent).join("/");

export function useClaudeStatus() {
  return useQuery({ queryKey: ["claude-status"], queryFn: ({ signal }) => get<ClaudeStatus>("/api/claude/status", signal), refetchInterval: 60_000 });
}

export function useRoutes() {
  return useQuery({ queryKey: ["routes"], queryFn: ({ signal }) => get<RouteList>("/api/routes", signal) });
}

export function useRouteDetail(key: string | null) {
  return useQuery({
    queryKey: ["route", key],
    queryFn: ({ signal }) => get<RouteDetail>(`/api/routes/${enc(key as string)}`, signal),
    enabled: !!key,
  });
}

export function useCapabilityTest() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (model: "opus" | "fable") => post<CapabilityTestView & { status: string }>("/api/claude/capability-test", { model }),
    onSettled: () => {
      qc.invalidateQueries({ queryKey: ["claude-status"] });
      qc.invalidateQueries({ queryKey: ["routes"] });
    },
  });
}

export function useRefreshCatalog() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => post<{ status: string; results: Record<string, { status?: string; reason?: string }> }>("/api/routes/refresh-catalog"),
    onSettled: () => qc.invalidateQueries({ queryKey: ["routes"] }),
  });
}

export function useActivateRoute(key: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: ActivatePayload) => post<{ decision_id: string; decision_kind: string }>(`/api/routes/${enc(key)}/activate`, body as unknown as Record<string, unknown>),
    onSettled: () => {
      qc.invalidateQueries({ queryKey: ["routes"] });
      qc.invalidateQueries({ queryKey: ["route", key] });
    },
  });
}

/** LOCAL route: the owner sets the loopback endpoint of their local server (a new configuration -> new TRIAL). */
export function useSetRouteEndpoint(key: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: EndpointPayload) =>
      post<{ config_changed: boolean; needs_new_trial: boolean }>(`/api/routes/${enc(key)}/endpoint`, body as unknown as Record<string, unknown>),
    onSettled: () => {
      qc.invalidateQueries({ queryKey: ["routes"] });
      qc.invalidateQueries({ queryKey: ["route", key] });
    },
  });
}

export function useDisableRoute(key: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (reason: string) => post(`/api/routes/${enc(key)}/disable`, { reason }),
    onSettled: () => {
      qc.invalidateQueries({ queryKey: ["routes"] });
      qc.invalidateQueries({ queryKey: ["route", key] });
    },
  });
}
