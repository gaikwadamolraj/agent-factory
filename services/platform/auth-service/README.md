# auth-service

Owns user authentication and every token of the platform. FastAPI on port 8004, Service DNS `auth-service`
(namespace `ats-platform`). Security model: [docs/security_auth.md](../../docs/security_auth.md).

| Endpoint | Who calls it | What it does |
|---|---|---|
| `POST /api/v1/auth/login` | browser (via frontend proxy) | checks username + password, sets httpOnly cookies `ats_access` + `ats_refresh`, returns the session; locks a username for 15 min after 5 failures (`AUTH-RTL-0001`) |
| `POST /api/v1/auth/refresh` | browser | rotates both tokens with the refresh cookie (single use: a replayed refresh token is rejected) |
| `POST /api/v1/auth/logout` | browser | revokes the refresh token, clears the cookies |
| `GET /api/v1/auth/me` | browser | user, client and projects |
| `GET/POST /api/v1/admin/users`, `PATCH /api/v1/admin/users/{id}` | browser (admins) | user management inside the admin's client (role `admin` in the token **and** in the database) |
| `POST /oauth/token` | services | OAuth2 `client_credentials`: service token for `audience` (5 min); locks a client after 5 wrong secrets (`AUTH-RTL-0002`) |
| `GET /.well-known/jwks.json` | every service | public ES256 keys (`kid` = RFC 7638 thumbprint) |
| `GET /.well-known/openid-configuration` | tooling | issuer, `jwks_uri`, `token_endpoint` |
| `GET /healthz` | kubelet | liveness/readiness |

The URLs under `/api/v1` are the same as when the orchestrator served them, so the browser code did not change;
the frontend's server-side proxy sends `/api/backend/v1/auth/*` and `/api/backend/v1/admin/*` here
(`INTERNAL_AUTH_URL`) and everything else to the orchestrator. `/oauth/token` and `/.well-known/*` are
cluster-internal (no route from the internet).

Other services never call this service per request: they verify tokens locally with the cached JWKS
(`agent_common.security.JwksVerifier`) and get service tokens with `ServiceTokenProvider` (cached per audience).

## Layout

```
src/auth_service/
├── main.py / app.py      entry point, app factory, middleware
├── config.py             AuthSettings (keys, lifetimes, cookies, lockout limits, OAUTH_CLIENT_AUDIENCES)
├── signing.py            SigningKey / KeyRing: PEM -> key, kid, JWKS, rotation
├── tokens.py             TokenIssuer: access / refresh / service tokens, local verification
├── clients.py            ServiceClients: client secrets (SHA-256 digests, constant-time compare), allowed audiences
├── throttle.py           FailureThrottle: Redis failure counters (logins, client secrets)
├── refresh_store.py      RefreshTokenStore: Redis allow-list of live refresh tokens
├── passwords.py          bcrypt hash / verify
└── api/                  auth.py, admin.py, oauth.py, well_known.py, schemas.py, deps.py
```

State: users/clients/projects/memberships in Postgres (`agent_common.db`, no tables of its own); refresh-token
allow-list and failure counters in Redis (`agent_common.redis_keys`: `REFRESH_TOKEN`, `LOGIN_ATTEMPTS`,
`CLIENT_AUTH_ATTEMPTS`).

## Configuration

| Variable | Source | Meaning |
|---|---|---|
| `AUTH_SIGNING_KEY` | Secret `auth-signing-key` | PEM, EC P-256 private key (`openssl genpkey -algorithm EC -pkeyopt ec_paramgen_curve:P-256`) |
| `AUTH_SIGNING_KEY_PREVIOUS` | Secret `auth-signing-key-previous` (optional) | previous key during a rotation; its public key stays in the JWKS |
| `CLIENT_SECRET_<CLIENT>` | Secret `client-secret-<client>` | secret of each calling service (same key the caller reads as `SERVICE_CLIENT_SECRET`) |
| `OAUTH_CLIENT_AUDIENCES` | ConfigMap (Helm: `services.authService.oauthClients`) | `caller=svc1\|svc2,caller2=svc3` |
| `AUTH_ISSUER`, `AUTH_JWKS_URL`, `AUTH_TOKEN_URL` | ConfigMap `ats-auth-config` | shared by every service |
| `ACCESS_TOKEN_MINUTES` (5), `REFRESH_TOKEN_DAYS` (1), `SERVICE_TOKEN_MINUTES` (5) | ConfigMap | lifetimes |
| `COOKIE_SECURE` (true), `COOKIE_SAMESITE` (strict) | ConfigMap | cookie flags (`false` only on plain-http dev) |
| `LOGIN_MAX_FAILURES` (5), `CLIENT_AUTH_MAX_FAILURES` (5) | ConfigMap | lockout thresholds (15-minute window) |
| `DATABASE_URL`, `REDIS_URL` | Secret | Postgres + Redis |

Key rotation: copy the current key to `auth-signing-key-previous`, put a new key into `auth-signing-key`, restart
the auth-service. Services pick up the new key on the first token with an unknown `kid`. Remove the previous key
after `REFRESH_TOKEN_DAYS`.

Password hash by hand (first admin of a new client):
`uv run python -c "from auth_service.passwords import hash_password; print(hash_password('<password>'))"`.

## Run and test

```bash
uv sync
uv run pytest -q          # SQLite + fake Redis + in-memory keys; no Postgres, Redis or real secrets needed
docker build -f services/platform/auth-service/Dockerfile .      # from the repo root
```
