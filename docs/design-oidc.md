# OpenID Connect login

Tankarr is a single-administrator application. OIDC is an optional way for an
operator to authenticate; it grants the same permissions as local login.
It is disabled by default and requires local forms authentication to remain
configured. The identity provider is external; Tankarr installs none.

The operator configures an HTTPS issuer, confidential client credentials, a
fixed public callback URI and an explicit allowlist of subjects or groups.
The callback is never inferred from an untrusted Host or forwarded header.
An approved subject or signed group claim grants administrator access.
Emails and display names never grant access by themselves.

Authlib implements Authorization Code with PKCE S256, state, nonce, discovery
and signed ID-token verification. The issuer must match both discovery and
the token. Browser-bound flow markers use a separate HttpOnly, Secure,
SameSite=Lax cookie. PKCE verifiers and nonces stay in an expiring, bounded
server-side cache; flows are consumed before token exchange and do not
survive a restart. No provider token is persisted or returned to the browser.

Successful login creates Tankarr's existing revocable twelve-hour session.
Password changes and local logout invalidate it under the existing rules.
OIDC failures use the existing login throttle and return generic errors;
provider error messages, tokens, codes and secrets are never logged.
Callback queries are removed from application access logs even at debug.
The cross-origin request check and security headers apply unchanged.

The API key and HTTP Basic authentication remain available for other
applications. The login form stays available unless
`TANKARR_OIDC_DISABLE_LOCAL_LOGIN` is set. That option hides the form,
rejects local form sign-in, and refuses to start unless OpenID Connect is
enabled and valid, so it cannot lock the browser out. Logout revokes the
local session without logging out the provider; an explicit new OIDC login
may therefore succeed without a second password prompt. Provider-side logout,
refresh tokens, user roles and bearer-token API authorization are outside
this first implementation.
