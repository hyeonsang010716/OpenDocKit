"""The recall scorecard (`evals/recall.py`): the words a document states in its own XML against its conversion."""

import json
from pathlib import Path
import struct

from evals.fetch_hangul_samples import sample_name
from evals.recall import _counts, _hwp_text, compare, compare_text, main, run, source_units
from tests import builder
from tests.test_hwp import TAG_CTRL_HEADER, TAG_LIST_HEADER, TAG_PARA_HEADER, TAG_PARA_TEXT, _fake_hwp, _sample_section


def _hwpx(tmp_path, body: str, name: str = "a.hwpx"):
    path = tmp_path / name
    path.write_bytes(builder.hwpx_bytes(body))
    return path


class TestSourceUnits:
    def test_the_builders_documents_lose_no_word(self, tmp_path):
        for name, data in (("a.docx", builder.docx_bytes()), ("a.pptx", builder.pptx_bytes()),
                           ("a.xlsx", builder.xlsx_bytes()), ("a.hwpx", builder.hwpx_bytes())):
            (tmp_path / name).write_bytes(data)

        card = run(tmp_path)

        assert card["totals"]["converted"] == 4
        assert card["totals"]["words"] > 20
        assert [entry["missing"] for entry in card["documents"].values()] == [[], [], [], []]
        assert [entry["joined"] for entry in card["documents"].values()] == [[], [], [], []]

    def test_a_paragraph_is_one_unit_and_a_break_inside_it_is_a_space(self, tmp_path):
        path = _hwpx(tmp_path, "<hp:p><hp:run><hp:t>첫 줄<hp:lineBreak/>둘째</hp:t></hp:run><hp:run><hp:t> 줄</hp:t></hp:run></hp:p>")

        (part, pieces), = source_units(path)

        assert part == "Contents/section0.xml"
        assert "".join(text for text, _ in pieces) == "첫 줄 둘째 줄"
        assert pieces[0][1].endswith("p/run/t")

    def test_a_paragraph_inside_a_paragraph_is_a_unit_of_its_own(self, tmp_path):
        path = tmp_path / "a.docx"
        path.write_bytes(builder.zip_with(builder.docx_bytes(), {"word/document.xml": lambda xml: xml.replace(
            "</w:body>",
            "<w:p><w:r><w:t>바깥</w:t></w:r><w:r><w:pict><w:txbxContent><w:p><w:r><w:t>상자 안</w:t></w:r></w:p></w:txbxContent>"
            "</w:pict></w:r><w:r><w:t>문단</w:t></w:r></w:p></w:body>")}))

        texts = ["".join(text for text, _ in pieces) for _, pieces in source_units(path)]

        assert "바깥문단" in texts and "상자 안" in texts


    def test_a_master_page_of_an_hwpx_is_text_of_the_document(self, tmp_path):
        """It is drawn on every page of its section. No reader writes it today: the count says so and no one has to remember."""
        master = ('<hm:masterPage xmlns:hm="http://www.hancom.co.kr/hwpml/2011/master-page" '
                  'xmlns:hp="http://www.hancom.co.kr/hwpml/2011/paragraph"><hp:subList><hp:p><hp:run><hp:t>대외비</hp:t></hp:run></hp:p>'
                  "</hp:subList></hm:masterPage>")
        path = tmp_path / "a.hwpx"
        path.write_bytes(builder.zip_with(builder.hwpx_bytes(), add={"Contents/masterpage0.xml": master}))

        assert ("Contents/masterpage0.xml", [("대외비", "masterPage/subList/p/run/t")]) in source_units(path)


class TestHwpSource:
    """An HWP 5 file states its text in the PARA_TEXT records of its section streams, read here without following a control."""

    def test_the_paragraphs_of_the_records_with_the_controls_they_lie_under(self, tmp_path):
        path = tmp_path / "a.hwp"
        path.write_bytes(_fake_hwp(None, flags=1, sections=[_sample_section()]))

        units = source_units(path)

        assert units[:3] == [("BodyText/Section0", [("1. 개요 ", "body")]), ("BodyText/Section0", [("본문입니다 ", "body")]),
                             ("BodyText/Section0", [("각주 내용 ", "fn")])]
        assert ("BodyText/Section0", [("분류 ", "tbl")]) in units
        assert run(tmp_path)["documents"]["a.hwp"]["missing"] == []

    def test_a_paragraph_under_a_control_no_reader_knows_is_counted(self, tmp_path):
        """What reading the records flat is for: the reader follows the controls it knows, the count does not."""
        record = builder.hwp_record
        section = (record(TAG_PARA_HEADER, 0, builder.hwp_para_header()) + record(TAG_PARA_TEXT, 1, builder.hwp_text("본문", (11, "zzzz")))
                   + record(TAG_CTRL_HEADER, 1, builder.hwp_ctrl("zzzz")) + record(TAG_LIST_HEADER, 2, b"\x01\x00" + b"\x00" * 6)
                   + record(TAG_PARA_HEADER, 2, builder.hwp_para_header()) + record(TAG_PARA_TEXT, 3, builder.hwp_text("모르는 개체 안의 글"))
                   + record(TAG_PARA_HEADER, 0, builder.hwp_para_header()) + record(TAG_PARA_TEXT, 1, builder.hwp_text("다음 문단")))
        path = tmp_path / "a.hwp"
        path.write_bytes(_fake_hwp(None, sections=[section]))

        assert source_units(path) == [("BodyText/Section0", [("본문 ", "body")]), ("BodyText/Section0", [("모르는 개체 안의 글 ", "zzzz")]),
                                      ("BodyText/Section0", [("다음 문단 ", "body")])]

    def test_a_tab_parts_two_words_and_a_field_does_not(self):
        tab = struct.pack("<H", 9) + b"\x00" * 12 + struct.pack("<H", 9)
        field = builder.hwp_text((3, "%clk"))[:-2]

        assert _hwp_text("가나".encode("utf-16-le") + tab + "다라".encode("utf-16-le")) == "가나 다라"
        assert _hwp_text("가나".encode("utf-16-le") + field + "다라".encode("utf-16-le") + struct.pack("<H", 4) + b"\x00" * 12
                         + struct.pack("<H", 4)) == "가나다라"

    def test_a_file_whose_body_is_encrypted_states_no_text(self, tmp_path):
        (tmp_path / "a.hwp").write_bytes(_fake_hwp(None, flags=0b10, sections=[_sample_section()]))
        (tmp_path / "b.hwp").write_bytes(_fake_hwp(None, flags=0b100, preview="미리보기 본문"))
        (tmp_path / "c.hwp").write_bytes(b"not a compound file")

        assert {entry["status"] for entry in run(tmp_path)["documents"].values()} == {"source not read"}


class TestHangulSamples:
    def test_a_sample_is_named_by_its_place(self):
        assert sample_name("hwp2hwpx", Path("table/from.hwp")) == "hwp2hwpx__table.hwp"
        assert sample_name("hwplib", Path("basic/a b.HWP")) == "hwplib__basic__a_b.hwp"


class TestCompareText:
    UNITS = [("section0.xml", [("배지를 제작하여 미국" + chr(0xF09E) + "영국 인사들이 동참", "sec/p/run/t")])]

    def test_a_word_that_is_not_written_is_missing_with_its_place(self):
        result = compare_text(self.UNITS, "배지를 제작하여 인사들이 동참")

        assert result["words"] == 6
        assert result["missing"] == [["미국", "section0.xml", "sec/p/run/t"], ["영국", "section0.xml", "sec/p/run/t"]]

    def test_two_words_written_as_one_are_joined_though_none_is_missing(self):
        """What removing the private-use middle dot of a Hangul file did: every word was there."""
        result = compare_text(self.UNITS, "배지를 제작하여 미국영국 인사들이 동참")

        assert result["missing"] == []
        assert result["joined"] == [["미국" + chr(0xF09E) + "영국", "section0.xml", "sec/p/run/t"]]
        assert compare_text(self.UNITS, "배지를 제작하여 미국·영국 인사들이 동참")["joined"] == []

    def test_the_private_use_characters_of_the_source_are_counted(self):
        """A symbol that stands alone is no word and joins none, so only this number points at it."""
        units = [("section0.xml", [(" " + chr(0xF06D) + " (일 시) 2026.11.20.", "sec/p/run/t")])]

        result = compare_text(units, "(일 시) 2026.11.20.")

        assert (result["missing"], result["joined"], result["private_use"]) == ([], [], 1)

    def test_a_mark_written_inside_a_word_does_not_make_it_missing(self):
        units = [("document.xml", [("CO", "p/r/t"), ("2", "p/r/t"), (" 배출량", "p/r/t")])]

        assert compare_text(units, "CO<sub>2</sub> 배출량")["missing"] == []
        assert compare_text(units, "CO[^1]2 배출량")["missing"] == []

    def test_words_the_source_itself_writes_as_one_or_parts_only_a_soft_hyphen_are_not_joined(self):
        soft = [("document.xml", [("docu" + chr(0xAD) + "ment", "p/r/t")])]
        twice = [("document.xml", [("가격 인상", "p/r/t")]), ("document.xml", [("가격인상", "p/r/t")])]

        assert compare_text(soft, "document")["joined"] == []
        assert compare_text(twice, "가격인상 가격인상")["joined"] == []


class TestBaseline:
    def test_a_baseline_keeps_counts_and_no_word_of_a_document(self, tmp_path):
        _hwpx(tmp_path, "<hp:p><hp:run><hp:t>비밀 계약 금액</hp:t></hp:run></hp:p>")
        card = run(tmp_path)
        card["documents"]["a.hwpx"]["missing"].append(["비밀", "Contents/section0.xml", "p/run/t"])

        kept = json.dumps(_counts(card), ensure_ascii=False)

        assert "비밀" not in kept
        assert _counts(card)["documents"]["a.hwpx"] == {"status": "converted", "words": 3, "missing": 1, "joined": 0,
                                                           "private_use": 0}

    def test_a_document_that_loses_more_than_its_baseline_fails_the_run(self, tmp_path, capsys):
        folder = tmp_path / "documents"
        folder.mkdir()
        _hwpx(folder, "<hp:p><hp:run><hp:t>첫째 둘째 셋째</hp:t></hp:run></hp:p>")
        baseline = tmp_path / "baseline.json"

        assert main(["--documents", str(folder), "--baseline", str(baseline), "--update-baseline"]) == 0
        assert main(["--documents", str(folder), "--baseline", str(baseline)]) == 0
        card = run(folder)
        card["documents"]["a.hwpx"]["missing"].append(["둘째", "Contents/section0.xml", "p/run/t"])
        lines, worse = compare(card, json.loads(baseline.read_text(encoding="utf-8")))

        assert worse
        assert "  a.hwpx: missing 0 -> 1" in lines
        assert "1 of 1 documents converted, 3 words: 0 missing, 0 joined; 0 private-use characters" in capsys.readouterr().out

    def test_a_document_cut_at_the_character_limit_is_not_compared(self, tmp_path, monkeypatch):
        """Its last words are missing because the caller's limit says so, not because a reader lost them."""
        from evals import recall
        from opendockit import ConvertOptions

        path = _hwpx(tmp_path, "".join(f"<hp:p><hp:run><hp:t>문단 {n} 의 글</hp:t></hp:run></hp:p>" for n in range(40)))
        monkeypatch.setattr(recall, "OPTIONS", ConvertOptions(max_chars=100))

        assert recall._recall(str(path)) == ("a.hwpx", {"status": "cut at the character limit"})

    def test_a_document_that_is_refused_or_is_no_package_is_named_by_what_happened(self, tmp_path):
        (tmp_path / "a.hwpx").write_bytes(builder.hwpx_bytes(encrypted=True))
        (tmp_path / "b.docx").write_bytes(b"not a package")

        documents = run(tmp_path)["documents"]

        assert documents["a.hwpx"]["status"].startswith("refused (")
        assert documents["b.docx"] == {"status": "source not read"}
