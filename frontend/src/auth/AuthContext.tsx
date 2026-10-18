import { useQuery, useQueryClient } from "@tanstack/react-query";
import { createContext, useCallback, useContext, useEffect, useMemo, type ReactNode } from "react";
import { ApiError, get, post, setCsrfToken } from "../api/client";
import { restartEventStream, stopEventStream } from "../lib/events";

export type Domain = "PERSONAL" | "OFFICE";

export interface SessionView {
  user: { id: string; username: string; display_name: string; role: "SUPER_ADMIN" | "VIEWER"; must_change_password: boolean };
  csrf_token: string;
  expires_at: string;
  allowed_domains: Domain[];
  context: { domain: Domain | null; project_id: string | null };
}

interface AuthState {
  session: SessionView | null;
  loading: boolean;
  isAdmin: boolean;
  domain: Domain | null;
  projectId: string | null;
  login: (username: string, password: string) => Promise<void>;
  logout: () => Promise<void>;
  selectContext: (domain: Domain, projectId?: string | null) => Promise<void>;
  refresh: () => Promise<unknown>;
}

const Ctx = createContext<AuthState | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const qc = useQueryClient();
  const q = useQuery<SessionView | null>({
    queryKey: ["session"],
    queryFn: async () => {
      try {
        return await get<SessionView>("/api/auth/session");
      } catch (e) {
        if (e instanceof ApiError && (e.status === 401 || e.code === "PASSWORD_CHANGE_REQUIRED")) return null;
        throw e;
      }
    },
    retry: false,
    staleTime: 60_000,
  });

  useEffect(() => {
    setCsrfToken(q.data?.csrf_token ?? null);
  }, [q.data]);

  const setSession = useCallback(
    (s: SessionView | null) => {
      setCsrfToken(s?.csrf_token ?? null);
      qc.setQueryData(["session"], s);
    },
    [qc],
  );

  const login = useCallback(
    async (username: string, password: string) => {
      const s = await post<SessionView>("/api/auth/login", { username, password });
      setSession(s);
      qc.invalidateQueries({ predicate: (query) => query.queryKey[0] !== "session" });
    },
    [qc, setSession],
  );

  const logout = useCallback(async () => {
    try {
      await post("/api/auth/logout");
    } finally {
      // Close the event stream now and forget its cursor: it was opened with this session's scope.
      stopEventStream();
      setSession(null);
      qc.clear();
    }
  }, [qc, setSession]);

  const selectContext = useCallback(
    async (domain: Domain, projectId: string | null = null) => {
      const s = await post<SessionView>("/api/session/context", { domain, project_id: projectId });
      setSession(s);
      // Domain switch: drop every cached view so no data from the other domain lingers (§35), and
      // reopen the event stream: the backend applies the domain/project scope when a stream opens.
      qc.removeQueries({ predicate: (query) => query.queryKey[0] !== "session" });
      restartEventStream();
    },
    [qc, setSession],
  );

  const value = useMemo<AuthState>(
    () => ({
      session: q.data ?? null,
      loading: q.isLoading,
      isAdmin: q.data?.user.role === "SUPER_ADMIN",
      domain: q.data?.context.domain ?? null,
      projectId: q.data?.context.project_id ?? null,
      login,
      logout,
      selectContext,
      refresh: () => q.refetch(),
    }),
    [q, login, logout, selectContext],
  );
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useAuth(): AuthState {
  const v = useContext(Ctx);
  if (!v) throw new Error("useAuth outside AuthProvider");
  return v;
}
