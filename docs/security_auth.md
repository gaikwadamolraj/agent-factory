# Authentication and authorization

Zero trust: every service verifies every token itself, in process. Only `auth-service` signs tokens; nothing
else holds a signing secret. Code: `services/platform/auth-service`, `agent_common.security`, `agent_common.authz`,
`agent_common.middleware`.

## Token types

| Token | Audience (`aud`) | Lifetime | Issued by | Verified by | Content |
|---|---|---|---|---|---|
| access | `ats-api` | 5 min (`ACCESS_TOKEN_MINUTES`) | auth-service at login / refresh | every user-facing service (orchestrator, auth-service) | `sub` user id, `cid` client id, `role`, `username`, `authz_ver` |
| refresh | `ats-refresh` | 1 day (`REFRESH_TOKEN_DAYS`), single use | auth-service | auth-service only | `sub`, `jti` (allow-listed in Redis, consumed on refresh, revoked on logout) |
| service | the called service's name (`agent-skill`, ...) | 5 min (`SERVICE_TOKEN_MINUTES`) | auth-service `POST /oauth/token` | the called service | `sub` calling service, `kind=service` |

All tokens: ES256, header `kid`, claims `iss` (`AUTH_ISSUER`), `iat`, `exp`, `jti`. Audiences never overlap, so a
token of one kind is useless elsewhere; a service token can never carry `ats-api` / `ats-refresh` (refused in the
auth-service policy). Browser tokens travel only in httpOnly, SameSite=Strict cookies (`ats_access`,
`ats_refresh`); cookie-authenticated writes need `X-Requested-With` (CSRF guard). API clients may send
`Authorization: Bearer`.

## Keys, JWKS and rotation

* `auth-signing-key` (agent-factory-secrets): PEM of an EC P-256 private key. `kid` = RFC 7638 thumbprint of the
  public key, so every replica and restart agrees on it.
* `GET /.well-known/jwks.json` publishes the public key(s); `GET /.well-known/openid-configuration` names the issuer,
  `jwks_uri` and `token_endpoint`.
* Services use `JwksVerifier` (`AUTH_JWKS_URL`): keys fetched once and cached; a token with an unknown `kid` refetches
  once (at most every `AUTH_JWKS_MIN_REFETCH_S`, so random kids cannot flood auth-service); fetches go through
  `resilient_call` (timeout, bounded retry, breaker `auth.jwks`). If auth-service is down, cached keys keep working;
  with no keys at all the answer is `SYS-UNA-0001` (retryable). Checks: ES256 only, signature, `iss`, `aud`, `exp`,
  `iat`, 30 s leeway.
* Rotation: copy the current key to `auth-signing-key-previous`, generate a new `auth-signing-key`, restart
  auth-service. New tokens use the new key; both public keys are in the JWKS, so older tokens stay valid; services
  pick up the new key on the first unknown `kid`. Remove the previous key after `REFRESH_TOKEN_DAYS`. A key removed
  from the JWKS stays trusted by a running service until it restarts (no periodic refetch by default;
  `AUTH_JWKS_CACHE_TTL_S` enables one).

## Authorization: tenant, membership, role (versioned cache)

Checked in process, without calling auth-service:

1. `JWTAuthMiddleware`: valid access token, and `authz_ver` not older than the user's current authz version.
2. `TenantMiddleware` (tenant paths): `X-Client-Id` = token `cid`; the user is active and a member of the active
   project `X-Project-Id`.
3. Admin endpoints (`require_admin`): role admin in the token and in the current snapshot.

`agent_common.authz` keeps, per user, `authz:user:{id}:version` and per client `authz:client:{id}:version` in Redis
(no TTL, never evicted). The authz version is their sum (both only grow, so every change raises it). Role, active
state and project ids are cached as `authz:user:{id}:v{version}` (1 h TTL); the database is read only on a miss.
The version itself is cached in process for `AUTHZ_VERSION_CACHE_TTL_S` (5 s), so a change reaches other replicas
within 5 s; the replica that made it sees it at once.

Every change of a user's role, active state, password or memberships calls `AuthzService.bump_user` /
`bump_authz_version(redis, env, user_id)` (after the database commit); client-wide changes call
`bump_client_authz_version`. The user's older access tokens then get `AUTH-EXP-0002` (401, retryable): the UI
refreshes silently, and the refresh issues a token with the new version (or fails for a disabled user).

## Service-to-service: client_credentials

* Policy (one place): `services.authService.oauthClients` in values.yaml -> `OAUTH_CLIENT_AUDIENCES`
  (`ats-orchestrator` -> skill, compute, knowledge graph, guardrail; `agent-knowledge-graph` -> guardrail).
* Secret: key `client-secret-<caller>` of agent-factory-secrets. The caller reads it as `SERVICE_CLIENT_SECRET`, the
  auth-service as `CLIENT_SECRET_<CALLER>` - the same key, nothing to keep in sync. Hashing it in the Secret would
  protect nothing (the plain value sits in the same Secret); the auth-service keeps only SHA-256 digests in memory and
  compares in constant time, unknown clients included.
* Callers: `ServiceTokenProvider` caches one token per audience until 80% of its lifetime; `McpToolClient`,
  `KnowledgeClient`, `GuardrailsClient` and the orchestrator's MCP clients (`ServiceTokenAuth`, a token per HTTP
  request of long MCP sessions) use it.
* Called services: `secure_mcp_app` = request context -> security headers -> service token (`aud` = own name,
  caller in `ALLOWED_CALLERS`) -> `TOOL_POLICY` -> MCP.
* Brute force: 5 wrong secrets per client in 15 min lock it (`AUTH-RTL-0002`), like 5 failed sign-ins lock a
  username (`AUTH-RTL-0001`).

## Routing

The browser reaches only the frontend (Traefik, `/ats`); its server-side proxy sends `/api/backend/v1/auth/*` and
`/api/backend/v1/admin/*` to auth-service and the rest to the orchestrator. There is no Traefik forwardAuth: an edge
check would add a call per request and protect nothing the services do not already check. `/oauth/token` and
`/.well-known/*` have no public route. NetworkPolicy `auth-service-traffic`: ingress from registry-service pods only;
egress DNS, Postgres, Redis, OTLP.
