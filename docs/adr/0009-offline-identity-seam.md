# 0009 — A verifier seam so identity is testable without the directory

## Status

Accepted — 2026-09-08. Extends `03-auth-security.md` §2 and §4.

## Context

`03-auth-security.md` §2 specifies OIDC authorization-code + PKCE against the
corporate IdP, and §4.3 specifies MSAL's Windows broker for the local MCP
process. Both require a live directory. Neither is reachable from the
development environment, and will not be until immediately before deployment.

That is not merely inconvenient. Look at what Phase 1's acceptance criteria
actually assert (`12-roadmap.md`):

- A geologist logs in via SSO and lands with the correct team memberships
- User A cannot read User B's private dataset — at both application and RLS layer
- MCP calls execute as the requesting user (two users, different results)
- Every endpoint reading the data plane has an explicit permission check

Every one of them needs a `Principal`. If the only way to obtain one is a
round trip to Entra ID, then none of them can be exercised during development,
and the most security-critical phase in the project ships having been tested
once, by hand, on deployment day. `03` §5 calls identity propagation "the
single most important control in this document"; a control that cannot be
tested is not a control.

The tempting shortcut is a development bypass — a header naming the user, or
an `if settings.debug: return admin_principal`. That is how production systems
end up authenticating on `X-User-Id`. The bypass is never removed, because
nothing fails when it stays.

## Decision

Introduce one seam, at the narrowest point: **verifying a bearer token and
producing claims**.

```python
class TokenVerifier(Protocol):
    async def verify(self, token: str) -> Claims: ...
```

Two implementations, selected by `settings.auth_mode`:

- **`OidcTokenVerifier`** — fetches JWKS from the discovery document, verifies
  signature, issuer, expiry, and **audience** (`03` §4.4), and returns claims.
  The production path.
- **`DevTokenVerifier`** — verifies tokens signed by a locally-generated key
  held only in the development environment. Issues them too, through
  `POST /auth/dev/token`, so tests and a developer's browser can obtain one
  for any of a fixed set of fictional users.

Everything downstream of `verify()` is identical in both modes: claims are
mapped to an `app_user` row, group claims reconcile `team_member`, a
`Principal` is constructed, `principal_session` sets the RLS context, and
service functions call `require()`. That is the code the acceptance criteria
are about, and it is now exercised offline by the same path production uses.

**What is not behind the seam**: authorization. There is exactly one
permission model, one set of RLS policies, and one `require()`. `auth_mode`
changes who you are proved to be, never what you may do.

### The guard

A development verifier that can run in production is worse than no development
verifier. Four independent controls, because one is a single edit away from
being wrong:

1. **Settings refuse the combination.** `environment=prod` with
   `auth_mode=dev` raises at construction, before the app starts.
2. **The dev module refuses to construct** a verifier when the environment is
   prod, even if the settings check is bypassed or reordered.
3. **Startup logs the active mode at WARNING** when it is not `oidc`, naming
   the environment. Silence is not a defence; the log line is.
4. **A test asserts the refusal**, so removing the guard fails CI rather than
   passing quietly.

The dev signing key is generated per process and never written to disk, so a
token minted in development is worthless anywhere else and cannot outlive the
process that issued it.

## Consequences

Phase 1's security criteria become testable now: two real users, two real
token verifications, two real RLS contexts, asserting that one cannot read the
other's data at both layers. Those tests are the point of this ADR.

**Deployment-day risk narrows to one integration** — does the real IdP issue
tokens this verifier accepts — rather than the whole phase. The things most
likely to be wrong there are the audience value, the groups claim name, and
whether groups arrive as object ids or display names. All three are
configuration, and `03` §2's team sync is written against `idp_group_id`
precisely so the answer can be either.

**The same seam absorbs the Entra vs on-prem AD fork** that
[[0007-multi-user-directory-sso]] left open for week 1. The fallback path —
Kerberos/SPNEGO for the browser, SSPI for the local MCP process — is a third
verifier behind the same protocol, not a rewrite of everything downstream.

**The cost is a code path that never runs in production.** That is a real
cost: it must be maintained, and it is a place a mistake can hide. It is
accepted because the alternative is an untested authorization layer, and
because the guard makes the failure mode loud rather than silent.

**Would change our mind.** A development-accessible IdP tenant. If one appears,
`auth_mode=oidc` against it is strictly better than the dev verifier, and the
dev path can be deleted rather than maintained — it has no callers outside
tests and the login route.
