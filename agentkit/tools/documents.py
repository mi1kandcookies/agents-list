"""
agentkit/tools/documents.py - read_document: customer documents as text.

Supports .txt, .md, .csv, .json, .html/.htm and .docx. Word files are read
with the standard library (zipfile + xml.etree): paragraphs become lines,
tabs and breaks are kept, table cells are separated by " | ". No third-party
parsers, so nothing in a document can execute.
"""
from __future__ import annotations

import json
import zipfile
from html.parser import HTMLParser
from pathlib import Path
from xml.etree import ElementTree

from agentkit.errors import ToolError
from agentkit.tools.base import Tool, ToolContext, truncate

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
MAX_DOCX_XML_BYTES = 50_000_000
TEXT_SUFFIXES = {".txt", ".md", ".markdown", ".csv", ".tsv", ".log", ".yaml", ".yml", ".xml", ".sql"}


class _TextExtractor(HTMLParser):
    SKIP = {"script", "style", "noscript", "template", "svg"}
    BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section",
             "article", "header", "footer", "table", "ul", "ol", "pre", "blockquote"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title = ""
        self._skip = 0
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in self.SKIP:
            self._skip += 1
        elif tag == "title":
            self._in_title = True
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self.SKIP and self._skip:
            self._skip -= 1
        elif tag == "title":
            self._in_title = False
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data
        elif not self._skip:
            self.parts.append(data)


def html_to_text(html: str) -> tuple[str, str]:
    """(title, text) of an HTML document; scripts and styles dropped."""
    parser = _TextExtractor()
    parser.feed(html)
    parser.close()
    lines = [" ".join(line.split()) for line in "".join(parser.parts).splitlines()]
    text = "\n".join(line for line in lines if line)
    return " ".join(parser.title.split()), text


def docx_to_text(path: Path) -> str:
    try:
        with zipfile.ZipFile(path) as zf:
            info = zf.getinfo("word/document.xml")
            if info.file_size > MAX_DOCX_XML_BYTES:
                raise ToolError("document body is too large")
            root = ElementTree.fromstring(zf.read(info))
    except (zipfile.BadZipFile, KeyError, ElementTree.ParseError) as exc:
        raise ToolError(f"not a readable .docx file: {exc}") from None
    body = root.find(f"{W}body")
    if body is None:
        return ""
    lines: list[str] = []
    for block in body:
        if block.tag == f"{W}p":
            lines.append(_paragraph(block))
        elif block.tag == f"{W}tbl":
            for row in block.iter(f"{W}tr"):
                cells = [" ".join(_paragraph(p) for p in tc.iter(f"{W}p")).strip()
                         for tc in row.iter(f"{W}tc")]
                lines.append(" | ".join(cells))
    return "\n".join(lines).strip()


def _paragraph(p: ElementTree.Element) -> str:
    out = []
    for el in p.iter():
        if el.tag == f"{W}t":
            out.append(el.text or "")
        elif el.tag == f"{W}tab":
            out.append("\t")
        elif el.tag in (f"{W}br", f"{W}cr"):
            out.append("\n")
        elif el.tag == f"{W}delText":
            continue  # deleted text in tracked changes is not part of the document
    return "".join(out)


def document_text(path: Path) -> str:
    """Plain text of a supported document (used by read_document and ledger tools)."""
    suffix = path.suffix.lower()
    if suffix == ".docx":
        return docx_to_text(path)
    raw = path.read_text(encoding="utf-8", errors="replace")
    if suffix in (".html", ".htm"):
        title, text = html_to_text(raw)
        return f"{title}\n\n{text}" if title else text
    if suffix == ".json":
        try:
            return json.dumps(json.loads(raw), indent=2, ensure_ascii=False)
        except json.JSONDecodeError:
            return raw
    if suffix in TEXT_SUFFIXES or not suffix:
        return raw
    raise ToolError(f"unsupported document type {suffix!r}")


def read_document(args: dict, ctx: ToolContext) -> str:
    path = ctx.path(args["path"])
    if not path.is_file():
        raise ToolError(f"no such file: {args['path']}")
    return truncate(document_text(path), ctx.max_output_chars)


TOOLS = [
    Tool("read_document", "Read a document (.txt, .md, .csv, .json, .html, .docx) as plain text.",
         {"type": "object", "required": ["path"], "properties": {
             "path": {"type": "string", "description": "Workspace-relative path, e.g. inputs/msa.docx"}}},
         read_document, risk="read", untrusted_output=True),
]
