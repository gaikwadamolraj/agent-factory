"""Text chunking for hybrid search."""

CHUNK_SIZE = 1200
CHUNK_OVERLAP = 200


def chunk_text(text: str, size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """Fixed-size chunks with overlap, cut at paragraph/line/space boundaries where possible."""
    text = text.strip()
    chunks, start = [], 0
    while start < len(text):
        end = min(start + size, len(text))
        if end < len(text):
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
