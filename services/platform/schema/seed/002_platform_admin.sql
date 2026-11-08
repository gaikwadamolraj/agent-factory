-- Bootstrap admin policies for local/dev (idempotent; applied only when SEED_DEMO_DATA=true). Never enable in PROD.
--
-- Roles are seeded policy bundles (docs/impl_agent_factory.md sections 4 & 6b), not a separate mechanism:
--   * super_admin  -> allow every action on every resource (unconditional); the only cross-tenant, platform-owner
--                     role. agent-factory recognises a super admin as the holder of this exact wildcard bundle.
--   * client_admin -> a scoped bundle guarded by the same_client condition, so it only ever affects its own client.
--
-- The platform owner (acme.admin) gets super_admin; a concrete client_admin is attached to globex.recruiter as the
-- bootstrap admin of the globex client, demonstrating the client_admin bundle. Idempotent via NOT EXISTS on
-- (client_id, principal_type, principal_id, name). external_id/created_at use their column defaults.

-- super_admin for the platform owner (acme.admin)
INSERT INTO access_policies (client_id, principal_type, principal_id, name, document, enabled)
SELECT u.client_id, 'user', u.id, 'super-admin',
       '{"statements": [{"effect": "allow", "actions": ["*"], "resources": ["*"]}]}'::jsonb, true
FROM users u
WHERE u.username = 'acme.admin'
  AND NOT EXISTS (
      SELECT 1 FROM access_policies p
      WHERE p.client_id = u.client_id AND p.principal_type = 'user' AND p.principal_id = u.id
        AND p.name = 'super-admin');

-- client_admin (scoped to own client via same_client) for the bootstrap admin of the globex client
INSERT INTO access_policies (client_id, principal_type, principal_id, name, document, enabled)
SELECT u.client_id, 'user', u.id, 'client-admin',
       '{"statements": [{"effect": "allow",
         "actions": ["skill.read", "skill.create", "skill.edit", "skill.submit", "skill.approve",
                     "skill.activate", "skill.archive", "policy.read", "policy.manage", "group.read",
                     "group.manage", "project.read", "project.manage", "admin.users.manage", "catalogue.read"],
         "resources": ["skill:*", "policy:*", "group:*", "project:*", "user:*", "catalogue:*"],
         "conditions": {"same_client": true}}]}'::jsonb, true
FROM users u
WHERE u.username = 'globex.recruiter'
  AND NOT EXISTS (
      SELECT 1 FROM access_policies p
      WHERE p.client_id = u.client_id AND p.principal_type = 'user' AND p.principal_id = u.id
        AND p.name = 'client-admin');
