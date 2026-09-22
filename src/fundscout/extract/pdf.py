"""PDF text extraction."""

import io

import pdfplumber


def pdf_to_text(content: bytes) -> str:
    """Text of every page, with page markers so the LLM can cite locations."""
    pages: list[str] = []
    with pdfplumber.open(io.BytesIO(content)) as pdf:
        for number, page in enumerate(pdf.pages, start=1):
            text = (page.extract_text() or "").strip()
            if text:
                pages.append(f"[Page {number}]\n{text}")
    return "\n\n".join(pages)
