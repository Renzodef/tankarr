import { FormEvent, useState } from "react";
import { api } from "../api";
import { Logo } from "../components";
import type { AuthStatus } from "../types";

export default function LoginPage({
  onAuthenticated,
}: {
  onAuthenticated: (status: AuthStatus) => void;
}) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [rememberMe, setRememberMe] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setSubmitting(true);
    setError(null);
    try {
      onAuthenticated(await api.login(username, password, rememberMe));
    } catch (caught) {
      const message = caught instanceof Error ? caught.message : String(caught);
      setError(message || "Login failed");
      setPassword("");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <main className="login-page">
      <section className="login-card" aria-labelledby="login-title">
        <div className="login-brand">
          <Logo size={54} />
          <h1 id="login-title">tankarr</h1>
        </div>
        <form onSubmit={(event) => void submit(event)}>
          <div className="form-row">
            <label htmlFor="login-username">Username</label>
            <input
              id="login-username"
              className="input"
              type="text"
              value={username}
              autoComplete="username"
              autoCapitalize="none"
              autoFocus
              required
              onChange={(event) => setUsername(event.target.value)}
            />
          </div>
          <div className="form-row">
            <label htmlFor="login-password">Password</label>
            <input
              id="login-password"
              className="input"
              type="password"
              value={password}
              autoComplete="current-password"
              required
              onChange={(event) => setPassword(event.target.value)}
            />
          </div>
          <label className="login-remember">
            <input
              type="checkbox"
              checked={rememberMe}
              onChange={(event) => setRememberMe(event.target.checked)}
            />
            <span>Remember me</span>
          </label>
          {error ? (
            <div className="login-error" role="alert">
              {error}
            </div>
          ) : null}
          <button className="btn btn-primary login-submit" type="submit" disabled={submitting}>
            {submitting ? "Signing in…" : "Log In"}
          </button>
        </form>
      </section>
    </main>
  );
}
