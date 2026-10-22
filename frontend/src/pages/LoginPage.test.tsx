import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, expect, it } from "vitest";
import LoginPage from "./LoginPage";

afterEach(cleanup);

it("keeps the local form beside OpenID Connect", () => {
  render(
    <LoginPage
      onAuthenticated={() => undefined}
      oidcLoginUrl="/api/auth/oidc/login"
    />,
  );
  expect(screen.getByLabelText("Username")).toBeTruthy();
  expect(screen.getByLabelText("Password")).toBeTruthy();
  expect(screen.getByRole("button", { name: "Log In" })).toBeTruthy();
  expect(
    screen.getByRole("link", { name: "Sign in with OpenID Connect" }).getAttribute("href"),
  ).toBe("/api/auth/oidc/login");
});

it("hides the local form when local login is disabled", () => {
  render(
    <LoginPage
      onAuthenticated={() => undefined}
      oidcLoginUrl="/api/auth/oidc/login"
      localLoginEnabled={false}
    />,
  );
  expect(screen.queryByLabelText("Username")).toBeNull();
  expect(screen.queryByLabelText("Password")).toBeNull();
  expect(screen.queryByRole("button", { name: "Log In" })).toBeNull();
  expect(screen.queryByLabelText("Remember me")).toBeNull();
  const oidc = screen.getByRole("link", { name: "Sign in with OpenID Connect" });
  expect(oidc.getAttribute("href")).toBe("/api/auth/oidc/login");
});
