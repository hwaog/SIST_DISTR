"""Conversor mínimo Markdown → Word (.docx) para el informe técnico.

Soporta: encabezados, párrafos con **negrita**/*cursiva*/`código`, listas (con anidación), listas numeradas,
tablas, bloques de código, imágenes con pie, citas, y directivas ``{{include ruta.md [shift=N] [skip_h1]}}``.
Los bloques ```mermaid se sustituyen por la figura PNG indicada en ``mermaid_figs`` (en orden de aparición).
"""

from __future__ import annotations

import re
from pathlib import Path

from docx import Document
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor

INK = RGBColor(0x0F, 0x17, 0x2A)
ACCENT = RGBColor(0x1D, 0x4E, 0xD8)
GREY = RGBColor(0x47, 0x55, 0x69)
BODY_FONT = "Calibri"
CODE_FONT = "Consolas"


def _shade(el, fill: str) -> None:
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), fill)
    el.append(shd)


def _cell_shade(cell, fill: str) -> None:
    _shade(cell._tc.get_or_add_tcPr(), fill)


def _para_shade(p, fill: str) -> None:
    _shade(p._p.get_or_add_pPr(), fill)


def _borders(p, color="CBD5E1") -> None:
    pPr = p._p.get_or_add_pPr()
    b = OxmlElement("w:pBdr")
    for side in ("top", "left", "bottom", "right"):
        e = OxmlElement(f"w:{side}")
        e.set(qn("w:val"), "single")
        e.set(qn("w:sz"), "4")
        e.set(qn("w:space"), "4")
        e.set(qn("w:color"), color)
        b.append(e)
    pPr.append(b)


def _add_field(run, instr: str) -> None:
    for t, text in (("begin", None), (None, instr), ("separate", None), ("end", None)):
        if t:
            fc = OxmlElement("w:fldChar")
            fc.set(qn("w:fldCharType"), t)
            run._r.append(fc)
        else:
            it = OxmlElement("w:instrText")
            it.set(qn("xml:space"), "preserve")
            it.text = text
            run._r.append(it)


INLINE = re.compile(r"(\*\*.+?\*\*|`[^`]+`|(?<![*\w])\*[^*\s][^*]*?\*(?![*\w]))")
LINK = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")


def _links(text: str) -> str:
    """[texto](url) -> 'texto (url)' si es externo; sólo 'texto' si es ruta relativa."""
    return LINK.sub(lambda m: f"{m.group(1)} ({m.group(2)})" if m.group(2).startswith("http") else m.group(1), text)


def add_inline(p, text: str, *, size=None, bold=False, italic=False, color=None) -> None:
    text = _links(text.replace("<br>", "\n"))
    for part in INLINE.split(text):
        if not part:
            continue
        if part.startswith("**") and part.endswith("**") and len(part) > 4:
            add_inline(p, part[2:-2], size=size, bold=True, italic=italic, color=color)  # permite `código` dentro de negrita
            continue
        if part.startswith("`") and part.endswith("`") and len(part) > 2:
            r = p.add_run(part[1:-1])
            r.font.name = CODE_FONT
            r._r.get_or_add_rPr().rFonts.set(qn("w:eastAsia"), CODE_FONT)
            r.font.size = Pt((size or 10.5) - 1)
            r.font.color.rgb = RGBColor(0xFF, 0xFF, 0xFF) if color == RGBColor(0xFF, 0xFF, 0xFF) else RGBColor(0x9F, 0x12, 0x39)
            r.bold = bold
            _shade(r._r.get_or_add_rPr(), "F1F5F9" if color != RGBColor(0xFF, 0xFF, 0xFF) else "1E40AF")
            continue
        if part.startswith("*") and part.endswith("*") and len(part) > 2:
            r = p.add_run(part[1:-1])
            r.italic = True
            r.bold = bold
        else:
            r = p.add_run(part)
            r.bold = bold
            r.italic = italic
        if size:
            r.font.size = Pt(size)
        if color is not None:
            r.font.color.rgb = color


class Builder:
    def __init__(self, base: Path, mermaid_figs: dict[str, list[str]] | None = None) -> None:
        self.base = base
        self.doc = Document()
        self.fig_n = 0
        self.tab_n = 0
        self.mermaid_figs = {k: list(v) for k, v in (mermaid_figs or {}).items()}
        self._setup()

    # ---------------------------------------------------------------- estilos
    def _setup(self) -> None:
        d = self.doc
        sec = d.sections[0]
        sec.page_width, sec.page_height = Cm(21.59), Cm(27.94)
        sec.left_margin = sec.right_margin = Cm(2.2)
        sec.top_margin, sec.bottom_margin = Cm(2.2), Cm(2.0)
        st = d.styles["Normal"]
        st.font.name = BODY_FONT
        st.font.size = Pt(10.5)
        st.element.rPr.rFonts.set(qn("w:eastAsia"), BODY_FONT)
        st.paragraph_format.space_after = Pt(5)
        st.paragraph_format.line_spacing = 1.12
        for lvl, (sz, col) in {1: (18, ACCENT), 2: (14, ACCENT), 3: (12, INK), 4: (11, GREY)}.items():
            h = d.styles[f"Heading {lvl}"]
            h.font.name = BODY_FONT
            h.font.size = Pt(sz)
            h.font.bold = True
            h.font.color.rgb = col
            h.element.rPr.rFonts.set(qn("w:eastAsia"), BODY_FONT)
            h.paragraph_format.space_before = Pt(16 if lvl == 1 else 11)
            h.paragraph_format.space_after = Pt(6)
            h.paragraph_format.keep_with_next = True
        for name in ("List Bullet", "List Bullet 2", "List Number"):
            d.styles[name].font.name = BODY_FONT
            d.styles[name].paragraph_format.space_after = Pt(2)
        # pie de página con número
        f = sec.footer.paragraphs[0]
        f.alignment = WD_ALIGN_PARAGRAPH.CENTER
        r = f.add_run("DFSha — Informe técnico · página ")
        r.font.size = Pt(8.5)
        r.font.color.rgb = GREY
        r2 = f.add_run()
        r2.font.size = Pt(8.5)
        r2.font.color.rgb = GREY
        _add_field(r2, "PAGE")
        # actualizar campos (índice) al abrir
        settings = d.settings.element
        u = OxmlElement("w:updateFields")
        u.set(qn("w:val"), "true")
        settings.append(u)

    # ----------------------------------------------------------------- portada
    def cover(self, title: str, subtitle: str, lines: list[str]) -> None:
        d = self.doc
        for _ in range(5):
            d.add_paragraph()
        p = d.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.LEFT
        r = p.add_run(title)
        r.font.size = Pt(34)
        r.bold = True
        r.font.color.rgb = ACCENT
        p = d.add_paragraph()
        r = p.add_run(subtitle)
        r.font.size = Pt(15)
        r.font.color.rgb = INK
        d.add_paragraph()
        for ln in lines:
            p = d.add_paragraph()
            add_inline(p, ln, size=11.5, color=GREY)
        d.add_page_break()

    def toc(self) -> None:
        d = self.doc
        h = d.add_paragraph()
        r = h.add_run("Contenido")
        r.font.size = Pt(18)
        r.bold = True
        r.font.color.rgb = ACCENT
        p = d.add_paragraph()
        _add_field(p.add_run(), r'TOC \o "1-2" \h \z \u')
        note = d.add_paragraph()
        rr = note.add_run("(Si el índice aparece vacío, pulse F9 / «Actualizar campo» en Word.)")
        rr.italic = True
        rr.font.size = Pt(8.5)
        rr.font.color.rgb = GREY
        d.add_page_break()

    # --------------------------------------------------------------- elementos
    def heading(self, text: str, level: int) -> None:
        self.doc.add_heading(text.strip(), level=min(max(level, 1), 4))

    def para(self, text: str, **kw) -> None:
        p = self.doc.add_paragraph()
        add_inline(p, text, **kw)

    def bullet(self, text: str, level: int = 0) -> None:
        p = self.doc.add_paragraph(style="List Bullet" if level == 0 else "List Bullet 2")
        add_inline(p, text)

    def number(self, text: str, n: int) -> None:
        # numeración manual: el estilo "List Number" de Word continuaría la cuenta entre listas distintas
        p = self.doc.add_paragraph()
        p.paragraph_format.left_indent = Cm(0.95)
        p.paragraph_format.first_line_indent = Cm(-0.6)
        p.paragraph_format.space_after = Pt(2)
        add_inline(p, f"{n}. " + text)

    def code(self, lines: list[str]) -> None:
        for ln in lines or [""]:
            p = self.doc.add_paragraph()
            p.paragraph_format.space_after = Pt(0)
            p.paragraph_format.line_spacing = 1.0
            p.paragraph_format.left_indent = Cm(0.3)
            r = p.add_run(ln if ln else " ")
            r.font.name = CODE_FONT
            r._r.get_or_add_rPr().rFonts.set(qn("w:eastAsia"), CODE_FONT)
            r.font.size = Pt(8)
            _para_shade(p, "F1F5F9")
        self.doc.add_paragraph().paragraph_format.space_after = Pt(2)

    def quote(self, text: str) -> None:
        p = self.doc.add_paragraph()
        p.paragraph_format.left_indent = Cm(0.6)
        add_inline(p, text, italic=True, color=GREY)
        _para_shade(p, "F8FAFC")

    def image(self, path: Path, caption: str = "", width_cm: float = 16.2) -> None:
        if not path.exists():
            self.para(f"[figura no disponible: {path.name}]", italic=True, color=GREY)
            return
        self.fig_n += 1
        p = self.doc.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        p.paragraph_format.keep_with_next = True
        p.add_run().add_picture(str(path), width=Cm(width_cm))
        c = self.doc.add_paragraph()
        c.alignment = WD_ALIGN_PARAGRAPH.CENTER
        r = c.add_run(f"Figura {self.fig_n}. {caption}" if caption else f"Figura {self.fig_n}.")
        r.italic = True
        r.font.size = Pt(9)
        r.font.color.rgb = GREY

    def table(self, rows: list[list[str]]) -> None:
        if not rows:
            return
        ncol = max(len(r) for r in rows)
        t = self.doc.add_table(rows=len(rows), cols=ncol)
        t.style = "Table Grid"
        t.alignment = WD_TABLE_ALIGNMENT.CENTER
        for i, row in enumerate(rows):
            for j in range(ncol):
                cell = t.cell(i, j)
                cell.text = ""
                p = cell.paragraphs[0]
                p.paragraph_format.space_after = Pt(1)
                p.paragraph_format.line_spacing = 1.0
                txt = row[j] if j < len(row) else ""
                add_inline(p, txt, size=8.6, bold=(i == 0), color=RGBColor(0xFF, 0xFF, 0xFF) if i == 0 else None)
                if i == 0:
                    _cell_shade(cell, "1D4ED8")
                elif i % 2 == 0:
                    _cell_shade(cell, "F1F5F9")
        # evitar que una fila se parta entre páginas
        for row in t.rows:
            trPr = row._tr.get_or_add_trPr()
            cs = OxmlElement("w:cantSplit")
            trPr.append(cs)
        self.doc.add_paragraph().paragraph_format.space_after = Pt(3)

    def page_break(self) -> None:
        self.doc.add_page_break()

    # ----------------------------------------------------------------- markdown
    def markdown(self, text: str, *, shift: int = 0, skip_h1: bool = False, src: Path | None = None) -> None:
        lines = text.splitlines()
        i, n = 0, len(lines)
        first_h1_skipped = not skip_h1
        while i < n:
            ln = lines[i]
            st = ln.strip()
            m = re.match(r"\{\{include\s+(\S+)(.*)\}\}", st)
            if m:
                opts = m.group(2)
                sh = re.search(r"shift=(-?\d+)", opts)
                path = (self.base / m.group(1)).resolve()
                self.markdown(path.read_text(), shift=int(sh.group(1)) if sh else shift, skip_h1="skip_h1" in opts, src=path)
                i += 1
                continue
            if st.startswith("```"):
                lang = st[3:].strip()
                buf = []
                i += 1
                while i < n and not lines[i].strip().startswith("```"):
                    buf.append(lines[i])
                    i += 1
                i += 1
                if lang == "mermaid":
                    key = src.name if src else ""
                    figs = self.mermaid_figs.get(key, [])
                    if figs:
                        path, cap = figs.pop(0)
                        self.image(self.base / path, cap)
                else:
                    self.code(buf)
                continue
            m = re.match(r"^(#{1,6})\s+(.*)$", ln)
            if m:
                level = len(m.group(1))
                if level == 1 and not first_h1_skipped:
                    first_h1_skipped = True
                    i += 1
                    continue
                self.heading(re.sub(r"[`*]", "", m.group(2)), level + shift)
                i += 1
                continue
            m = re.match(r"^!\[(.*?)\]\((.*?)\)\s*$", st)
            if m:
                self.image(self.base / m.group(2), m.group(1))
                i += 1
                continue
            if st.startswith("|") and i + 1 < n and re.match(r"^\|?\s*:?-{2,}", lines[i + 1].strip()):
                rows = []
                while i < n and lines[i].strip().startswith("|"):
                    raw = lines[i].strip()
                    raw = raw[1:] if raw.startswith("|") else raw
                    raw = raw[:-1] if raw.endswith("|") and not raw.endswith("\\|") else raw
                    cells = [c.strip().replace("\\|", "|") for c in re.split(r"(?<!\\)\|", raw)]
                    if not all(re.match(r"^:?-{2,}:?$", c) for c in cells if c):
                        rows.append(cells)
                    i += 1
                self.table(rows)
                continue
            m = re.match(r"^(\s*)([*-])\s+(.*)$", ln)
            if m:
                text = m.group(3)
                i += 1
                while i < n and lines[i].strip() and re.match(r"^\s{2,}\S", lines[i]) and not re.match(r"^\s*([*-]|\d+[.)])\s", lines[i]):
                    text += " " + lines[i].strip()
                    i += 1
                self.bullet(text, level=1 if len(m.group(1)) >= 2 else 0)
                continue
            m = re.match(r"^\s*(\d+)[.)]\s+(.*)$", ln)
            if m:
                text, num = m.group(2), int(m.group(1))
                i += 1
                while i < n and lines[i].strip() and re.match(r"^\s{2,}\S", lines[i]) and not re.match(r"^\s*([*-]|\d+[.)])\s", lines[i]):
                    text += " " + lines[i].strip()
                    i += 1
                self.number(text, num)
                continue
            if st.startswith(">"):
                self.quote(st.lstrip("> ").strip())
                i += 1
                continue
            if st in ("---", "***"):
                i += 1
                continue
            if not st or st.startswith("<!--"):
                i += 1
                continue
            # párrafo (une líneas contiguas)
            buf = [st]
            i += 1
            while i < n and lines[i].strip() and not re.match(r"^(\s*([*-]|\d+[.)])\s|#|\||```|>|!\[|\{\{)", lines[i]):
                buf.append(lines[i].strip())
                i += 1
            self.para(" ".join(buf))

    def save(self, path: Path) -> None:
        self.doc.save(str(path))
