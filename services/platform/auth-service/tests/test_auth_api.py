"""Sign-in, refresh rotation, logout, current user and user management (fake Redis + SQLite)."""
from agent_common.constants import COOKIE_ACCESS_TOKEN, COOKIE_REFRESH_TOKEN
from agent_common.security import JwksVerifier, UserClaims, verify_user_token
from agent_common.security.testing import LocalTokenIssuer
from conftest import PASSWORD, TEST_ISSUER

CSRF = {"X-Requested-With": "test"}


async def login(service, username="acme.recruiter", password=PASSWORD):
    return await service.client.post("/api/v1/auth/login", json={"username": username, "password": password})


# ------------------------------------------------------------------------------------------------ login
async def test_login_sets_httponly_cookies_and_returns_the_session(service):
    response = await login(service)
    assert response.status_code == 200
    body = response.json()
    assert body["user"] == {"id": 2, "username": "acme.recruiter", "role": "recruiter"}
    assert body["client"] == {"id": 1, "name": "Acme"} and [p["name"] for p in body["projects"]] == ["Engineering"]
    assert body["expiresIn"] == 300
    cookies = response.headers.get_list("set-cookie")
    assert any(c.startswith(f"{COOKIE_ACCESS_TOKEN}=") and "HttpOnly" in c for c in cookies)
    assert any(c.startswith(f"{COOKIE_REFRESH_TOKEN}=") and "HttpOnly" in c for c in cookies)


async def test_issued_access_token_is_es256_and_verifies_with_the_published_jwks(service):
    await login(service)
    token = service.client.cookies[COOKIE_ACCESS_TOKEN]
    jwks = (await service.client.get("/.well-known/jwks.json")).json()

    async def published_keys():
        return jwks

    claims = await verify_user_token(JwksVerifier(published_keys, TEST_ISSUER), token)
    assert (claims.user_id, claims.client_id, claims.role, claims.username) == (2, 1, "recruiter", "acme.recruiter")


async def test_wrong_password_unknown_and_disabled_users_get_the_same_error(service):
    for username, password in [("acme.recruiter", "wrong-password"), ("nobody", PASSWORD), ("acme.disabled", PASSWORD)]:
        response = await login(service, username, password)
        assert response.status_code == 401 and response.json()["error"]["code"] == "AUTH-INV-0002"


async def test_username_is_locked_after_max_failures_even_for_the_right_password(service):
    for _ in range(3):                                           # login_max_failures=3 in the test settings
        assert (await login(service, password="wrong-password")).status_code == 401
    locked = await login(service)
    assert locked.status_code == 429 and locked.json()["error"]["code"] == "AUTH-RTL-0001"
    assert int(locked.headers["Retry-After"]) > 0
    assert (await login(service, "acme.admin")).status_code == 200      # other usernames are not affected


async def test_success_resets_the_failure_counter(service):
    for _ in range(2):
        await login(service, password="wrong-password")
    assert (await login(service)).status_code == 200
    for _ in range(2):
        await login(service, password="wrong-password")
    assert (await login(service)).status_code == 200                  # 2 + 2 failures, never 3 in a row


async def test_invalid_login_payload_is_a_validation_error(service):
    response = await service.client.post("/api/v1/auth/login", json={"username": "a b", "password": "x"})
    assert response.status_code == 422 and response.json()["error"]["code"] == "VAL-INV-0001"


# ------------------------------------------------------------------------------------------------ refresh / logout
async def test_refresh_rotates_the_token_and_a_replayed_refresh_token_is_rejected(service):
    await login(service)
    old_refresh = service.client.cookies[COOKIE_REFRESH_TOKEN]
    rotated = await service.client.post("/api/v1/auth/refresh", headers=CSRF)
    assert rotated.status_code == 200 and rotated.json()["user"]["id"] == 2
    new_refresh = service.client.cookies[COOKIE_REFRESH_TOKEN]
    assert new_refresh != old_refresh

    service.client.cookies.set(COOKIE_REFRESH_TOKEN, old_refresh)     # attacker replays the used token
    replay = await service.client.post("/api/v1/auth/refresh", headers=CSRF)
    assert replay.status_code == 401 and replay.json()["error"]["code"] == "AUTH-INV-0001"


async def test_refresh_without_cookie_or_with_an_access_token_fails(service):
    assert (await service.client.post("/api/v1/auth/refresh")).json()["error"]["code"] == "AUTH-MIS-0001"
    await login(service)
    service.client.cookies.set(COOKIE_REFRESH_TOKEN, service.client.cookies[COOKIE_ACCESS_TOKEN])
    assert (await service.client.post("/api/v1/auth/refresh")).status_code == 401      # wrong audience


async def test_logout_revokes_the_refresh_token_and_clears_cookies(service):
    await login(service)
    refresh = service.client.cookies[COOKIE_REFRESH_TOKEN]
    response = await service.client.post("/api/v1/auth/logout")
    assert response.status_code == 204
    assert not [key for key in service.redis.values if ":auth:refresh:" in key]
    service.client.cookies.set(COOKIE_REFRESH_TOKEN, refresh)
    assert (await service.client.post("/api/v1/auth/refresh")).status_code == 401


# ------------------------------------------------------------------------------------------------ me
async def test_me_needs_a_token_and_rejects_tokens_of_other_issuers(service):
    assert (await service.client.get("/api/v1/auth/me")).status_code == 401
    foreign = LocalTokenIssuer(issuer=TEST_ISSUER).user_token(user_id=2)      # right issuer name, unknown key
    response = await service.client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {foreign}"})
    assert response.status_code == 401
    await login(service)
    me = await service.client.get("/api/v1/auth/me")
    assert me.status_code == 200 and me.json()["user"]["username"] == "acme.recruiter"


# ------------------------------------------------------------------------------------------------ admin
async def test_admin_lists_and_creates_users_of_their_own_client_only(service):
    await login(service, "acme.admin")
    users = await service.client.get("/api/v1/admin/users")
    assert users.status_code == 200
    assert {u["username"] for u in users.json()} == {"acme.admin", "acme.recruiter", "acme.disabled"}

    created = await service.client.post("/api/v1/admin/users", headers=CSRF, json={
        "username": "acme.new", "password": "Secret@123", "role": "recruiter"})
    assert created.status_code == 201 and created.json()["isActive"] is True
    duplicate = await service.client.post("/api/v1/admin/users", headers=CSRF, json={
        "username": "acme.new", "password": "Secret@123"})
    assert duplicate.status_code == 409 and duplicate.json()["error"]["code"] == "USR-CNF-0001"
    assert (await login(service, "acme.new", "Secret@123")).status_code == 200


async def test_admin_cannot_touch_other_clients_or_remove_the_last_admin(service):
    await login(service, "acme.admin")
    other_client = await service.client.patch("/api/v1/admin/users/4", headers=CSRF, json={"isActive": False})
    assert other_client.status_code == 404 and other_client.json()["error"]["code"] == "USR-NFD-0001"
    last_admin = await service.client.patch("/api/v1/admin/users/1", headers=CSRF, json={"role": "recruiter"})
    assert last_admin.status_code == 400 and last_admin.json()["error"]["code"] == "USR-INV-0001"


async def test_recruiters_get_403_and_cookie_writes_need_the_csrf_header(service):
    await login(service)
    assert (await service.client.get("/api/v1/admin/users")).status_code == 403
    await login(service, "acme.admin")
    no_csrf = await service.client.post("/api/v1/admin/users", json={"username": "x.y.z", "password": "Secret@123"})
    assert no_csrf.status_code == 403 and no_csrf.json()["error"]["code"] == "AUTH-FBD-0001"


async def test_admin_role_in_the_token_is_not_enough_when_the_database_disagrees(service):
    # A validly signed token that still says "admin" for a user who is a recruiter in the database (e.g. demoted
    # after sign-in) gets 403 at once, not only after the next refresh.
    stale = service.tokens.access_token(UserClaims(user_id=2, client_id=1, role="admin", username="acme.recruiter"))
    response = await service.client.get("/api/v1/admin/users", headers={"Authorization": f"Bearer {stale.value}"})
    assert response.status_code == 403 and response.json()["error"]["code"] == "AUTH-FBD-0001"


# ------------------------------------------------------------------------------------------------ authz versions
async def test_user_change_by_an_admin_makes_the_users_old_token_stale_until_refresh(service):
    await login(service)                                             # acme.recruiter (id 2)
    recruiter_cookies = dict(service.client.cookies)
    old_token = recruiter_cookies[COOKIE_ACCESS_TOKEN]
    bearer = {"Authorization": f"Bearer {old_token}"}
    assert (await service.client.get("/api/v1/auth/me", headers=bearer)).status_code == 200

    service.client.cookies.clear()
    await login(service, "acme.admin")
    changed = await service.client.patch("/api/v1/admin/users/2", headers=CSRF, json={"role": "admin"})
    assert changed.status_code == 200 and changed.json()["role"] == "admin"

    stale = await service.client.get("/api/v1/auth/me", headers=bearer)
    assert stale.status_code == 401 and stale.json()["error"]["code"] == "AUTH-EXP-0002"
    assert stale.json()["error"]["retryable"] is True

    service.client.cookies.clear()
    for name, value in recruiter_cookies.items():
        service.client.cookies.set(name, value)
    refreshed = await service.client.post("/api/v1/auth/refresh", headers=CSRF)     # silent refresh in the UI
    assert refreshed.status_code == 200 and refreshed.json()["user"]["role"] == "admin"
    assert (await service.client.get("/api/v1/admin/users")).status_code == 200    # new token, new permissions


async def test_issued_access_token_carries_the_current_authz_version(service):
    await service.container.authz.bump_user(2)
    await service.container.authz.bump_client(1)
    await login(service)
    claims = await service.tokens.verify_access(service.client.cookies[COOKIE_ACCESS_TOKEN])
    assert claims.authz_ver == 2
