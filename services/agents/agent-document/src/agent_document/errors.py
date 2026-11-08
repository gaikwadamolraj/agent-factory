"""Document agent error codes (format DOMAIN-CATEGORY-NNNN; registered in the shared catalog at import).

Numbers are distinct from the ats-orchestrator FILE-* codes so a given code never means two different things.
LLM/embedding and generic internal codes are reused from the shared catalog.
"""
from agent_common.errors import Errors, register


class DocumentErrors:
    NO_CONTENT = register("FILE-MIS-0001", 422, "The upload contained no file data.",
                          "Send the file bytes as base64 in dataBase64.")
    UNSUPPORTED_TYPE = register("FILE-INV-0010", 415,
                                "This file type is not supported; only PDF, Word (.docx) and Excel (.xlsx) are read.",
                                "Upload a PDF, DOCX or XLSX file.")
    UNREADABLE = register("FILE-INV-0011", 422, "The file could not be read; it may be corrupt or password-protected.",
                          "Re-export the file and upload it again.")
    NO_TEXT = register("FILE-INV-0012", 422, "No text could be extracted from the file.",
                       "Upload a file that contains selectable text (not a scanned image).")
    EXTRACTION_TIMEOUT = register("FILE-TMO-0010", 504,
                                  "Reading this file took too long and was stopped.",
                                  "Retry after 'retryAfter' seconds, or send a smaller or simpler file.",
                                  retryable=True)
    TOO_LARGE = register("FILE-LIM-0010", 413, "The file is larger than the allowed limit.",
                         "Upload a smaller file or split it into parts.")
    # Reused shared codes
    INPUT_INVALID = Errors.VALIDATION_FAILED
    INTERNAL = Errors.INTERNAL
