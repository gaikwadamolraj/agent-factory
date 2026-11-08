# PgBouncer — connection pooling and its authentication

PgBouncer sits between the services and Postgres so the KEDA-scaled pods' many short-lived connections do not
each become a Postgres backend. It is **off by default** (`infra.pgbouncer.enabled: false`) and nothing in the
platform depends on it.

Chart: `deploy/helm/agent-factory/templates/infra/pgbouncer/`. Values: `infra.pgbouncer`.
Image: `edoburu/pgbouncer` (pinned in values). Everything below was checked against that image's entrypoint;
re-check it when the pin moves.

---

## 1. Why authentication needs care here

The image generates `/etc/pgbouncer/pgbouncer.ini` and `/etc/pgbouncer/userlist.txt` from environment variables
at startup. Its `generate_userlist_if_needed` step writes the value of `DB_PASSWORD` into `userlist.txt`, and
when `auth_type` is `scram-sha-256` or `plain` it writes it **in cleartext** (only `md5` auth gets a hash).

So the naive configuration — `DB_USER` / `DB_PASSWORD` = the Postgres superuser, `AUTH_TYPE=scram-sha-256` —
puts the superuser password in cleartext in a file inside the pooler container, readable by anything that can
exec into the pod or read the container filesystem. That is the default the chart shipped with, and it is why
PgBouncer stays disabled.

Two modes are therefore supported.

| Mode | `infra.pgbouncer.authUser` | What the pod holds | Use for |
|---|---|---|---|
| **userlist** (default) | empty | the Postgres superuser password, cleartext in `userlist.txt` | a throwaway dev cluster only |
| **auth_query** | set | only the lookup role's password; no application credential | everything else |

## 2. auth_query mode

PgBouncer connects to Postgres as a dedicated low-privilege role and asks it for each connecting user's stored
password. No application or superuser credential is written into the pod.

When `authUser` is set, the template deliberately stops passing `DB_USER`/`DB_PASSWORD` from
`postgres-user`/`postgres-password` and passes the lookup role instead, so the entrypoint has no application
password to write.

### 2.1 Create the lookup role and function (once per database)

Run as the Postgres superuser, against every database the pooler fronts. The function is `SECURITY DEFINER` so
the lookup role itself needs no access to `pg_shadow`.

```sql
-- 1. the lookup role: it may log in and call the function below, nothing else
CREATE ROLE pgbouncer_auth LOGIN PASSWORD 'THE-VALUE-OF-pgbouncer-auth-password';

-- 2. the lookup function, owned by the superuser, returning only the asked-for user
CREATE OR REPLACE FUNCTION public.pgbouncer_get_auth(username text)
RETURNS TABLE (usename name, passwd text)
LANGUAGE sql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
  SELECT usename, passwd FROM pg_catalog.pg_shadow WHERE usename = $1;
$$;

-- 3. nobody but the lookup role may call it
REVOKE ALL ON FUNCTION public.pgbouncer_get_auth(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.pgbouncer_get_auth(text) TO pgbouncer_auth;
```

This is **not** in `services/schema/seed/` on purpose: the role's password is a secret and seeds are committed.

### 2.2 Secret and values

```yaml
# agent-infra-secrets (created by hand, never committed)
pgbouncer-auth-password: "<same value as the role's password>"
```

```yaml
infra:
  pgbouncer:
    enabled: true
    authUser: pgbouncer_auth
    authDbname: ""        # empty = look up in the client's own database
    # authQuery already defaults to: SELECT usename, passwd FROM public.pgbouncer_get_auth($1)
```

### 2.3 Rotating the lookup password

`ALTER ROLE pgbouncer_auth PASSWORD '<new>'`, update the Secret key, restart the PgBouncer Deployment. No
application restart is needed — application credentials never lived in the pooler.

## 3. The other hardening the chart applies

| Setting | Value | Why |
|---|---|---|
| `adminUsers` | `pgbouncer-console-disabled` | the image defaults `admin_users` to `postgres`, which would let the application superuser run `PAUSE`/`RECONNECT` on the pooler. Pointing it at a role that does not exist closes the console. Name a real operator role only if you want it. |
| `statsUsers` | empty | no read-only stats role unless asked for |
| `serverTlsSslmode` | `prefer` | uses TLS on the pooler -> Postgres hop when Postgres offers it, still connects when it does not. Move to `require`, or `verify-full` with a CA file, once Postgres serves certificates. |
| `clientTlsSslmode` | empty | app -> pooler stays plaintext in-cluster, as the rest of the in-cluster traffic does |
| `ignoreStartupParameters` | `extra_float_digits,search_path,options` | transaction pooling cannot honour per-session startup parameters; asyncpg and SQLAlchemy send all three |
| container `securityContext` | `allowPrivilegeEscalation: false`, drop `ALL` | same baseline as every other workload |
| `automountServiceAccountToken` | `false` | the pooler never calls the Kubernetes API |
| NetworkPolicy | `pgbouncer-clients` | only pods of the release namespace may connect |

The root filesystem is **not** read-only: the entrypoint writes `pgbouncer.ini` and `userlist.txt` under
`/etc/pgbouncer` at every start. Making it read-only would mean supplying both files ourselves (a ConfigMap and
a Secret) and dropping the env-var path entirely — worth doing only if we also move off the image's generator.

## 4. Turning it on

1. Create the role and function (§2.1) and add `pgbouncer-auth-password` to `agent-infra-secrets`.
2. Set `infra.pgbouncer.enabled: true` and `authUser: pgbouncer_auth`.
3. Point the services at it: `database-url` host becomes
   `pgbouncer.agent-infra.svc.cluster.local:6432`, and set `DB_PGBOUNCER=true` on the services so the driver
   disables statement caching (transaction pooling breaks prepared statements).
4. **Leave the schema migration on the direct Postgres connection.** DDL and advisory locks must not go through
   a transaction pooler.
5. `./deploy.sh apply`.

## 5. What is still unvalidated

Deploy is on hold, so none of this has run against a live Postgres. At deploy time, verify in this order:

- the pod starts and `kubectl -n agent-infra exec deploy/pgbouncer -- cat /etc/pgbouncer/userlist.txt` contains
  **no application password** (auth_query mode: it should hold at most the lookup role);
- an application pod connects through the pooler and runs a query;
- a wrong password is rejected (the lookup path actually runs);
- under load, Postgres `pg_stat_activity` shows the pool size, not one backend per app connection;
- `search_path`/`extra_float_digits` warnings do not appear in the service logs.

If auth_query misbehaves against the pinned image, fall back to `authUser: ""` **only** on a dev cluster, and
re-check the entrypoint for the `AUTH_USER`/`AUTH_QUERY`/`AUTH_DBNAME` variable names before changing the
template.
