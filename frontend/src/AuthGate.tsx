import { useCallback, useEffect, useState } from "react";
import App from "./App";
import { api } from "./api";
import { Logo } from "./components";
import LoginPage from "./pages/LoginPage";
import type { AuthStatus } from "./types";

export default function AuthGate() {
  const [status, setStatus] = useState<AuthStatus | null>(null);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      setStatus(await api.authStatus());
      setError(null);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    }
  }, []);

  useEffect(() => {
    void refresh();
    const authenticationRequired = () => {
      setStatus((current) =>
        current ? { ...current, authenticated: false, username: null } : current,
      );
    };
    window.addEventListener("tankarr:authentication-required", authenticationRequired);
    return () =>
      window.removeEventListener("tankarr:authentication-required", authenticationRequired);
  }, [refresh]);

  const logout = async () => {
    await api.logout();
    await refresh();
  };

  if (!status) {
    return (
      <main className="login-page">
        <section className="login-card login-loading">
          <div className="login-brand">
            <Logo size={54} />
            <h1>tankarr</h1>
          </div>
          <p className={error ? "login-error" : "muted"}>
            {error ?? "Loading…"}
          </p>
          {error ? (
            <button type="button" className="btn btn-primary" onClick={() => void refresh()}>
              Retry
            </button>
          ) : null}
        </section>
      </main>
    );
  }

  if (status.configured && status.method === "forms" && !status.authenticated) {
    return <LoginPage onAuthenticated={setStatus} />;
  }

  return <App authentication={status} onLogout={logout} />;
}
