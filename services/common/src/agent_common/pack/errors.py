"""Domain-pack error codes.

PCK is the pack domain: authoring and validating the declarative bundle that defines a domain. Codes never name
a service or a function (CLAUDE.md section 5), and the messages tell an author what to fix.
"""
from agent_common.errors import register


class PackErrors:
    PACK_INVALID = register("PCK-INV-0001", 422, "The pack file is not a valid pack document.",
                            "Fix the problem listed in 'fields' in pack.json and save the version again.")
    PACK_REFERENCES_INVALID = register("PCK-INV-0002", 422,
                                       "The pack points at files or keys that this version does not contain.",
                                       "Add the missing files to the version, or correct the paths in pack.json.")
    PACK_NOT_FOUND = register("PCK-NFD-0001", 404, "This domain pack is not available for your organisation.",
                              "Ask an administrator to publish and activate a version of the pack.")
    JOURNEY_NOT_ENABLED = register("PCK-FBD-0001", 403, "This pack does not offer that journey.",
                                   "Enable the journey in the pack and publish a new version.")
