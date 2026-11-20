"""Экспорт заметки: Word (.docx) — собственный небольшой конвертер Markdown → DOCX без сторонних
библиотек; PDF — печать HTML браузером на движке Chromium (Edge на Windows; Chrome, Edge или
Яндекс Браузер на Mac) без окна. Если такого браузера нет, страница открывается для печати вручную."""

import html as htmllib
import os
import re
import subprocess
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path
from xml.sax.saxutils import escape

from . import osutil


def downloads_dir() -> Path:
    d = Path.home() / "Downloads"
    return d if d.is_dir() else Path.home()


def safe_name(title: str) -> str:
    name = re.sub(r'[\\/:*?"<>|\n\r\t]+', " ", title).strip(" .")[:120]
    return name or "Встреча"


def unique_path(folder: Path, name: str, ext: str) -> Path:
    p = folder / f"{name}{ext}"
    n = 2
    while p.exists():
        p = folder / f"{name} ({n}){ext}"
        n += 1
    return p


# ---------- Markdown → DOCX ----------

_INLINE = re.compile(r"(\*\*[^*]+\*\*|__[^_]+__|\*[^*\s][^*]*\*|`[^`]+`|\[[^\]]+\]\([^)]+\))")


def _runs(text: str) -> str:
    """Жирный, курсив, код и ссылки внутри строки → набор <w:r>."""
    out = []
    for part in _INLINE.split(text):
        if not part:
            continue
        props = ""
        if part.startswith(("**", "__")) and len(part) > 4:
            part, props = part[2:-2], "<w:b/>"
        elif part.startswith("*") and part.endswith("*") and len(part) > 2:
            part, props = part[1:-1], "<w:i/>"
        elif part.startswith("`") and part.endswith("`"):
            part, props = part[1:-1], '<w:rFonts w:ascii="Menlo" w:hAnsi="Menlo"/>'
        elif m := re.fullmatch(r"\[([^\]]+)\]\(([^)]+)\)", part):
            part, props = f"{m.group(1)} ({m.group(2)})", ""
        rpr = f"<w:rPr>{props}</w:rPr>" if props else ""
        out.append(f'<w:r>{rpr}<w:t xml:space="preserve">{escape(part)}</w:t></w:r>')
    return "".join(out)


def _p(text: str, style: str | None = None, extra_ppr: str = "") -> str:
    ppr = (f'<w:pStyle w:val="{style}"/>' if style else "") + extra_ppr
    return f"<w:p>{f'<w:pPr>{ppr}</w:pPr>' if ppr else ''}{_runs(text)}</w:p>"


def _table(rows: list[list[str]]) -> str:
    cols = max(len(r) for r in rows)
    width = 9000 // max(cols, 1)
    grid = "".join(f'<w:gridCol w:w="{width}"/>' for _ in range(cols))
    body = []
    for ri, row in enumerate(rows):
        cells = []
        for ci in range(cols):
            text = row[ci] if ci < len(row) else ""
            if ri == 0:
                text = f"**{text}**" if text and not text.startswith("**") else text
            cells.append(f'<w:tc><w:tcPr><w:tcW w:w="{width}" w:type="dxa"/></w:tcPr>{_p(text)}</w:tc>')
        body.append(f"<w:tr>{''.join(cells)}</w:tr>")
    return (f'<w:tbl><w:tblPr><w:tblStyle w:val="TableGrid"/><w:tblW w:w="0" w:type="auto"/></w:tblPr>'
            f"<w:tblGrid>{grid}</w:tblGrid>{''.join(body)}</w:tbl>{_p('')}")


def markdown_to_body(md: str) -> str:
    lines = md.replace("\r\n", "\n").split("\n")
    out, i = [], 0
    while i < len(lines):
        line = lines[i].rstrip()
        s = line.strip()
        if not s:
            i += 1
            continue
        if s.startswith("|"):
            rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                if not all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c):
                    rows.append(cells)
                i += 1
            if rows:
                out.append(_table(rows))
            continue
        if m := re.match(r"^(#{1,6})\s+(.*)$", s):
            level = min(len(m.group(1)), 3)
            out.append(_p(m.group(2), f"Heading{level}"))
        elif re.fullmatch(r"(-{3,}|\*{3,}|_{3,})", s):
            out.append('<w:p><w:pPr><w:pBdr><w:bottom w:val="single" w:sz="6" w:space="1" w:color="999999"/>'
                       "</w:pBdr></w:pPr></w:p>")
        elif m := re.match(r"^(\s*)[-*+]\s+(?:\[( |x|X)\]\s+)?(.*)$", line):
            depth = min(len(m.group(1).replace("\t", "  ")) // 2, 3)
            box = {" ": "☐ ", "x": "☑ ", "X": "☑ "}.get(m.group(2) or "", "")
            ind = f'<w:ind w:left="{360 + depth * 360}" w:hanging="240"/>'
            out.append(_p(("• " if not box else "") + box + m.group(3), None, ind))
        elif m := re.match(r"^(\s*)(\d+)[.)]\s+(.*)$", line):
            depth = min(len(m.group(1)) // 2, 3)
            out.append(_p(f"{m.group(2)}. {m.group(3)}", None, f'<w:ind w:left="{360 + depth * 360}" w:hanging="300"/>'))
        elif s.startswith(">"):
            out.append(_p(s.lstrip("> "), "Quote"))
        else:
            out.append(_p(s))
        i += 1
    return "".join(out)


_STYLES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
<w:docDefaults><w:rPrDefault><w:rPr><w:rFonts w:ascii="Calibri" w:hAnsi="Calibri" w:cs="Calibri" w:eastAsia="Calibri"/>
<w:sz w:val="22"/><w:lang w:val="ru-RU"/></w:rPr></w:rPrDefault>
<w:pPrDefault><w:pPr><w:spacing w:after="120" w:line="276" w:lineRule="auto"/></w:pPr></w:pPrDefault></w:docDefaults>
<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/></w:style>
<w:style w:type="paragraph" w:styleId="Title"><w:name w:val="Title"/><w:basedOn w:val="Normal"/>
<w:pPr><w:spacing w:after="80"/></w:pPr><w:rPr><w:b/><w:sz w:val="40"/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="Subtitle"><w:name w:val="Subtitle"/><w:basedOn w:val="Normal"/>
<w:rPr><w:color w:val="666666"/><w:sz w:val="20"/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="heading 1"/><w:basedOn w:val="Normal"/>
<w:pPr><w:keepNext/><w:spacing w:before="320" w:after="120"/><w:outlineLvl w:val="0"/></w:pPr><w:rPr><w:b/><w:sz w:val="32"/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="Heading2"><w:name w:val="heading 2"/><w:basedOn w:val="Normal"/>
<w:pPr><w:keepNext/><w:spacing w:before="280" w:after="100"/><w:outlineLvl w:val="1"/></w:pPr><w:rPr><w:b/><w:sz w:val="28"/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="Heading3"><w:name w:val="heading 3"/><w:basedOn w:val="Normal"/>
<w:pPr><w:keepNext/><w:spacing w:before="200" w:after="80"/><w:outlineLvl w:val="2"/></w:pPr><w:rPr><w:b/><w:sz w:val="24"/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="Quote"><w:name w:val="Quote"/><w:basedOn w:val="Normal"/>
<w:pPr><w:ind w:left="360"/></w:pPr><w:rPr><w:i/><w:color w:val="555555"/></w:rPr></w:style>
<w:style w:type="table" w:styleId="TableGrid"><w:name w:val="Table Grid"/><w:tblPr><w:tblBorders>
<w:top w:val="single" w:sz="4" w:space="0" w:color="999999"/><w:left w:val="single" w:sz="4" w:space="0" w:color="999999"/>
<w:bottom w:val="single" w:sz="4" w:space="0" w:color="999999"/><w:right w:val="single" w:sz="4" w:space="0" w:color="999999"/>
<w:insideH w:val="single" w:sz="4" w:space="0" w:color="999999"/><w:insideV w:val="single" w:sz="4" w:space="0" w:color="999999"/>
</w:tblBorders><w:tblCellMar><w:left w:w="80" w:type="dxa"/><w:right w:w="80" w:type="dxa"/></w:tblCellMar></w:tblPr></w:style>
</w:styles>"""


def to_docx(path: Path, title: str, subtitle: str, md: str) -> Path:
    body = _p(title, "Title") + (_p(subtitle, "Subtitle") if subtitle else "") + markdown_to_body(md)
    doc = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
           '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
           f'<w:body>{body}<w:sectPr><w:pgSz w:w="11906" w:h="16838"/>'
           '<w:pgMar w:top="1134" w:right="1134" w:bottom="1134" w:left="1134" w:header="709" w:footer="709" w:gutter="0"/>'
           "</w:sectPr></w:body></w:document>")
    now = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml",
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                   '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                   '<Default Extension="xml" ContentType="application/xml"/>'
                   '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
                   '<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>'
                   '<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>'
                   "</Types>")
        z.writestr("_rels/.rels",
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
                   '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>'
                   "</Relationships>")
        z.writestr("word/_rels/document.xml.rels",
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
                   "</Relationships>")
        z.writestr("word/document.xml", doc)
        z.writestr("word/styles.xml", _STYLES)
        z.writestr("docProps/core.xml",
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
                   'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" '
                   'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
                   f"<dc:title>{escape(title)}</dc:title><dc:creator>Meeting Agent</dc:creator>"
                   f'<dcterms:created xsi:type="dcterms:W3CDTF">{now}</dcterms:created></cp:coreProperties>')
    return path


# ---------- PDF через Chromium без окна ----------

def _chromium() -> str | None:
    if osutil.IS_WIN:
        roots = [os.environ.get(k, "") for k in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA")]
        rels = [r"Microsoft\Edge\Application\msedge.exe", r"Google\Chrome\Application\chrome.exe",
                r"Yandex\YandexBrowser\Application\browser.exe"]
        cands = [Path(r) / rel for r in roots if r for rel in rels]
    else:
        cands = [Path(p) for p in ("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                                   "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
                                   "/Applications/Yandex.app/Contents/MacOS/Yandex",
                                   "/Applications/Chromium.app/Contents/MacOS/Chromium")]
    return next((str(p) for p in cands if p.exists()), None)


PRINT_CSS = """
body{font-family:-apple-system,"Segoe UI",Calibri,Arial,sans-serif;font-size:11pt;line-height:1.45;color:#111;margin:0}
h1{font-size:20pt;margin:0 0 4pt}h2{font-size:15pt;margin:16pt 0 6pt}h3{font-size:12.5pt;margin:12pt 0 4pt}
.sub{color:#666;font-size:9.5pt;margin-bottom:14pt}
table{border-collapse:collapse;width:100%;margin:6pt 0}th,td{border:1px solid #999;padding:4pt 6pt;text-align:left;vertical-align:top}
th{background:#f0f0f0}blockquote{margin:6pt 0 6pt 12pt;color:#555;font-style:italic}code{font-family:Menlo,Consolas,monospace}
.utt{margin:0 0 5pt}.ts{color:#888;font-size:9pt;margin-right:6pt}.who{font-weight:600;margin-right:4pt}
@page{size:A4;margin:18mm 16mm}
"""


def print_html(title: str, body_html: str) -> str:
    return (f'<!doctype html><html lang="ru"><head><meta charset="utf-8"><title>{htmllib.escape(title)}</title>'
            f"<style>{PRINT_CSS}</style></head><body>{body_html}</body></html>")


def to_pdf(path: Path, html_text: str) -> Path | None:
    """PDF или None, если подходящего браузера нет (тогда открываем HTML для печати вручную)."""
    exe = _chromium()
    if not exe:
        return None
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "note.html"
        src.write_text(html_text, encoding="utf-8")
        cmd = [exe, "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
               f"--user-data-dir={Path(tmp) / 'profile'}", "--no-pdf-header-footer",
               f"--print-to-pdf={path}", src.as_uri()]
        try:
            subprocess.run(cmd, capture_output=True, timeout=90, **osutil.popen_kwargs())
        except (OSError, subprocess.TimeoutExpired):
            return None
    return path if path.exists() and path.stat().st_size > 0 else None
