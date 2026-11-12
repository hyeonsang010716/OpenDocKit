"""What the benchmark checks, as data and small functions a reader can audit.

Two suites are defined here:

COVERAGE — one representative file per format. A tool passes a row when its output contains every string in
`expect` (text that is plainly in the document). "Partial" means it produced output without all of them; "fail" means
an error, an empty result or a timeout.

PROBES — yes/no questions about structure, asked of the realistic documents: is the table still a table, did the
merged cell survive, is the running header gone. Each probe is a short function over the tool's Markdown. Several
probes are ones OpenDocKit is known to fail (link targets, bold text); they are here because they matter to users.

Matching is forgiving about Markdown noise: case, whitespace, backslash escapes, emphasis markers and HTML entities
are normalized away before any comparison (`normalize`).
"""

from collections.abc import Callable
from dataclasses import dataclass
import html
import re
import unicodedata


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFC", html.unescape(text))
    text = text.replace("\\", "").replace("*", "").replace(" ", " ").replace("−", "-").replace("–", "-").replace("—", "-")
    return re.sub(r"\s+", " ", text).strip().lower()


def contains(text: str, *needles: str) -> bool:
    haystack = normalize(text)
    return all(normalize(needle) in haystack for needle in needles)


def rows(text: str) -> list[str]:
    """Table rows in the output, whichever table syntax the tool writes: Markdown pipes or HTML <tr>."""
    found = [line for line in text.split("\n") if line.strip().startswith("|")]
    found += re.findall(r"<tr\b.*?</tr>", text, flags=re.DOTALL | re.IGNORECASE)
    return found


def table_row(text: str, *cells: str) -> bool:
    """Some table row holds all of `cells` — the association between the values of a row survived, as a table."""
    return any(contains(row, *cells) for row in rows(text))


def line_with(text: str, *parts: str) -> bool:
    """Some single line holds all of `parts` (a weaker form of table_row: the values stayed together)."""
    return any(contains(line, *parts) for line in text.split("\n")) or table_row(text, *parts)


def in_order(text: str, *needles: str) -> bool:
    haystack, position = normalize(text), -1
    for needle in needles:
        position = haystack.find(normalize(needle), position + 1)
        if position < 0:
            return False
    return True


def count(text: str, needle: str) -> int:
    return normalize(text).count(normalize(needle))


def heading(text: str, title: str) -> bool:
    return any(re.match(r"\s{0,3}#{1,6}\s", line) and contains(line, title) for line in text.split("\n")) or bool(
        re.search(rf"<h[1-6][^>]*>[^<]*{re.escape(title)}", text, flags=re.IGNORECASE))


def nested_under(text: str, parent: str, child: str) -> bool:
    """`child` is visibly subordinate to `parent`: an indented list item under a list item, or a list item under a paragraph."""
    lines = [line for line in text.split("\n") if line.strip()]
    for index, line in enumerate(lines):
        if not contains(line, parent):
            continue
        parent_match = re.match(r"(\s*)([-*+•]|\d+[.)])\s+", line)
        for follower in lines[index + 1:index + 3]:
            child_match = re.match(r"(\s*)([-*+•]|\d+[.)])\s+(.*)", follower)
            if child_match and contains(child_match.group(3), child):
                if parent_match is None:
                    return True
                return len(child_match.group(1).replace("\t", "  ")) > len(parent_match.group(1).replace("\t", "  "))
    return False


def header_row(text: str, *cells: str) -> bool:
    """A row holding `cells` is a table's header row: followed by a Markdown separator row, or written with <th>."""
    lines = [line for line in text.split("\n") if line.strip()]
    for index, line in enumerate(lines[:-1]):
        if line.strip().startswith("|") and contains(line, *cells) and re.fullmatch(r"\s*\|[\s:|-]+\|\s*", lines[index + 1]):
            return True
    return any("<th" in row.lower() and contains(row, *cells) for row in re.findall(r"<tr\b.*?</tr>", text, flags=re.DOTALL | re.IGNORECASE))


def list_item(text: str, item: str, *, ordered: bool | None = None, indented: bool = False) -> bool:
    for line in text.split("\n"):
        match = re.match(r"(\s*)([-*+•]|\d+[.)])\s+(.*)", line)
        if not match or not contains(match.group(3), item):
            continue
        if ordered is not None and ordered != match.group(2)[0].isdigit():
            continue
        if indented and len(match.group(1).replace("\t", "  ")) < 2:
            continue
        return True
    return False


# ---- Suite 1: format coverage -------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Case:
    file: str
    label: str
    group: str
    expect: tuple[str, ...]
    note: str = ""


COVERAGE = [
    Case("report.docx", "Word (.docx)", "Office", ("Quarterly Business Review", "Gross margin improved", "Overseas")),
    Case("deck.pptx", "PowerPoint (.pptx)", "Office", ("Product Launch Plan", "Feature freeze", "Staged rollout")),
    Case("sales.xlsx", "Excel (.xlsx)", "Office", ("Northwind", "Contoso", "Fabrikam")),
    Case("legacy-sales.xls", "Excel 97-2003 (.xls)", "Office", ("Northwind", "Contoso", "Fabrikam")),
    Case("inventory-2003.xml", "Excel 2003 XML", "Office", ("Gasket KX-200", "Bearing ZR-9", "315")),
    Case("legacy-report.doc", "Word 97-2003 (.doc)", "Office", ("Quarterly Business Review", "Gross margin improved", "Overseas")),
    Case("legacy-deck.ppt", "PowerPoint 97-2003 (.ppt)", "Office", ("Product Launch Plan", "Feature freeze", "Staged rollout")),
    Case("report.rtf", "Rich Text (.rtf)", "Office", ("Quarterly Business Review", "Gross margin improved", "Overseas")),
    Case("report.odt", "OpenDocument Text (.odt)", "Office", ("Quarterly Business Review", "Gross margin improved", "Overseas")),
    Case("sales.ods", "OpenDocument Spreadsheet (.ods)", "Office", ("Northwind", "Contoso", "Fabrikam")),
    Case("deck.odp", "OpenDocument Presentation (.odp)", "Office", ("Product Launch Plan", "Feature freeze", "Staged rollout")),

    Case("table.hwp", "Hangul (.hwp)", "Korean office", ("ABC", "OPQ", "UVM")),
    Case("score-table.hwpx", "Hangul (.hwpx)", "Korean office", ("C반 기말고사", "개똥이", "국어")),
    Case("branch-sales-cp949.csv", "CSV in CP949", "Korean office", ("강남점", "김민준", "128,450,000")),
    Case("notice-cp949.txt", "Text in CP949", "Korean office", ("정기 점검 안내", "3월 7일")),

    Case("service-report.pdf", "PDF (text)", "PDF", ("Service Level Report", "Expired certificate", "99.97%")),
    Case("scanned-notice.pdf", "PDF (scanned, no text layer)", "PDF", ("NOTICE OF OFFICE RELOCATION", "ADM-2026-017", "Harbor Tower"),
         "needs OCR or a vision model"),

    Case("article.html", "HTML", "Web and mail", ("Deploying with blue-green releases", "Rollback time", "lb set-backend")),
    Case("dashboard.mht", "MHTML (.mht)", "Web and mail", ("Warehouse dashboard", "Open orders: 1,482", "North dock")),
    Case("meeting.eml", "Email (.eml)", "Web and mail", ("Budget review moved to Thursday", "dana@example.com", "read the attached draft")),
    Case("mailbox.mbox", "Mailbox (.mbox)", "Web and mail", ("Pump station alarm cleared", "Night shift handover", "valve V-18")),

    Case("notes.txt", "Plain text", "Text and data", ("Maintenance window", "02:00 UTC")),
    Case("release-notes.md", "Markdown", "Text and data", ("Release notes 2.4", "Dates lost their time zone")),
    Case("orders.csv", "CSV", "Text and data", ("Northwind", "1280.00", "Contoso")),
    Case("analysis.ipynb", "Jupyter notebook", "Text and data", ("Churn analysis", "3.2 percent", "print(len(retained))")),
    Case("release-notes.epub", "EPUB", "Text and data", ("Release notes 2.4", "Dates lost their time zone")),

    Case("invoice.png", "Image (.png)", "Images", ("INVOICE 2026-0457", "1,930.00"), "needs OCR or a vision model"),
    Case("receipt.jpg", "Image (.jpg)", "Images", ("RECEIPT 88-1204", "64.20"), "needs OCR or a vision model"),
    Case("fax.tiff", "Image (multi-page .tiff)", "Images", ("FAX PAGE ONE", "FAX PAGE TWO", "5502-B"), "needs OCR or a vision model"),
    Case("ticket.heic", "Image (.heic)", "Images", ("BOARDING PASS", "Gate C17"), "needs OCR or a vision model"),

    Case("html-table-named.xls", "HTML table saved as .xls", "Mislabeled", ("Northwind", "Fabrikam", "2,045.75"),
         "what many web apps produce for 'Download as Excel'"),
]


# ---- Suite 2: structure probes ------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Probe:
    id: str
    file: str
    question: str
    check: Callable[[str], bool]


def _one_header(text: str) -> bool:
    return 0 < count(text, "Service Level Report") <= 2  # the title once, plus at most one stray running header


PROBES = [
    # Word
    Probe("docx-headings", "report.docx", "Section titles are Markdown headings", lambda t: heading(t, "Summary") and heading(t, "Revenue by region")),
    Probe("docx-bullets", "report.docx", "Bulleted list items are list items", lambda t: list_item(t, "Gross margin improved", ordered=False)),
    Probe("docx-numbered", "report.docx", "Numbered steps keep their numbers", lambda t: list_item(t, "Convert to USD at the month-end rate", ordered=True)),
    Probe("docx-table", "report.docx", "The table is a table (a row holds North, 150, 170)", lambda t: table_row(t, "North", "150", "170")),
    Probe("docx-merged-cell", "report.docx", "The vertically merged 'Domestic' cell still applies to the South row",
          lambda t: table_row(t, "Domestic", "South", "95") or (table_row(t, "South", "95", "110") and bool(re.search(r"rowspan=.?2", t)))),
    Probe("docx-equation", "report.docx", "The equation is written as LaTeX", lambda t: "\\frac" in t),
    Probe("docx-image-position", "report.docx", "The picture leaves a marker between the text before and after it",
          lambda t: bool(re.search(r"only its position is kept\.?\s*(!\[|\[image|<!--\s*image|<img|\*\*==>|\[figure)", normalize(t)))),
    Probe("docx-link-target", "rich-text.docx", "A hyperlink keeps its address", lambda t: "https://example.com/release-guide" in t),
    Probe("docx-bold", "rich-text.docx", "Bold text stays bold", lambda t: bool(re.search(r"(\*\*|__|<b>|<strong>)\s*breaking", t))),
    Probe("docx-italic", "rich-text.docx", "Italic text stays italic", lambda t: bool(re.search(r"(?<![*_])([*_]|<i>|<em>)\s*migration note", t))),
    Probe("docx-no-header-footer", "rich-text.docx", "Page header and footer text is kept out of the body",
          lambda t: contains(t, "Release checklist") and not contains(t, "CONFIDENTIAL DRAFT HEADER") and not contains(t, "Internal footer text")),

    # PowerPoint
    Probe("pptx-slides", "deck.pptx", "Each slide title is a heading", lambda t: heading(t, "Goals") and heading(t, "Schedule") and heading(t, "Expected adoption")),
    Probe("pptx-nesting", "deck.pptx", "A second-level bullet is visibly nested under its parent",
          lambda t: nested_under(t, "Ship to all regions by 30 April", "Staged rollout")),
    Probe("pptx-notes", "deck.pptx", "Speaker notes are included", lambda t: contains(t, "depends on the security review")),
    Probe("pptx-table", "deck.pptx", "The slide table is a table", lambda t: table_row(t, "Security review", "Platform", "2026-03-27")),
    Probe("pptx-chart-data", "deck.pptx", "The native chart becomes its numbers (Week 4: 8100 and 1650 in one row)",
          lambda t: table_row(t, "Week 4", "8100", "1650")),

    # Excel
    Probe("xlsx-table", "sales.xlsx", "Cell values come out as a table", lambda t: table_row(t, "South", "80", "85")),
    Probe("xlsx-two-tables", "sales.xlsx", "The second table on the sheet is its own table, with its own header row",
          lambda t: header_row(t, "Customer", "Closed", "Amount") and table_row(t, "Contoso", "250.5")),
    Probe("xlsx-percent", "sales.xlsx", "Percentages read as Excel shows them (46.4%)", lambda t: contains(t, "46.4%")),
    Probe("xlsx-date", "sales.xlsx", "Dates read as dates, without a spurious time", lambda t: contains(t, "2025-09-02") and not contains(t, "2025-09-02 00:00")),
    Probe("xlsx-sheets", "sales.xlsx", "The second sheet is included and named", lambda t: contains(t, "Notes", "Source: finance ledger")),
    Probe("xlsx-hidden-sheet", "sales.xlsx", "A hidden sheet is left out", lambda t: contains(t, "Northwind") and not contains(t, "internal working data")),

    # PDF
    Probe("pdf-headings", "service-report.pdf", "Headings are detected from the layout", lambda t: heading(t, "Availability") and heading(t, "Incidents")),
    Probe("pdf-ruled-table", "service-report.pdf", "The ruled table is a table with all five columns in a row",
          lambda t: table_row(t, "Web console", "99.90%", "99.93%", "99.88%", "99.95%")),
    Probe("pdf-borderless-table", "service-report.pdf", "The table without borders is still a table",
          lambda t: table_row(t, "2025-11-19", "Web console", "52 min", "Database failover")),
    Probe("pdf-running-header", "service-report.pdf", "The header repeated on every page is not repeated in the output", _one_header),
    Probe("pdf-page-numbers", "service-report.pdf", "Page-number lines are removed", lambda t: not re.search(r"(?m)^\s*page [1-4]\s*$", normalize_lines(t))),
    Probe("pdf-two-columns", "service-report.pdf", "Two-column pages are read down the left column, then the right",
          lambda t: in_order(t, "3.1 Review round 1", "Root cause analysis", "3.2 Review round 2", "3.3 Review round 3", "3.4 Review round 4")),

    # HTML
    Probe("html-chrome-removed", "article.html", "Navigation, script and footer boilerplate is dropped",
          lambda t: contains(t, "Deploying with blue-green releases") and not contains(t, "Pricing") and not contains(t, "window.analytics")),
    Probe("html-hidden-removed", "article.html", "Hidden elements are dropped", lambda t: contains(t, "Checklist") and not contains(t, "Tracking pixel placeholder")),
    Probe("html-spanned-header", "article.html", "A two-row header with rowspan/colspan still labels the columns",
          lambda t: table_row(t, "Compute") and table_row(t, "Rolling", "1.1×", "Medium", "Minutes")),
    Probe("html-nested-list", "article.html", "The nested list keeps its nesting", lambda t: nested_under(t, "Run smoke tests against green", "Login")),
    Probe("html-code-block", "article.html", "The code block is fenced", lambda t: bool(re.search(r"```[^\n]*\n\s*lb set-backend", t))),
    Probe("html-mathml", "article.html", "MathML is written as LaTeX", lambda t: "\\frac" in t),
    Probe("html-link-target", "rich-text.html", "A hyperlink keeps its address", lambda t: "https://example.com/release-guide" in t),
    Probe("html-bold", "rich-text.html", "Bold text stays bold", lambda t: bool(re.search(r"(\*\*|__|<b>|<strong>)\s*breaking", t))),
    Probe("html-italic", "rich-text.html", "Italic text stays italic", lambda t: bool(re.search(r"(?<![*_])([*_]|<i>|<em>)\s*migration note", t))),
    Probe("html-image-alt", "rich-text.html", "An image's alt text is kept", lambda t: contains(t, "Release pipeline diagram")),

    # Mail
    Probe("eml-headers", "meeting.eml", "Subject, sender and recipient are kept", lambda t: contains(t, "Budget review moved to Thursday", "dana@example.com", "finance@example.com")),
    Probe("eml-body-once", "meeting.eml", "The body is read once (not the HTML and the plain-text copy)", lambda t: count(t, "read the attached draft before the meeting") == 1),
    Probe("eml-table", "meeting.eml", "The table in the HTML body is a table", lambda t: table_row(t, "Travel", "60,000", "45,000")),
    Probe("eml-invitation", "meeting.eml", "The meeting invitation's place and time are readable",
          lambda t: contains(t, "Room 7B") and bool(re.search(r"2026-02-12|20260212|feb(ruary)? 12", normalize(t)))),
    Probe("mbox-both-mails", "mailbox.mbox", "Both mails of the mailbox are read, in order", lambda t: in_order(t, "Pump station alarm cleared", "Alarm 7731", "Night shift handover", "sensor T-204")),

    # Korean documents
    Probe("hwp-table", "table.hwp", "The HWP table is a table", lambda t: table_row(t, "LMN", "OPQ", "STR")),
    Probe("hwpx-table", "score-table.hwpx", "The HWPX table is a table", lambda t: table_row(t, "칠득이", "77", "77", "77")),
    Probe("hwpx-numbering", "numbered-outline.hwpx", "Multi-level numbering is kept (3.1., 가.)", lambda t: contains(t, "3.1. 번호3-1") and contains(t, "가. 개요1-1")),
    Probe("cp949-csv-table", "branch-sales-cp949.csv", "A CP949 CSV is decoded and its rows are a table", lambda t: table_row(t, "부산점", "이서연", "96,200,000")),
    Probe("cp949-csv-title", "branch-sales-cp949.csv", "Title lines above the header row do not become the header",
          lambda t: table_row(t, "지점", "담당자") and not table_row(t, "2025년 4분기 지점별 매출 현황")),

    # Detection
    Probe("mislabeled-xls", "html-table-named.xls", "An HTML table named .xls is read as the table it is", lambda t: table_row(t, "10022", "Contoso", "Pending", "310.50")),
]


def normalize_lines(text: str) -> str:
    return "\n".join(normalize(line) for line in text.split("\n"))
