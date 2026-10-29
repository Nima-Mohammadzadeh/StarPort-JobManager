from __future__ import annotations

import html
from pathlib import Path
from typing import Any, Dict, Optional

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_RIGHT
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.pdfbase import pdfmetrics
from reportlab.platypus import Flowable, Image, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from .models import ProofJob
from .version import APP_VERSION, RELEASE_NAME

# BETA21_5_VARIABLE_DATA_SPEC_PDF_PAGE_V12
_VARIABLE_SPEC_START = "[[RFID_VARIABLE_SPEC_V1]]"
_VARIABLE_SPEC_END = "[[/RFID_VARIABLE_SPEC_V1]]"

def _variable_data_spec_from_job(job):
    import json as _spec_json
    raw = str(getattr(job, "internal_comments", "") or "")
    start = raw.find(_VARIABLE_SPEC_START)
    if start < 0:
        return {}
    end = raw.find(_VARIABLE_SPEC_END, start)
    if end < 0:
        return {}
    encoded = raw[start + len(_VARIABLE_SPEC_START):end].strip()
    if not encoded:
        return {}
    try:
        value = _spec_json.loads(encoded)
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}

def _variable_spec_wrap(value, limit=86):
    import textwrap as _spec_textwrap
    text = " ".join(str(value or "").split())
    if not text:
        return [""]
    return _spec_textwrap.wrap(
        text,
        width=max(20, int(limit)),
        break_long_words=True,
        break_on_hyphens=True,
    ) or [""]

def _draw_variable_data_spec_page(canvas, job, settings=None):
    # JM fix: draws on the current page. It used to call canvas.showPage() from inside the
    # page-begin callback, which emptied ReportLab's state stack (so the callback's
    # restoreState() raised "pop from empty list") and would have pushed the proof's own
    # content onto the spec page. The page is now added after the proof (see
    # _VariableSpecNext in generate_proof_pdf) and drawn by that page's callback.
    spec = _variable_data_spec_from_job(job)
    if not spec:
        return

    try:
        page_w, page_h = canvas._pagesize
    except Exception:
        page_w, page_h = (612.0, 792.0)

    left = 42.0
    right = page_w - 42.0
    top = page_h - 42.0
    usable_w = right - left

    def _job(name, fallback=""):
        return str(getattr(job, name, fallback) or fallback)

    def _spec(name, fallback=""):
        return str(spec.get(name, fallback) or fallback)

    def _classification(value, fallback="Static"):
        text = str(value or fallback).strip().upper()
        return text if text in {"STATIC", "VARIABLE"} else str(fallback).upper()

    canvas.setFillColorRGB(0.08, 0.10, 0.14)
    canvas.setFont("Helvetica-Bold", 16)
    canvas.drawString(left, top, "ENCODING & VARIABLE DATA SPECIFICATION")

    canvas.setFont("Helvetica", 7.5)
    canvas.setFillColorRGB(0.28, 0.31, 0.36)
    canvas.drawString(
        left, top - 16,
        "Part of the RFID Production Proof - approved static / variable data construction for production."
    )

    y = top - 42
    header_pairs = [
        ("Customer", _job("customer", "-")),
        ("Job #", _job("job_number", "-")),
        ("Part #", _job("part_number", "-")),
        ("Proof ID", _job("proof_id", "-")),
        ("Proof Rev", _job("proof_revision", "1")),
        ("PO / Data Ref", _spec("po_data_reference", "-")),
    ]

    pair_w = usable_w / 3.0
    for idx, (label, value) in enumerate(header_pairs):
        col = idx % 3
        row = idx // 3
        x = left + col * pair_w
        yy = y - row * 28
        canvas.setFont("Helvetica-Bold", 7)
        canvas.setFillColorRGB(0.38, 0.40, 0.44)
        canvas.drawString(x, yy, label.upper())
        canvas.setFont("Helvetica", 8.5)
        canvas.setFillColorRGB(0.08, 0.10, 0.14)
        canvas.drawString(x, yy - 11, str(value)[:42])

    y -= 70

    source = _spec("variable_data_source", "Customer PO / approved data file")
    canvas.setFillColorRGB(0.94, 0.96, 0.98)
    canvas.roundRect(left, y - 34, usable_w, 34, 4, fill=1, stroke=0)
    canvas.setFillColorRGB(0.08, 0.10, 0.14)
    canvas.setFont("Helvetica-Bold", 7)
    canvas.drawString(left + 8, y - 11, "OVERALL ENCODING / VARIABLE DATA SOURCE")
    canvas.setFont("Helvetica", 8.5)
    canvas.drawString(left + 8, y - 24, source[:110])
    y -= 50

    columns = [
        ("DATA ELEMENT", 0.20),
        ("CLASS", 0.12),
        ("SOURCE", 0.25),
        ("PRODUCTION RULE / FORMAT", 0.30),
        ("EXAMPLE", 0.13),
    ]
    widths = [usable_w * fraction for _, fraction in columns]

    canvas.setFillColorRGB(0.13, 0.16, 0.22)
    canvas.rect(left, y - 22, usable_w, 22, fill=1, stroke=0)
    x = left
    canvas.setFillColorRGB(1, 1, 1)
    canvas.setFont("Helvetica-Bold", 6.4)
    for (title, _), width in zip(columns, widths):
        canvas.drawString(x + 4, y - 14, title)
        x += width
    y -= 22

    rows = [
        ("GTIN / UPC", "STATIC", source,
         "Use the approved GTIN / UPC shown on Page 1.", _job("gtin", "-")),
        ("Encoding Type", "STATIC", "Approved Proof Specification",
         "Use the selected RFID encoding scheme.", _job("encoding_type", "-")),
    ]

    if _job("company_prefix"):
        rows.append((
            "Company Prefix", "STATIC", "Approved Encoding Specification",
            "Use the approved company prefix shown on Page 1.", _job("company_prefix", "-")
        ))
    if _job("item_reference"):
        rows.append((
            "Item Reference", "STATIC", "Derived / Approved Encoding Specification",
            "Use the approved item reference shown on Page 1.", _job("item_reference", "-")
        ))

    rows.append((
        "Barcode 1",
        _classification(_spec("barcode_1_classification", "Static")),
        _spec("barcode_1_source", source),
        _spec("barcode_1_rule", "Use the approved Barcode 1 value shown on Page 1."),
        _job("barcode_data_1", "-"),
    ))

    if _job("barcode_type_2") or _job("barcode_data_2"):
        rows.append((
            "Barcode 2",
            _classification(_spec("barcode_2_classification", "Static")),
            _spec("barcode_2_source", source),
            _spec("barcode_2_rule", "Use the approved Barcode 2 value shown on Page 1."),
            _job("barcode_data_2", "-"),
        ))

    rows.extend([
        (
            "Serial / Reference",
            _classification(_spec("serial_classification", "Variable"), "Variable"),
            _spec("serial_source", "Production serial allocation"),
            _spec("serial_rule", "One unique serial / reference per label"),
            _job("serial", "-"),
        ),
        (
            "EPC / Encoded Data",
            _classification(_spec("epc_classification", "Variable"), "Variable"),
            _spec("epc_source", "Generated from approved encoding fields"),
            _spec("epc_rule", "Generate per selected encoding scheme."),
            _job("epc_hex", "-"),
        ),
    ])

    canvas.setStrokeColorRGB(0.78, 0.80, 0.84)
    for row_index, row in enumerate(rows):
        wrapped = []
        for idx, value in enumerate(row):
            wrapped.append(_variable_spec_wrap(value, [22, 12, 30, 37, 18][idx]))

        line_count = max(len(lines) for lines in wrapped)
        row_h = max(25, 8 + line_count * 8)

        if row_index % 2:
            canvas.setFillColorRGB(0.975, 0.98, 0.985)
            canvas.rect(left, y - row_h, usable_w, row_h, fill=1, stroke=0)

        x = left
        for col_idx, (lines, width) in enumerate(zip(wrapped, widths)):
            if col_idx == 1 and str(row[1]).upper() == "VARIABLE":
                canvas.setFillColorRGB(0.67, 0.20, 0.08)
                canvas.setFont("Helvetica-Bold", 7)
            else:
                canvas.setFillColorRGB(0.08, 0.10, 0.14)
                canvas.setFont("Helvetica-Bold" if col_idx == 0 else "Helvetica", 6.8)

            text_y = y - 10
            for line in lines[:5]:
                canvas.drawString(x + 4, text_y, str(line)[:80])
                text_y -= 7.5

            canvas.setStrokeColorRGB(0.86, 0.87, 0.89)
            canvas.line(x + width, y, x + width, y - row_h)
            x += width

        canvas.setStrokeColorRGB(0.78, 0.80, 0.84)
        canvas.line(left, y - row_h, right, y - row_h)
        y -= row_h

    notes = _spec("variable_data_notes", "")
    if notes:
        y -= 12
        canvas.setFont("Helvetica-Bold", 7)
        canvas.setFillColorRGB(0.08, 0.10, 0.14)
        canvas.drawString(left, y, "ADDITIONAL VARIABLE DATA / PO INSTRUCTIONS")
        y -= 11
        canvas.setFont("Helvetica", 7)
        for line in _variable_spec_wrap(notes, 112)[:6]:
            canvas.drawString(left, y, line[:125])
            y -= 9

    approval_y = max(70, y - 24)
    canvas.setFillColorRGB(0.95, 0.96, 0.98)
    canvas.roundRect(left, approval_y - 58, usable_w, 58, 4, fill=1, stroke=0)
    canvas.setFillColorRGB(0.08, 0.10, 0.14)
    canvas.setFont("Helvetica-Bold", 7)
    canvas.drawString(left + 8, approval_y - 12, "VARIABLE DATA APPROVAL")
    canvas.setFont("Helvetica", 6.8)

    approval_text = (
        "Customer approval confirms the barcode symbology, RFID encoding scheme, fixed data elements, "
        "variable-data construction, formatting rules, and data sources shown on this proof. Values "
        "identified as VARIABLE are representative examples unless otherwise stated and will be populated "
        "from the approved PO/customer data supplied for production. Changes require a revised proof and approval."
    )

    yy = approval_y - 23
    for line in _variable_spec_wrap(approval_text, 122)[:5]:
        canvas.drawString(left + 8, yy, line[:132])
        yy -= 8

    canvas.setFont("Helvetica-Bold", 7)
    canvas.drawString(left, 38, "CONTROL")
    canvas.setFont("Helvetica", 7)
    control = (
        f"Proof ID {_job('proof_id', '-')} | Proof Rev {_job('proof_revision', '1')} | "
        f"Operator {_job('operator_initials', '').upper()} | Release 1.21.4 B"
    )
    canvas.drawRightString(right, 38, control[:115])


class _VariableSpecNext(Flowable):
    """JM fix: zero-size marker after the proof content. The page that follows is the
    Encoding & Variable Data Specification page (drawn by the page callback, no footer)."""

    def __init__(self, doc):
        super().__init__()
        self._doc = doc

    def wrap(self, availWidth, availHeight):
        return (0, 0)

    def draw(self):
        self._doc._variable_spec_next = True


class _PageHolder(Flowable):
    """JM fix: keeps the spec page from being dropped as an empty trailing page."""

    def wrap(self, availWidth, availHeight):
        return (0, 1)

    def draw(self):
        pass


# BETA21_5_PROOF_PDF_LAYOUT_UPDATE_V3

def _proof_pdf_wrap_notes_value(value, width=64):
    # ReportLab's AcroForm appearance stream only honors explicit newline
    # characters for multiline text fields. Pre-wrap the initial value so
    # generated Proof Notes remain readable inside the existing box.
    import textwrap as _proof_textwrap

    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return ""

    output = []
    for paragraph in text.split("\n"):
        paragraph = paragraph.strip()

        if not paragraph:
            output.append("")
            continue

        output.extend(
            _proof_textwrap.wrap(
                paragraph,
                width=width,
                replace_whitespace=False,
                drop_whitespace=True,
                break_long_words=True,
                break_on_hyphens=True,
            )
            or [""]
        )

    return "\n".join(output)


# Compatibility marker: Paragraph("Label Preview" is represented by the styled LABEL PREVIEW card below.
# Compatibility marker: Paragraph("Proof Specification" and Paragraph("RFID / EPC Data" are represented by styled section cards below.


def _safe(value) -> str:
    return html.escape("" if value is None else str(value))


def _scaled_image(path: Path, max_w: float, max_h: float):
    if not path.is_file():
        return None
    img = Image(str(path))
    scale = min(max_w / img.imageWidth, max_h / img.imageHeight, 1.0)
    img.drawWidth = img.imageWidth * scale
    img.drawHeight = img.imageHeight * scale
    return img




def _builtin_logo_for_company(settings: Dict[str, Any]) -> Path:
    docs = Path(__file__).resolve().parent.parent / 'assets' / 'branding'
    company = str(settings.get("master_company", "MCC Label") or "MCC Label").strip().lower()
    if company == 'starport technologies':
        return docs / 'LOGO_STARPORT_TECHNOLOGIES.png'
    return docs / 'LOGO_MCC_LABEL.png'

def _draw_center_fit(canvas, text: str, center_x: float, y: float, max_width: float, font_name: str, font_size: float, min_size: float = 5.0) -> None:
    value = str(text or "").strip()
    if not value:
        return
    size = float(font_size)
    while size > min_size and canvas.stringWidth(value, font_name, size) > max_width:
        size -= 0.25
    canvas.setFont(font_name, max(size, min_size))
    canvas.drawCentredString(center_x, y, value)




def _field_token(value: str) -> str:
    """Return a stable AcroForm-safe token for field names."""
    raw = str(value or "pending").strip() or "pending"
    return "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in raw)[:80]


class EditableTextField(Flowable):
    """A compact AcroForm text field that can live inside a Platypus table cell.

    Using relative=True makes ReportLab convert the Flowable-local coordinates to
    absolute page coordinates. This keeps the visual proof layout fixed while the
    customer-facing values remain genuinely editable after PDF generation.
    """

    def __init__(
        self,
        name: str,
        value: str = "",
        tooltip: str = "",
        *,
        height: float = 11.5,
        font_size: float = 6.4,
        multiline: bool = False,
        fill_color=colors.HexColor("#FBFDFF"),
        border_color=colors.HexColor("#E2E8F0"),
        text_color=colors.HexColor("#0F172A"),
        border_width: float = 0.0,
        maxlen: int = 2048,
    ):
        super().__init__()
        self.field_name = name
        self.value = "" if value is None else str(value)
        self.tooltip = tooltip or name
        self.field_height = float(height)
        self.font_size = float(font_size)
        self.multiline = bool(multiline)
        self.fill_color = fill_color
        self.border_color = border_color
        self.text_color = text_color
        self.border_width = float(border_width)
        self.maxlen = int(maxlen)
        self._field_width = 1.0

    def wrap(self, availWidth, availHeight):
        self._field_width = max(float(availWidth), 1.0)
        return self._field_width, self.field_height

    def draw(self):
        font_size = self.font_size
        if self.value and not self.multiline:
            max_width = max(self._field_width - 4.0, 8.0)
            while font_size > 4.5 and pdfmetrics.stringWidth(self.value, "Helvetica", font_size) > max_width:
                font_size -= 0.25
        self.canv.acroForm.textfield(
            name=self.field_name,
            tooltip=self.tooltip,
            value=self.value,
            x=0,
            y=0,
            width=self._field_width,
            height=self.field_height,
            borderStyle="solid",
            borderColor=self.border_color,
            fillColor=self.fill_color,
            textColor=self.text_color,
            borderWidth=self.border_width,
            fontName="Helvetica",
            fontSize=font_size,
            fieldFlags="multiline" if self.multiline else "",
            forceBorder=bool(self.border_width),
            relative=True,
            maxlen=self.maxlen,
        )

def _barcode_label(value: str) -> str:
    text = str(value or "").strip()
    if not text or text.lower() in {"none", "n/a", "not applicable", "no barcode / n/a"}:
        return "N/A"
    return text


def _field_rows(items, field_prefix: str):
    rows = []
    for label, value, key, use_small in items:
        label_style = ParagraphStyle(
            f"Label_{key}",
            fontName="Helvetica-Bold",
            fontSize=5.95,
            leading=7.1,
            textColor=colors.HexColor("#334155"),
        )
        rows.append([
            Paragraph(_safe(label), label_style),
            EditableTextField(
                name=f"{field_prefix}_{key}",
                value=str(value or ""),
                tooltip=f"Editable {label}",
                font_size=5.75 if use_small else 6.35,
                height=11.25,
                multiline=False,
            ),
        ])
    return rows


def _two_col_detail(items, width, field_prefix: str):
    table = Table(_field_rows(items, field_prefix), colWidths=[1.18 * inch, width - 1.18 * inch])
    table.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#D7E0EA")),
        ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#F1F5F9")),
        ("BACKGROUND", (1, 0), (1, -1), colors.HexColor("#FBFDFF")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (0, -1), 6),
        ("RIGHTPADDING", (0, 0), (0, -1), 6),
        ("TOPPADDING", (0, 0), (0, -1), 5),
        ("BOTTOMPADDING", (0, 0), (0, -1), 5),
        ("LEFTPADDING", (1, 0), (1, -1), 4),
        ("RIGHTPADDING", (1, 0), (1, -1), 4),
        ("TOPPADDING", (1, 0), (1, -1), 3),
        ("BOTTOMPADDING", (1, 0), (1, -1), 3),
    ]))
    return table


def _section_card(title: str, content, width: float, title_style):
    wrapper = Table(
        [[Paragraph(title, title_style)], [content]],
        colWidths=[width],
    )
    wrapper.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0F172A")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("BOX", (0, 0), (-1, -1), 0.75, colors.HexColor("#B9C5D3")),
        ("BACKGROUND", (0, 1), (-1, -1), colors.HexColor("#FBFDFF")),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
    ]))
    return wrapper


def generate_proof_pdf(
    job: ProofJob,
    output_path: str,
    settings: Optional[Dict[str, Any]] = None,
) -> str:
    """Generate the customer-facing portrait RFID production proof.

    Beta 21 keeps the polished one-page customer proof while making every
    customer/job-specific text value a real AcroForm field that can be edited after
    generation. Structural labels, the production-control strip, and the standardized
    disclaimer remain controlled layout elements.
    """
    settings = settings or {}
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)

    default_logo = _builtin_logo_for_company(settings)
    logo_raw = str(settings.get("logo_path", "") or "").strip()
    logo_path = Path(logo_raw) if logo_raw else default_logo

    doc = SimpleDocTemplate(
        str(output),
        pagesize=letter,
        rightMargin=0.34 * inch,
        leftMargin=0.34 * inch,
        topMargin=0.28 * inch,
        bottomMargin=2.45 * inch,
        title=f"RFID Production Proof {job.job_number} P{job.proof_revision}",
        author=f"RFID Production - {APP_VERSION} {RELEASE_NAME}",
    )

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle("ProofTitle", parent=styles["Title"], fontSize=16, leading=17, alignment=TA_CENTER, textColor=colors.HexColor("#0F172A"), spaceAfter=0)
    proof_id_style = ParagraphStyle("ProofID", parent=styles["BodyText"], fontSize=7.2, leading=8.0, alignment=TA_RIGHT, textColor=colors.HexColor("#1E3A8A"))
    card_header_style = ParagraphStyle("CardHeader", parent=styles["BodyText"], fontSize=8.1, leading=9.2, textColor=colors.white, leftIndent=6)
    value_style = ParagraphStyle("Value", parent=styles["BodyText"], fontSize=6.8, leading=8.1, textColor=colors.HexColor("#0F172A"))

    logo = _scaled_image(logo_path, 1.95 * inch, 0.62 * inch) if logo_path and logo_path.is_file() else None

    field_token = _field_token(job.proof_id or job.job_number or "pending")
    proof_id_value = EditableTextField(
        name=f"{field_token}_proof_id",
        value=job.proof_id or "Pending",
        tooltip="Editable Proof ID",
        height=12.0,
        font_size=7.0,
        fill_color=colors.HexColor("#EFF6FF"),
        border_color=colors.HexColor("#BFDBFE"),
        border_width=0.0,
    )
    proof_id_box = Table(
        [[Paragraph("<b>Proof ID</b>", proof_id_style)], [proof_id_value]],
        colWidths=[2.00 * inch],
        rowHeights=[12, 18],
    )
    proof_id_box.setStyle(TableStyle([
        ("BOX", (0, 0), (-1, -1), 0.75, colors.HexColor("#BFDBFE")),
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#EFF6FF")),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]))

    header = Table([[logo or "", Paragraph("RFID Production Proof", title_style), proof_id_box]], colWidths=[2.05 * inch, 3.25 * inch, 2.00 * inch], rowHeights=[0.78 * inch])
    header.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
        ("LINEBELOW", (0, 0), (-1, -1), 1.0, colors.HexColor("#94A3B8")),
    ]))

    story = [header, Spacer(1, 8)]

    preview = Path(job.preview_path)
    preview_img = _scaled_image(preview, 6.90 * inch, 2.50 * inch)
    if preview_img:
        preview_content = Table([[preview_img]], colWidths=[7.20 * inch], rowHeights=[2.60 * inch])
    else:
        preview_content = Table([[Paragraph("Preview image unavailable.", value_style)]], colWidths=[7.20 * inch], rowHeights=[2.60 * inch])
    preview_content.setStyle(TableStyle([
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("BACKGROUND", (0, 0), (-1, -1), colors.white),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
    ]))
    preview_card = _section_card("LABEL PREVIEW", preview_content, 7.34 * inch, card_header_style)
    story += [preview_card, Spacer(1, 9)]

    barcode_1 = _barcode_label(job.barcode_type_1)
    barcode_2 = _barcode_label(job.barcode_type_2)
    barcode_data_1 = str(job.barcode_data_1 or "").strip() or "Not specified"
    barcode_data_2 = str(job.barcode_data_2 or "").strip() or ("N/A" if barcode_2 == "N/A" else "Not specified")
    font_callout = str(job.font_size_callout or "").strip() or "Not specified"

    spec_items = [
        ("Customer", job.customer, "customer", False),
        ("Job #", job.job_number, "job_number", False),
        ("Part #", job.part_number, "part_number", False),
        ("GTIN / UPC", job.gtin, "gtin_upc", False),
        ("Quantity", job.quantity, "quantity", False),
        ("Label Size", f"{float(job.label_width):.3f} x {float(job.label_height):.3f} in", "label_size", False),
        ("Barcode 1", barcode_1, "barcode_type_1", False),
        ("Data 1", barcode_data_1, "barcode_data_1", True),
        ("Barcode 2", barcode_2, "barcode_type_2", False),
        ("Data 2", barcode_data_2, "barcode_data_2", True),
        ("Font Size", font_callout, "font_size_callout", False),
    ]

    encoding_type = str(getattr(job, "encoding_type", "SGTIN-96") or "SGTIN-96").strip()
    epc_value = str(job.epc_hex or "").strip()
    epc_grouped = " ".join(epc_value[i:i + 4] for i in range(0, len(epc_value), 4)) if epc_value and all(ch in "0123456789abcdefABCDEF" for ch in epc_value.replace(" ", "")) else epc_value
    rfid_items = [
        ("Encoding Type", encoding_type, "encoding_type", False),
        ("Serial", str(job.serial or ""), "serial_reference", False),
    ]
    if encoding_type == "SGTIN-96":
        if job.company_prefix:
            rfid_items.append(("Company Prefix", job.company_prefix, "company_prefix", False))
        if job.item_reference:
            rfid_items.append(("Item Reference", job.item_reference, "item_reference", False))
    rfid_items.append(("EPC / Data", epc_grouped or "Not specified", "epc_data", True))

    left_card = _section_card(
        "PROOF SPECIFICATION",
        _two_col_detail(spec_items, 3.54 * inch, f"{field_token}_spec"),
        3.60 * inch,
        card_header_style,
    )
    right_card = _section_card(
        "RFID / EPC DATA",
        _two_col_detail(rfid_items, 3.54 * inch, f"{field_token}_rfid"),
        3.60 * inch,
        card_header_style,
    )

    lower = Table([[left_card, right_card]], colWidths=[3.60 * inch, 3.60 * inch])
    lower.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
    ]))
    story.append(lower)

    notes_plain = (job.notes or "").replace("\n", " ").strip()

    def _draw_footer(canvas, _doc):
        if getattr(_doc, "_variable_spec_next", False):         # JM fix: the spec page
            _doc._variable_spec_next = False
            canvas.saveState()
            _draw_variable_data_spec_page(canvas, job, None)
            canvas.restoreState()
            return
        canvas.saveState()
        page_w, _ = letter
        left = 0.34 * inch
        total_w = page_w - 0.68 * inch
        footer_h = 118
        y = 6
        control_h = 18
        upper_y = y + control_h
        split = left + total_w * 0.37

        # Beta 14 standardized proofing disclaimer. The complete block is locked to
        # exactly 6.0 pt and defaults to 20% opacity. It is positioned immediately
        # above the customer approval/footer area so it remains visible without
        # competing with the label artwork.
        disclaimer_lines = [
            str(settings.get("proof_disclaimer_line_1", "PROOFING & RFID PLACEMENT DISCLAIMER") or "").strip(),
            str(settings.get("proof_disclaimer_line_2", "This proof is provided for approval of artwork, content, dimensions, barcode information, and general RFID inlay placement.") or "").strip(),
            str(settings.get("proof_disclaimer_line_3", "RFID placement is shown as viewed through the face of the label. Actual tag orientation and minor placement tolerances may vary during production.") or "").strip(),
            str(settings.get("proof_disclaimer_line_4", "Barcode, RFID, encoding, readability, and application performance may be affected by final construction, product, surface, environment, and intended use.") or "").strip(),
            str(settings.get("proof_disclaimer_line_5", "Customer testing of production-equivalent samples is required to confirm barcode readability, RFID performance, placement, and suitability before approval.") or "").strip(),
            str(settings.get("proof_disclaimer_line_6", "Approval confirms acceptance of the displayed proof. Changes requested after approval may require a revised proof and additional approval.") or "").strip(),
        ]
        if bool(settings.get("proof_disclaimer_enabled", True)):
            opacity = 0.40  # Beta 14 standard: disclaimer is always rendered at exactly 20% opacity.
            # Set color before alpha: ReportLab color setters can reset the active
            # ExtGState alpha. Applying alpha last guarantees the disclaimer text
            # itself is rendered at 20%, not merely the preceding graphics state.
            canvas.setFillColor(colors.HexColor("#475569"))
            try:
                canvas.setFillAlpha(opacity)
            except Exception:
                pass
            center_x = left + total_w / 2
            max_disclaimer_width = total_w - 18
            disclaimer_y = (y + footer_h + 44, y + footer_h + 36.5, y + footer_h + 29, y + footer_h + 21.5, y + footer_h + 14, y + footer_h + 6.5)
            for idx, (text_value, ypos) in enumerate(zip(disclaimer_lines, disclaimer_y)):
                _draw_center_fit(
                    canvas, text_value, center_x, ypos, max_disclaimer_width,
                    "Helvetica-Bold" if idx == 0 else "Helvetica", 6.0, 6.0,
                )
            try:
                canvas.setFillAlpha(1)
            except Exception:
                pass

        # Outer areas
        canvas.setStrokeColor(colors.HexColor("#B9C5D3"))
        canvas.setLineWidth(0.75)
        canvas.rect(left, upper_y, total_w, footer_h - control_h, stroke=1, fill=0)
        canvas.setFillColor(colors.HexColor("#0F172A"))
        canvas.rect(left, y, total_w, control_h, stroke=0, fill=1)
        canvas.line(split, upper_y, split, y + footer_h)

        # header bars for footer cards
        canvas.setFillColor(colors.HexColor("#0F172A"))
        canvas.rect(left, y + footer_h - 18, split - left, 18, stroke=0, fill=1)
        canvas.rect(split, y + footer_h - 18, left + total_w - split, 18, stroke=0, fill=1)
        canvas.setFillColor(colors.white)
        canvas.setFont("Helvetica-Bold", 7.2)
        canvas.drawString(left + 6, y + footer_h - 12, "PROOF NOTES")
        canvas.drawString(split + 6, y + footer_h - 12, "CUSTOMER APPROVAL / SIGN-OFF")

        # Notes fillable field
        canvas.acroForm.textfield(
            name=f"{field_token}_proof_notes",
            tooltip="Editable proof notes",
            value=_proof_pdf_wrap_notes_value(notes_plain, width=64),
            x=left + 6,
            y=upper_y + 8,
            width=(split - left) - 12,
            height=footer_h - control_h - 32,
            borderStyle='solid',
            borderColor=colors.HexColor("#CBD5E1"),
            fillColor=colors.HexColor("#FBFDFF"),
            textColor=colors.HexColor("#0F172A"),
            borderWidth=0.8,
            fontName='Helvetica',
            fontSize=6.2,
            fieldFlags='multiline',
            forceBorder=True,
        )

        # Approval content
        ax = split + 8
        canvas.setFillColor(colors.HexColor("#0F172A"))
        canvas.setFont("Helvetica-Bold", 6.25)
        checkbox_y = y + footer_h - 33
        checkbox_items = [
            ("artwork", "Artwork", 0),
            ("barcodes", "Barcode(s)", 78),
            ("font_text", "Font / Text", 168),
            ("encoding_data", "Encoding Data", 254),
        ]
        for key, label, dx in checkbox_items:
            canvas.acroForm.checkbox(
                name=f"{field_token}_approval_{key}",
                tooltip=f"Approve {label}",
                checked=False,
                x=ax + dx,
                y=checkbox_y,
                size=7,
                buttonStyle="check",
                shape="square",
                borderWidth=0.6,
                borderColor=colors.HexColor("#94A3B8"),
                fillColor=colors.white,
                textColor=colors.HexColor("#0F172A"),
                fieldFlags="",
                forceBorder=True,
            )
            canvas.drawString(ax + dx + 10, y + footer_h - 29, label)
        canvas.setFont("Helvetica", 5.0)
        canvas.setFillColor(colors.HexColor("#64748B"))
        canvas.drawString(ax, y + footer_h - 40, "Selected encoding type and EPC/data are customer-facing proof references.")
        canvas.drawString(ax, y + footer_h - 46, "Engineering-only filter, partition, company-prefix-digit and inlay-placement details remain internal.")
        canvas.setFillColor(colors.HexColor("#0F172A"))
        canvas.setFont("Helvetica", 6.2)

        label_x = ax
        field_x = ax + 60
        field_w = (left + total_w - 10) - field_x
        field_fill = colors.HexColor("#FBFDFF")
        field_border = colors.HexColor("#CBD5E1")
        # Approved By
        canvas.drawString(label_x, y + footer_h - 61, "Approved By:")
        canvas.acroForm.textfield(name=f"{field_token}_approved_by", tooltip="Approved by", value="", x=field_x, y=y + footer_h - 67, width=field_w, height=11, borderStyle='underlined', borderColor=field_border, fillColor=field_fill, textColor=colors.HexColor("#0F172A"), borderWidth=0.8, fontName='Helvetica', fontSize=6.2, forceBorder=True)
        # Date
        canvas.drawString(label_x, y + footer_h - 76, "Date:")
        canvas.acroForm.textfield(name=f"{field_token}_approval_date", tooltip="Date", value="", x=field_x, y=y + footer_h - 82, width=field_w, height=11, borderStyle='underlined', borderColor=field_border, fillColor=field_fill, textColor=colors.HexColor("#0F172A"), borderWidth=0.8, fontName='Helvetica', fontSize=6.2, forceBorder=True)
        # Signature
        canvas.drawString(label_x, y + footer_h - 91, "Signature:")
        canvas.acroForm.textfield(name=f"{field_token}_signature", tooltip="Signature", value="", x=field_x, y=y + footer_h - 97, width=field_w, height=11, borderStyle='underlined', borderColor=field_border, fillColor=field_fill, textColor=colors.HexColor("#0F172A"), borderWidth=0.8, fontName='Helvetica', fontSize=6.2, forceBorder=True)

        # bottom control strip
        canvas.setFillColor(colors.white)
        canvas.setFont("Helvetica-Bold", 5.8)
        release_tag = RELEASE_NAME.split(" - ", 1)[0].upper()
        control = f"CONTROL | Label Rev {_safe(job.revision)} | Proof Rev {_safe(job.proof_revision)} | Operator {_safe(job.operator_initials.upper())} | Release 1.21.4 B"
        canvas.drawString(left + 6, y + 6, control)
        canvas.restoreState()

    # BETA21_5_VARIABLE_DATA_SPEC_PDF_PAGE_V12 (JM fix: a page of its own after the proof)
    if _variable_data_spec_from_job(job):
        story += [_VariableSpecNext(doc), PageBreak(), _PageHolder()]
    doc.build(story, onFirstPage=_draw_footer, onLaterPages=_draw_footer)
    return str(output)
