-- Demo tenants for local/dev environments. Idempotent: safe to run on every deploy.
-- Every demo user's password is: Demo@12345  (bcrypt hash below). Never enable SEED_DEMO_DATA in PROD.

INSERT INTO clients (slug, name) VALUES
    ('acme',   'Acme Talent'),
    ('globex', 'Globex Hiring')
ON CONFLICT (slug) DO NOTHING;

INSERT INTO projects (client_id, slug, name, description)
SELECT c.id, p.slug, p.name, p.description
FROM (VALUES
    ('acme',   'engineering', 'Engineering hiring', 'Software, data and AI roles'),
    ('acme',   'pharma',      'Pharma hiring',      'Quality, validation and clinical roles'),
    ('globex', 'sales',       'Sales hiring',       'Account executives and sales engineers')
) AS p(client_slug, slug, name, description)
JOIN clients c ON c.slug = p.client_slug
ON CONFLICT (client_id, slug) DO NOTHING;

INSERT INTO users (client_id, username, email, password_hash, role)
SELECT c.id, u.username, u.email, '$2b$12$oyIdiA2kES8pokzu6sLDpOinDUFpokEV9WtCmP5ifd1ePd09NislW', u.role
FROM (VALUES
    ('acme',   'acme.admin',     'admin@acme.example',     'admin'),
    ('acme',   'acme.recruiter', 'recruiter@acme.example', 'recruiter'),
    ('globex', 'globex.recruiter', 'recruiter@globex.example', 'recruiter')
) AS u(client_slug, username, email, role)
JOIN clients c ON c.slug = u.client_slug
ON CONFLICT (username) DO NOTHING;

INSERT INTO project_members (project_id, user_id)
SELECT p.id, u.id
FROM (VALUES
    ('acme.admin',       'acme',   'engineering'),
    ('acme.admin',       'acme',   'pharma'),
    ('acme.recruiter',   'acme',   'engineering'),
    ('globex.recruiter', 'globex', 'sales')
) AS m(username, client_slug, project_slug)
JOIN users u ON u.username = m.username
JOIN clients c ON c.slug = m.client_slug
JOIN projects p ON p.client_id = c.id AND p.slug = m.project_slug
ON CONFLICT DO NOTHING;
