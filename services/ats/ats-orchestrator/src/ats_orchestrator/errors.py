"""Orchestrator error codes (format DOMAIN-CATEGORY-NNNN; registered in the shared catalog)."""
from agent_common.errors import register


class OrchErrors:
    # Sign-in and user-management codes (AUTH-RTL-0001, USR-*) moved with those endpoints to the auth-service.
    CHAT_NOT_FOUND = register("CHAT-NFD-0001", 404, "This chat does not exist in the selected project.",
                              "Refresh the chat list or create a new chat.")
    MESSAGE_EMPTY = register("CHAT-MIS-0001", 400, "The message is empty.",
                             "Type a message or attach a resume PDF.")
    MESSAGE_TOO_LONG = register("CHAT-LIM-0001", 413, "The message or job description is too long.",
                                "Shorten the text and send it again.")

    FILE_INSTRUCTIONS = register("FILE-INV-0004", 422, "The document contains text that tries to instruct the AI "
                                 "(for example hidden, encoded or 'system' instructions about scoring).",
                                 "Upload a clean copy of the document. The file was not stored or screened.")
    FILE_NOT_PDF = register("FILE-INV-0001", 415, "Only PDF files are accepted.", "Attach the resume as a PDF file.")
    FILE_TOO_LARGE = register("FILE-LIM-0001", 413, "The PDF is too large.", "Attach a PDF within the size limit.")
    FILE_TYPE_NOT_ALLOWED = register("FILE-INV-0003", 415, "This file type is not accepted for a job description.",
                                     "Upload the job description as PDF, TXT or MD, or paste the text.")
    DOCUMENT_NOT_FOUND = register("FILE-NFD-0001", 404, "This chat has no such document.",
                                  "Upload the resume or job description first.")
    FILE_NO_TEXT = register("FILE-INV-0002", 422, "No text could be read from the PDF.",
                            "The PDF is probably a scanned image; attach a text-based PDF.")

    JD_MISSING = register("SCR-MIS-0001", 400, "This chat has no job description yet.",
                          "Paste the job description in the Job description panel.")
    RESUME_MISSING = register("SCR-MIS-0002", 400, "This chat has no resume yet.", "Attach the candidate's resume PDF.")
    # Recorded against one subject of a bulk match, never returned to a request: the run carries on and the
    # row says why this subject has no score.
    SUBJECT_NOT_GRADEABLE = register("SCR-MIS-0003", 422,
                                     "This subject could not be graded against the criteria.",
                                     "Check that the job description and the subject's document are both "
                                     "readable in this project.")
    SUBJECT_NOT_FOUND = register("SCR-NFD-0001", 404, "This subject does not exist in the selected project.",
                                 "Refresh the list; it may belong to another project or have been removed.")

    AGENT_UNAVAILABLE = register("AGT-UNA-0001", 503, "The screening agent is temporarily unavailable.",
                                 "Retry after 'retryAfter' seconds.", retryable=True)
    AGENT_TIMEOUT = register("AGT-TMO-0001", 504, "The screening took too long and was stopped.",
                             "Retry; very long resumes or a busy model take longer.", retryable=True)

    COMPUTE_UNAVAILABLE = register("CMP-UNA-0002", 503, "The calculator is temporarily unavailable.",
                                   "Retry after 'retryAfter' seconds.", retryable=True)
    COMPUTE_TIMEOUT = register("CMP-TMO-0001", 504, "The calculation took too long and was stopped.",
                               "Ask a simpler question or retry.", retryable=True)

    TURN_IN_PROGRESS = register("WFL-CNF-0001", 409, "This chat is still working on the previous message.",
                                "Wait for it to finish or abort it, then send again.")
    TURN_NOT_FOUND = register("WFL-NFD-0001", 404, "There is no running request with this id.",
                              "It may have finished already; refresh the chat.")
    TURN_ABORTED = register("WFL-CAN-0001", 499, "The request was stopped.", "Send a new message when ready.")

    RUN_NOT_FOUND = register("WFL-NFD-0002", 404, "There is no such run in the selected project.",
                             "Refresh the list of runs; it may belong to another project.")
    RUN_ALREADY_FINISHED = register("WFL-CNF-0002", 409, "This run has already finished.",
                                    "Nothing to cancel. Refresh to see its outcome.")

    # Resolving the client's domain pack from registry-service. Internal: a failure falls back to the packaged
    # guidance and ontology rather than surfacing, so a registry outage never breaks screening or chat.
    REGISTRY_UNAVAILABLE = register("AGT-UNA-0002", 503, "The pack registry is not reachable.",
                                    "Retry shortly; the built-in guidance is used meanwhile.", retryable=True)
    REGISTRY_TIMEOUT = register("AGT-TMO-0002", 504, "The pack registry did not answer in time.",
                                "Retry shortly; the built-in guidance is used meanwhile.", retryable=True)
