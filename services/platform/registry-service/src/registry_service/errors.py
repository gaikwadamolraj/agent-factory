"""registry-service error codes (format DOMAIN-CATEGORY-NNNN; registered in the shared catalog at import).

DOMAINs used here: SCR (skills/screening authoring), AGT (agent/tool catalogue), AUTH (authorization), USR (user
management), VAL (validation). Codes never name a service or function (CLAUDE.md section 5).
"""
from agent_common.errors import register


class FactoryErrors:
    # ---- skills & versions
    SKILL_EXISTS = register("SCR-CNF-0001", 409, "A skill with this domain and name already exists.",
                            "Open the existing skill, or choose a different name.")
    SKILL_NOT_FOUND = register("SCR-NFD-0001", 404, "This skill does not exist in your organisation.",
                               "Refresh the skills list.")
    VERSION_NOT_FOUND = register("SCR-NFD-0002", 404, "This skill version does not exist.",
                                 "Refresh the versions list.")
    VERSION_NOT_EDITABLE = register("SCR-CNF-0002", 409, "Only a draft version can be edited.",
                                    "Branch a new draft from the published version instead.")
    INVALID_TRANSITION = register("SCR-INV-0001", 409, "This action is not allowed from the version's current state.",
                                  "Refresh the version; its lifecycle state may have changed.")
    NO_PUBLISHED_VERSION = register("SCR-NFD-0003", 404, "This skill has no published version to activate.",
                                    "Submit and approve a version first.")
    NO_RUNNABLE_VERSION = register("SCR-NFD-0004", 404, "This skill has no version to run.",
                                   "Activate a published version, or ask for an exact version number.")

    # ---- tool catalogue proxy
    UNKNOWN_MCP_SERVER = register("AGT-NFD-0001", 404, "That tool server is not registered.",
                                  "Pick a server from the tool catalogue.")
    UNKNOWN_TOOL = register("AGT-NFD-0002", 404, "That operation does not exist.",
                            "Check the tool name against the catalogue.")

    # ---- authorization / delegated admin guardrails
    POLICY_NOT_FOUND = register("AUTH-NFD-0001", 404, "That access policy does not exist.",
                                "Refresh the policies list.")
    GROUP_NOT_FOUND = register("AUTH-NFD-0002", 404, "That group does not exist.", "Refresh the groups list.")
    ESCALATION_FORBIDDEN = register("AUTH-FBD-0010", 403, "You cannot grant access broader than your own.",
                                    "Ask a platform administrator to perform this change.")
    CROSS_CLIENT_FORBIDDEN = register("AUTH-FBD-0011", 403, "You can only administer your own organisation.",
                                      "Switch to the correct organisation and try again.")

    # ---- delegated identity (answers surfaced from the auth-service)
    IDENTITY_UPSTREAM = register("USR-UNA-0001", 502, "The identity service could not complete the change.",
                                 "Retry in a moment; if it keeps failing, contact support.", retryable=True)

    # ---- generic
    PROJECT_NOT_FOUND = register("TEN-NFD-0001", 404, "That project does not exist in your organisation.",
                                 "Refresh the projects list.")
