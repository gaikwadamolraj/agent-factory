"""Split a document into section-sized episodes for extraction.

Why: one extraction call per section keeps every prompt small (a whole resume plus the ontology overflowed an 8k
model and produced truncated JSON) and lets the model focus on one part at a time. Sections after the first get a
short context line with the start of the document (whose resume it is), so the person is resolved the same way in
every section.

Headings are ALL-CAPS lines ("PROFESSIONAL EXPERIENCE") or short lines ending in a colon ("Education:"). Sections
are then packed together up to `max_chars`; a single section longer than that is cut at line or space boundaries.
"""
import re

HEADING = re.compile(r"^(?:[A-Z][A-Z0-9 &/,()'.\-]{2,60}|[A-Z][\w &/,()'.\-]{2,60}:)$")
CONTEXT_CHARS = 300


def _by_heading(text: str) -> list[str]:
    sections, current = [], []
    for line in text.splitlines():
        if HEADING.match(line.strip()) and any(part.strip() for part in current):
            sections.append("\n".join(current).strip())
            current = []
        current.append(line)
    if any(part.strip() for part in current):
        sections.append("\n".join(current).strip())
    return sections


def _cut(section: str, max_chars: int) -> list[str]:
    """A section longer than max_chars, cut at the last line break (else space) before the limit."""
    pieces = []
    while len(section) > max_chars:
        cut = section.rfind("\n", max_chars // 2, max_chars)
        cut = cut if cut != -1 else section.rfind(" ", max_chars // 2, max_chars)
        cut = cut if cut != -1 else max_chars
        pieces.append(section[:cut].strip())
        section = section[cut:].strip()
    return pieces + ([section] if section else [])


def split_sections(text: str, max_chars: int) -> list[str]:
    """Document text -> section texts of at most max_chars each (in order, nothing dropped)."""
    packed, buffer = [], ""
    for section in _by_heading(text.strip()):
        for piece in _cut(section, max_chars):
            if buffer and len(buffer) + len(piece) + 2 > max_chars:
                packed.append(buffer)
                buffer = ""
            buffer = f"{buffer}\n\n{piece}" if buffer else piece
    return packed + ([buffer] if buffer else [])


def with_context(sections: list[str], source_label: str) -> list[str]:
    """Every section after the first, prefixed with the start of the document so the subject stays clear."""
    if len(sections) < 2:
        return sections
    start = " ".join(sections[0][:CONTEXT_CHARS].split())
    context = f"(Part of {source_label}. The document begins: \"{start}...\")\n\n"
    return [sections[0]] + [context + section for section in sections[1:]]
