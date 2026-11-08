"""registry-service constants (shared ones live in agent_common.constants)."""

SERVICE_NAME = "registry-service"
API_V1 = "/api/v1"
MCP_PATH = "/mcp"

# REST wrapper roots. `POST /api/v1/factory/{tool}` dispatches to the MCP tool of that name; `POST
# /api/v1/mcp/{server}/{tool}` proxies a tool on another registered MCP server (both share the same pipeline).
FACTORY_PREFIX = f"{API_V1}/factory"
CATALOGUE_PREFIX = f"{API_V1}/mcp"
NOTIFICATIONS_PREFIX = f"{API_V1}/notifications"

# Skill version lifecycle states and the legal transitions (docs/impl_registry_service.md section 1).
STATE_DRAFT = "draft"
STATE_IN_REVIEW = "in_review"
STATE_PUBLISHED = "published"
STATE_ARCHIVED = "archived"
STATE_REJECTED = "rejected"

# Principal types an access policy can be attached to (agent_common.db.AccessPolicy.principal_type).
PRINCIPAL_TYPES = frozenset({"user", "group", "project", "client"})

# Delegated-admin role bundles (seeded policies, not a separate mechanism). A user "is" a role when the matching
# policy bundle is attached to them; `super_admin` is the only cross-tenant role.
ROLE_SUPER_ADMIN = "super_admin"
ROLE_CLIENT_ADMIN = "client_admin"

# The grant that marks a super admin: allow every action on every resource. Used both as the seeded bundle and to
# recognise a super admin when building a policy Subject.
SUPER_ADMIN_ACTION = "*"
SUPER_ADMIN_RESOURCE = "*"
