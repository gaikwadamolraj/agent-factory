"""registry-service: the platform admin & authoring plane.

Skills and their versions/approvals, IAM-style access policies and groups, delegated user/client/project
administration, in-app notifications and an MCP tool-catalogue proxy. Every capability is defined once as an MCP
tool and reached two ways behind one shared auth+policy pipeline: an MCP front door (service tokens) and a hardened
REST wrapper (`POST /api/v1/factory/{tool}`). See docs/impl_registry_service.md.
"""
