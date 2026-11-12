"""Build the benchmark documents in `benchmarks/cases/`.

    uv run python benchmarks/make_cases.py            # the documents Python can write
    (then, inside the benchmark image)  sh benchmarks/make_legacy_cases.sh   # .doc/.ppt/.xls/.rtf/ODF/EPUB via LibreOffice and pandoc

Everything here is synthetic and its content is known exactly, which is what lets the probes in `probes.py` be checked
by a program instead of by eye. The Office, PDF, HTML and mail documents are the ones from `examples/` (built by
`examples/make_samples.py`); this script adds the formats and details the examples do not cover.
The three Hangul files are public samples (see examples/samples/THIRD_PARTY.md).
"""

from email.message import EmailMessage
from io import BytesIO
import json
from pathlib import Path
import shutil
import sys

from docx import Document
from docx.oxml import parse_xml
from docx.oxml.ns import qn
from PIL import Image, ImageDraw, ImageFont


ROOT = Path(__file__).parent
CASES = ROOT / "cases"
sys.path.insert(0, str(ROOT.parent / "examples"))
import make_samples  # noqa: E402 — the example documents are reused as benchmark cases


def text_image(lines: list[str], size: tuple[int, int] = (1000, 600)) -> Image.Image:
    image = Image.new("RGB", size, "white")
    draw = ImageDraw.Draw(image)
    for index, line in enumerate(lines):
        draw.text((60, 60 + index * 90), line, fill="black", font=ImageFont.load_default(size=56))
    return image


def image_bytes(image: Image.Image, image_format: str, **options: object) -> bytes:
    buffer = BytesIO()
    image.save(buffer, image_format, **options)
    return buffer.getvalue()


def rich_docx() -> bytes:
    """Inline formatting and a hyperlink: things a converter may keep (as Markdown) or flatten to plain text."""
    document = Document()
    document.add_heading("Release checklist", level=1)
    paragraph = document.add_paragraph("Before tagging, read the ")
    relationship = document.part.relate_to("https://example.com/release-guide",
                                           "http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink",
                                           is_external=True)
    paragraph._p.append(parse_xml(
        f'<w:hyperlink xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
        f'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" r:id="{relationship}">'
        "<w:r><w:t>release guide</w:t></w:r></w:hyperlink>"))
    paragraph.add_run(". Changes marked ")
    paragraph.add_run("breaking").bold = True
    paragraph.add_run(" need a ")
    paragraph.add_run("migration note").italic = True
    paragraph.add_run(".")
    table = document.add_table(rows=3, cols=3)
    table.style = "Table Grid"
    for row_index, row in enumerate([("Step", "Owner", "Done"), ("Changelog", "Mina", "yes"), ("Tag", "Joon", "no")]):
        for column_index, value in enumerate(row):
            table.cell(row_index, column_index).text = value
    section = document.sections[0]
    section.header.paragraphs[0].text = "CONFIDENTIAL DRAFT HEADER"
    section.footer.paragraphs[0].text = "Internal footer text"
    assert document.element.find(qn("w:body")) is not None
    buffer = BytesIO()
    document.save(buffer)
    return buffer.getvalue()


RICH_HTML = """<!doctype html><html><head><meta charset="utf-8"><title>Release checklist</title></head><body>
<h1>Release checklist</h1>
<p>Before tagging, read the <a href="https://example.com/release-guide">release guide</a>. Changes marked
<strong>breaking</strong> need a <em>migration note</em>.</p>
<p><img src="https://example.com/pipeline.png" alt="Release pipeline diagram"></p>
<ul><li>Changelog updated<ul><li>Breaking changes listed</li></ul></li><li>Tag pushed</li></ul>
</body></html>
"""

XML_SPREADSHEET = """<?xml version="1.0"?>
<?mso-application progid="Excel.Sheet"?>
<Workbook xmlns="urn:schemas-microsoft-com:office:spreadsheet" xmlns:ss="urn:schemas-microsoft-com:office:spreadsheet">
 <Worksheet ss:Name="Inventory">
  <Table>
   <Row><Cell><Data ss:Type="String">Part</Data></Cell><Cell><Data ss:Type="String">Bin</Data></Cell><Cell><Data ss:Type="String">Quantity</Data></Cell></Row>
   <Row><Cell><Data ss:Type="String">Gasket KX-200</Data></Cell><Cell><Data ss:Type="String">A-14</Data></Cell><Cell><Data ss:Type="Number">315</Data></Cell></Row>
   <Row><Cell><Data ss:Type="String">Bearing ZR-9</Data></Cell><Cell><Data ss:Type="String">C-02</Data></Cell><Cell><Data ss:Type="Number">48</Data></Cell></Row>
  </Table>
 </Worksheet>
</Workbook>
"""

NOTEBOOK = {"cells": [{"cell_type": "markdown", "metadata": {}, "source": ["# Churn analysis\n", "Quarterly churn fell to 3.2 percent."]},
                      {"cell_type": "code", "execution_count": 1, "metadata": {}, "outputs": [],
                       "source": ["retained = customers[customers.active]\n", "print(len(retained))"]}],
            "metadata": {"kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"}},
            "nbformat": 4, "nbformat_minor": 5}


def mhtml_page() -> bytes:
    """A web page saved as a single file (what browsers write as .mht / .mhtml)."""
    message = EmailMessage()
    message["From"] = "<Saved by Blink>"
    message["Subject"] = "Warehouse dashboard"
    message["MIME-Version"] = "1.0"
    message.set_content("<html><body><h1>Warehouse dashboard</h1><p>Open orders: 1,482</p><table><tr><th>Dock</th><th>Trucks</th></tr>"
                        "<tr><td>North dock</td><td>17</td></tr></table></body></html>", subtype="html")
    message.make_related()
    message.add_related(image_bytes(Image.new("RGB", (8, 8), "navy"), "PNG"), maintype="image", subtype="png", cid="<logo>")
    return message.as_bytes()


def mbox_file() -> bytes:
    def mail(subject: str, sender: str, body: str) -> bytes:
        message = EmailMessage()
        message["Subject"], message["From"], message["To"] = subject, sender, "ops@example.com"
        message["Date"] = "Tue, 10 Feb 2026 08:00:00 +0900"
        message.set_content(body)
        return b"From MAILER-DAEMON Tue Feb 10 08:00:00 2026\n" + message.as_bytes() + b"\n"

    return (mail("Pump station alarm cleared", "noc@example.com", "Alarm 7731 on pump station B cleared at 07:52.")
            + mail("Night shift handover", "shift@example.com", "Two tickets remain open: valve V-18 and sensor T-204."))


def main() -> None:
    if CASES.exists():
        shutil.rmtree(CASES)
    CASES.mkdir(parents=True)
    samples = ROOT.parent / "examples" / "samples"
    files: dict[str, bytes] = {
        # Office and PDF documents shared with examples/
        "report.docx": make_samples.docx_report(),
        "deck.pptx": make_samples.pptx_deck(),
        "sales.xlsx": make_samples.xlsx_workbook(),
        "service-report.pdf": make_samples.pdf_report(),
        "scanned-notice.pdf": make_samples.scanned_pdf(),
        "article.html": make_samples.HTML_PAGE.encode("utf-8"),
        "meeting.eml": make_samples.eml_message(),
        "html-table-named.xls": make_samples.FAKE_XLS.encode("utf-8"),
        "branch-sales-cp949.csv": make_samples.KOREAN_CSV.encode("cp949"),
        "release-notes.md": make_samples.MARKDOWN_NOTES.encode("utf-8"),
        # Added for the benchmark
        "rich-text.docx": rich_docx(),
        "rich-text.html": RICH_HTML.encode("utf-8"),
        "inventory-2003.xml": XML_SPREADSHEET.encode("utf-8"),
        "dashboard.mht": mhtml_page(),
        "mailbox.mbox": mbox_file(),
        "orders.csv": b"order_id,customer,total\n10021,Northwind,1280.00\n10022,Contoso,310.50\n",
        "notes.txt": b"Maintenance window\n\nThe database upgrade starts at 02:00 UTC and takes 40 minutes.\n",
        "notice-cp949.txt": "정기 점검 안내\n\n서버 점검은 3월 7일 02시부터 04시까지 진행됩니다.\n".encode("cp949"),
        "analysis.ipynb": json.dumps(NOTEBOOK).encode(),
        "invoice.png": image_bytes(text_image(["INVOICE 2026-0457", "Total due: 1,930.00 USD"]), "PNG"),
        "receipt.jpg": image_bytes(text_image(["RECEIPT 88-1204", "Paid: 64.20 EUR"]), "JPEG", quality=92),
        "ticket.heic": image_bytes(text_image(["BOARDING PASS", "Gate C17 Seat 22A"]), "HEIF"),
        "fax.tiff": image_bytes(text_image(["FAX PAGE ONE", "Ref 5501-A"]), "TIFF", save_all=True,
                                append_images=[text_image(["FAX PAGE TWO", "Ref 5502-B"])]),
    }
    for name, data in files.items():
        (CASES / name).write_bytes(data)
    for name in ("table.hwp", "score-table.hwpx", "numbered-outline.hwpx"):
        shutil.copy(samples / name, CASES / name)
    print(f"{len(list(CASES.iterdir()))} files in {CASES}")


if __name__ == "__main__":
    main()
