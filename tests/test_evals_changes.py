"""What a change does to the text of a folder (`evals/changes.py`): two runs of the same folder, compared."""

import gzip
import json

from evals.changes import changed_blocks, compare, convert_folders, main
from tests import builder
from tests.test_hwp import _fake_hwp, _sample_section


_BROKEN = b"PK\x03\x04" + b"\x00" * 40   # the start of a ZIP archive and nothing after it


def _paragraphs(*texts: str) -> str:
    return "".join(f"<hp:p><hp:run><hp:t>{text}</hp:t></hp:run></hp:p>" for text in texts)


def _folder(tmp_path, **documents: bytes):
    folder = tmp_path / "corpus"
    folder.mkdir(exist_ok=True)
    for name, data in documents.items():
        (folder / name.replace("_", ".")).write_bytes(data)
    return folder


def _run(text: str, **more) -> dict:
    return {"documents": {"corpus/a.hwpx": {"text": text, "warnings": [], "seconds": 0.1, **more}}}


class TestConvertFolders:
    def test_every_file_is_named_by_its_folder_and_converted(self, tmp_path):
        folder = _folder(tmp_path, a_hwpx=builder.hwpx_bytes(_paragraphs("첫 문단")), b_hwpx=_BROKEN)

        documents = convert_folders([folder])["documents"]

        assert sorted(documents) == ["corpus/a.hwpx", "corpus/b.hwpx"]
        assert documents["corpus/a.hwpx"]["text"] == "첫 문단"
        assert documents["corpus/a.hwpx"]["warnings"] == []
        assert documents["corpus/b.hwpx"] == {"refused": "CorruptedDocumentError", "seconds": documents["corpus/b.hwpx"]["seconds"]}

    def test_files_in_subfolders_are_read(self, tmp_path):
        folder = _folder(tmp_path)
        (folder / "inner").mkdir()
        (folder / "inner" / "a.hwpx").write_bytes(builder.hwpx_bytes(_paragraphs("안쪽")))

        assert list(convert_folders([folder])["documents"]) == ["corpus/inner/a.hwpx"]


class TestCompare:
    def test_the_same_run_twice_changes_nothing(self):
        lines, refused = compare(_run("가\n\n나"), _run("가\n\n나"))

        assert not refused
        assert "texts changed: 0" in lines
        assert "documents with a word that is gone: 0" in lines

    def test_a_table_that_became_text_is_counted_by_its_kind(self):
        before = "앞\n\n<table>\n<tr><th></th><th>< 요 약 ></th></tr>\n<tr><td>본문</td></tr>\n</table>\n\n뒤"
        lines, _ = compare(_run(before), _run("앞\n\n< 요 약 >\n\n본문\n\n뒤"))

        assert "texts changed: 1" in lines
        assert "      1  an HTML table -> text" in lines
        assert "documents with a word that is gone: 0" in lines
        assert "documents whose headings changed: 0" in lines

    def test_a_word_written_fewer_times_is_not_gone_and_one_written_nowhere_is(self):
        lines, _ = compare(_run("가정에서는\n\n가정에서는\n\n눈을 치운다"), _run("가정에서는\n\n눈을"))

        assert "documents with a word that is gone: 1" in lines
        assert "  corpus/a.hwpx  1 gone: 치운다" in lines

    def test_headings_that_appear_and_go(self):
        lines, _ = compare(_run("# 제목\n\n본문\n\n부록"), _run("제목\n\n본문\n\n## 부록"))

        assert "documents whose headings changed: 1 (1 appeared, 1 went)" in lines
        assert "  corpus/a.hwpx  +1 -1" in lines

    def test_a_document_refused_now_ends_the_run_with_an_error(self):
        after = {"documents": {"corpus/a.hwpx": {"refused": "CorruptedDocumentError", "seconds": 0.1}}}

        lines, refused = compare(_run("본문"), after)

        assert refused
        assert "refused: 0 before, 1 now" in lines
        assert "  REFUSED NOW  corpus/a.hwpx  CorruptedDocumentError" in lines

    def test_a_document_read_now_is_listed_and_is_no_error(self):
        before = {"documents": {"corpus/a.hwpx": {"refused": "CorruptedDocumentError", "seconds": 0.1}}}

        lines, refused = compare(before, _run("본문"))

        assert not refused
        assert "  read now  corpus/a.hwpx  (was CorruptedDocumentError)" in lines

    def test_warnings_that_changed(self):
        lines, _ = compare(_run("본문"), _run("본문", warnings=["No text was extracted."]))

        assert "documents whose warnings changed: 1" in lines

    def test_twins_that_were_alike_and_are_apart(self):
        def run(hwp: str, hwpx: str) -> dict:
            return {"documents": {"corpus/a.hwp": {"text": hwp, "warnings": [], "seconds": 0.1},
                                  "corpus/a.hwpx": {"text": hwpx, "warnings": [], "seconds": 0.1}}}

        lines, _ = compare(run("본문", "본문"), run("본문", "본 문"))

        assert "twins (HWP and HWPX of one name): 1 pairs, 1 alike before, 0 alike now" in lines
        assert "  APART NOW  corpus/a.hwp" in lines

    def test_documents_of_one_run_only_are_counted_and_left_out(self):
        after = _run("본문")
        after["documents"]["corpus/new.hwpx"] = {"text": "새 문서", "warnings": [], "seconds": 0.1}

        lines, _ = compare(_run("본문"), after)

        assert lines[0] == "1 documents in both runs (0 only before, 1 only now)"


class TestChangedBlocks:
    def test_only_the_blocks_that_differ(self):
        assert changed_blocks("가\n\n나\n\n다", "가\n\n나나\n\n다") == [("나", "나나")]

    def test_a_block_taken_out(self):
        assert changed_blocks("가\n\n나\n\n다", "가\n\n다") == [("나", "")]


class TestMain:
    def test_save_then_compare_the_same_folder(self, tmp_path, monkeypatch, capsys):
        folder = _folder(tmp_path, a_hwpx=builder.hwpx_bytes(_paragraphs("첫 문단", "둘째 문단")),
                         a_hwp=_fake_hwp(monkeypatch, sections=[_sample_section()]))
        saved = tmp_path / "before.json.gz"

        assert main(["--documents", str(folder), "--save", str(saved)]) == 0
        with gzip.open(saved, "rt", encoding="utf-8") as handle:
            assert sorted(json.load(handle)["documents"]) == ["corpus/a.hwp", "corpus/a.hwpx"]
        capsys.readouterr()
        assert main(["--documents", str(folder), "--against", str(saved)]) == 0

        output = capsys.readouterr().out
        assert "2 documents in both runs" in output
        assert "texts changed: 0" in output
        assert "twins (HWP and HWPX of one name): 1 pairs, 0 alike before, 0 alike now" in output

    def test_a_document_that_broke_since_ends_with_status_1(self, tmp_path, capsys):
        folder = _folder(tmp_path, a_hwpx=builder.hwpx_bytes(_paragraphs("첫 문단")))
        saved = tmp_path / "before.json.gz"
        main(["--documents", str(folder), "--save", str(saved)])
        (folder / "a.hwpx").write_bytes(_BROKEN)

        assert main(["--documents", str(folder), "--against", str(saved)]) == 1
        assert "REFUSED NOW  corpus/a.hwpx" in capsys.readouterr().out
