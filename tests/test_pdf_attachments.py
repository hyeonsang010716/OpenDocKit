"""Files attached to a PDF (EmbeddedFiles name tree, FileAttachment annotations)."""


from opendockit import convert
from tests.test_pdf import _raw_pdf


def _attachment_pdf(names_tree: bytes) -> bytes:
    """One line of body text and two attachments: object 6 (report.txt, name tree + page annotation) and object 9
    (사진.png, annotation only)."""
    content = b"BT /F1 12 Tf 50 780 Td (Main body text: see attachment.) Tj ET"
    attached = b"Quarterly revenue 1,234,000 KRW\n"
    korean_name = b"<FEFF" + "사진.png".encode("utf-16-be").hex().upper().encode() + b">"
    return _raw_pdf({1: b"<< /Type /Catalog /Pages 2 0 R " + names_tree + b" >>",
                     2: b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
                     3: b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 4 0 R "
                        b"/Resources << /Font << /F1 << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> >> >> /Annots [8 0 R 10 0 R] >>",
                     4: b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream",
                     6: b"<< /Type /Filespec /F (report.txt) /UF (report.txt) /EF << /F 7 0 R >> >>",
                     7: b"<< /Type /EmbeddedFile /Length %d >>\nstream\n" % len(attached) + attached + b"\nendstream",
                     8: b"<< /Type /Annot /Subtype /FileAttachment /Rect [50 700 70 720] /FS 6 0 R >>",
                     9: b"<< /Type /Filespec /F (photo.png) /UF " + korean_name + b" /EF << /F 7 0 R >> >>",
                     10: b"<< /Type /Annot /Subtype /FileAttachment /Rect [80 700 100 720] /FS 9 0 R >>"})


class TestEmbeddedFiles:
    """Attachments were dropped without a warning (.eml reports the names and the number of attachments)."""

    def test_attachments_are_named_and_counted_in_a_warning(self):
        result = convert(_attachment_pdf(b"/Names << /EmbeddedFiles << /Names [(report.txt) 6 0 R] >> >>"), "a.pdf")

        assert "Main body text: see attachment." in result.text
        assert "Quarterly revenue" not in result.text
        assert "2 PDF attachment(s) (report.txt, 사진.png) were not read. Convert attachments separately." in result.warnings

    def test_attachments_in_a_name_tree_with_kids_are_found(self):
        tree = b"/Names << /EmbeddedFiles << /Kids [<< /Limits [(a) (z)] /Names [(report.txt) 6 0 R] >>] >> >>"

        result = convert(_attachment_pdf(tree), "a.pdf")

        assert any(warning.startswith("2 PDF attachment(s) (") for warning in result.warnings)

    def test_a_huge_kids_array_is_looked_at_only_up_to_the_node_bound(self, monkeypatch):
        """The whole /Kids array of a node was resolved before the bound on the nodes looked at applied, so one node with
        millions of kids resolved every one of them."""
        from opendockit.extractors import _pdf_forms

        kids = [{"Names": [b"f%d" % index, {"F": b"f%d.txt" % index}]} for index in range(200_000)]
        kid_ids = {id(kid) for kid in kids}
        resolved_kids = []

        def resolve(value):
            if id(value) in kid_ids:
                resolved_kids.append(value)
            return value

        monkeypatch.setattr(_pdf_forms, "resolve1", resolve)
        found = {}

        _pdf_forms._named_attachments({"Names": {"EmbeddedFiles": {"Kids": kids}}}, found)

        assert 0 < len(found) < _pdf_forms._ATTACHMENT_TREE_NODES
        assert len(resolved_kids) < _pdf_forms._ATTACHMENT_TREE_NODES

    def test_attachments_given_as_direct_objects_are_each_counted(self):
        """Attachments were told apart by the id() of their file specification, and the id of a direct object freed after
        it was counted could be taken by the next one, which was then dropped."""
        from opendockit.extractors._pdf_forms import _add_attachment

        found = {}
        for name in (b"a.txt", b"b.txt", b"c.txt"):
            _add_attachment({"F": name}, None, found)

        assert sorted(found.values()) == ["a.txt", "b.txt", "c.txt"]
