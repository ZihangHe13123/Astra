"""Saved-copy readback is confined to the approved directory and a bounded main part."""
import hashlib
import os
from pathlib import Path
from zipfile import ZipFile

import pytest

from agent.runtime.computer_saved_document import ApprovedCopy


pytestmark = pytest.mark.skipif(os.name != "posix", reason="macOS native document path")


def package(path, body, target="word/document.xml"):
    with ZipFile(path, "w") as zip_file:
        zip_file.writestr("_rels/.rels",
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="r" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
            f'Target="{target}"/></Relationships>')
        zip_file.writestr("word/document.xml",
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            f'<w:body>{body}</w:body></w:document>')


def body(text):
    return f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>"


def bound(path: Path):
    result = ApprovedCopy.bind(str(path), "session", "space", ("window", "source.docx"))
    assert result is not None
    return result


def test_readback_preserves_paragraphs_breaks_and_whitespace(tmp_path):
    path = tmp_path / "copy.docx"
    copy = bound(path)
    package(path, body("first"))
    old = copy.version()
    package(path, body("first") + "<w:p/><w:p><w:r><w:t> a </w:t><w:tab/><w:t>b</w:t><w:br/><w:t>三</w:t></w:r></w:p>")
    proof = copy.after_save(old)
    assert proof["status"] == "read_back" and proof["version_changed_since_save_started"]
    assert proof["paragraphs"] == ["first", "", " a \tb\n三"]
    assert proof["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()


def test_unchanged_file_is_not_read_or_published(tmp_path, monkeypatch):
    path = tmp_path / "copy.docx"
    copy = bound(path)
    package(path, body("old text"))
    before = copy.version()
    monkeypatch.setattr(ApprovedCopy, "_read", lambda self: pytest.fail("opened stale document"))
    proof = copy.after_save(before, wait=False)
    assert proof["status"] == "unverified" and proof["missing"] == "saved_file_update"
    assert not {"paragraphs", "sha256"} & proof.keys()


def test_recheck_rejects_old_version_even_if_stat_saw_an_update(tmp_path, monkeypatch):
    path = tmp_path / "copy.docx"
    copy = bound(path)
    package(path, body("old text"))
    before = copy.version()
    # The leaf can be replaced twice between the preliminary stat and the verified open.
    monkeypatch.setattr(ApprovedCopy, "version", lambda self: (1, 2, 3, 4, 5))
    proof = copy.after_save(before, wait=False)
    assert proof["status"] == "unverified" and proof["missing"] == "saved_file_update"
    assert not {"paragraphs", "sha256"} & proof.keys()


@pytest.mark.parametrize("issue", ["symlink", "hardlink", "directory_replaced", "redirected_main", "large",
                                   "unknown_structure", "hidden", "credential", "invalid_package"])
def test_incomplete_or_redirected_readback_never_returns_body(tmp_path, issue):
    directory = tmp_path / "original"
    directory.mkdir()
    path = directory / "copy.docx"
    copy = bound(path)
    package(path, body("before"))
    before = copy.version()
    if issue in {"symlink", "hardlink"}:
        other = tmp_path / "private.docx"
        package(other, body("must not be returned"))
        path.unlink()
        if issue == "symlink":
            path.symlink_to(other)
        else:
            path.hardlink_to(other)
    elif issue == "directory_replaced":
        directory.rename(tmp_path / "moved")
        directory.mkdir()
        package(path, body("wrong directory"))
    elif issue == "invalid_package":
        path.write_bytes(b"not a document")
    else:
        special = {"unknown_structure": "<w:tbl/>", "hidden": "<w:p><w:r><w:rPr><w:vanish/></w:rPr><w:t>x</w:t></w:r></w:p>",
                   "large": body("x" * 4110), "credential": body("password=secret")}.get(issue, body("unrelated"))
        package(path, special, "word/other.xml" if issue == "redirected_main" else "word/document.xml")
    proof = copy.after_save(before)
    assert proof["status"] == "unverified" and "paragraphs" not in proof and "sha256" not in proof
