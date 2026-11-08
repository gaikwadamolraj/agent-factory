"""Text chunking for search. Self-contained (agent-document imports no other service) but deliberately the same
fixed-size-with-overlap approach the ats-orchestrator uses, so chunks index and search the same way everywhere.
"""


def chunk_text(text: str, size: int, overlap: int, max_chunks: int) -> list[str]:
    """Split `text` into overlapping chunks, cutting at paragraph/line/space boundaries where possible.

    Args:
        text: The text to split.
        size: Target chunk size in characters.
        overlap: Characters of overlap carried from one chunk into the next.
        max_chunks: Hard cap on the number of chunks (bounds memory/response size).

    Returns the list of non-empty chunk strings (at most `max_chunks`).
    """
    text = text.strip()
    chunks: list[str] = []
    start = 0
    while start < len(text) and len(chunks) < max_chunks:
        end = min(start + size, len(text))
        if end < len(text):
            # Prefer a natural boundary in the second half of the window so we do not cut mid-sentence.
            for separator in ("\n\n", "\n", " "):
                cut = text.rfind(separator, start + size // 2, end)
                if cut != -1:
                    end = cut
                    break
        if chunk := text[start:end].strip():
            chunks.append(chunk)
        if end >= len(text):
            break
        start = max(end - overlap, start + 1)
    return chunks
