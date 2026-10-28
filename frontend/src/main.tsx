import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import { ApiError } from "./api/client";
import App from "./App";
import { AuthProvider } from "./auth/AuthContext";
import { installPreloadRecovery } from "./lib/preloadRecovery";
import { applyTheme } from "./lib/theme";
import "./index.css";

// Apply the stored light/dark choice before the first paint (CSP forbids an inline <head> script).
applyTheme();
// A tab still on an older build reloads once when one of its chunks is gone after a rebuild (R28).
installPreloadRecovery();

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      refetchOnWindowFocus: true,
      staleTime: 15_000,
      // A 4xx (403 scope, 404 not visible in this domain, 409 state) is an answer, not a glitch:
      // show it immediately instead of retrying. Network errors and 5xx are retried once.
      retry: (failureCount, error) => !(error instanceof ApiError && error.status < 500) && failureCount < 1,
    },
  },
});

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <AuthProvider>
          <App />
        </AuthProvider>
      </BrowserRouter>
    </QueryClientProvider>
  </StrictMode>,
);
