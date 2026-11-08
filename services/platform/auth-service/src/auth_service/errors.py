"""auth-service error codes (format DOMAIN-CATEGORY-NNNN; registered in the shared catalog).

The sign-in and user-management codes keep the numbers they had in the orchestrator, so clients see no change.
"""
from agent_common.errors import register


class AuthErrors:
    LOGIN_THROTTLED = register("AUTH-RTL-0001", 429, "Too many failed sign-in attempts.",
                               "Wait for 'retryAfter' seconds, then sign in again.", retryable=True)
    CLIENT_THROTTLED = register("AUTH-RTL-0002", 429, "Too many failed service sign-ins for this client.",
                                "Wait for 'retryAfter' seconds and check the client secret.", retryable=True)
    CLIENT_CREDENTIALS = register("AUTH-INV-0003", 401, "The service client id or secret is incorrect.",
                                  "Check SERVICE_CLIENT_SECRET of the calling service.")
    GRANT_UNSUPPORTED = register("AUTH-INV-0004", 400, "Only the client_credentials grant is supported.",
                                 "Send grant_type=client_credentials.")
    AUDIENCE_FORBIDDEN = register("AUTH-FBD-0003", 403, "This service may not call the requested service.",
                                  "Add the pair to OAUTH_CLIENT_AUDIENCES of the auth-service if it is intended.")

    INVITE_INVALID = register("USR-INV-0002", 400, "This invitation link is invalid or has already been used.",
                              "Ask an administrator to send you a new invitation.")
    INVITE_EXPIRED = register("USR-EXP-0001", 400, "This invitation has expired.",
                              "Ask an administrator to send you a new invitation.")
    INVITE_NOT_FOUND = register("USR-NFD-0002", 404, "This invitation does not exist.", "Refresh the invites list.")

    USER_EXISTS = register("USR-CNF-0001", 409, "This username is already taken.", "Choose a different username.")
    USER_NOT_FOUND = register("USR-NFD-0001", 404, "This user does not exist in your organisation.",
                              "Refresh the user list.")
    LAST_ADMIN = register("USR-INV-0001", 400, "This would leave your organisation without an active admin.",
                          "Make another user admin first.")
