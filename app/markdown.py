"""Tiny, safe Markdown subset for KB articles: headings, lists, bold, italic, code, paragraphs."""

import html
import re

from markupsafe import Markup


def _inline(text: str) -> str:
    text = html.escape(text)
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"\*([^*]+)\*", r"<em>\1</em>", text)
    return text


def render(source: str) -> Markup:
    out, para, list_tag = [], [], None

    def flush_para():
        if para:
            out.append(f"<p>{_inline(' '.join(para))}</p>")
            para.clear()

    def close_list():
        nonlocal list_tag
        if list_tag:
            out.append(f"</{list_tag}>")
            list_tag = None

    for line in (source or "").splitlines():
        stripped = line.strip()
        heading = re.match(r"(#{1,3})\s+(.*)", stripped)
        bullet = re.match(r"[-*]\s+(.*)", stripped)
        numbered = re.match(r"\d+\.\s+(.*)", stripped)
        if heading:
            flush_para()
            close_list()
            level = len(heading.group(1)) + 1  # article title is the page's h1
            out.append(f"<h{level}>{_inline(heading.group(2))}</h{level}>")
        elif bullet or numbered:
            flush_para()
            tag = "ul" if bullet else "ol"
            if list_tag != tag:
                close_list()
                out.append(f"<{tag}>")
                list_tag = tag
            out.append(f"<li>{_inline((bullet or numbered).group(1))}</li>")
        elif not stripped:
            flush_para()
            close_list()
        else:
            close_list()
            para.append(stripped)
    flush_para()
    close_list()
    return Markup("\n".join(out))
