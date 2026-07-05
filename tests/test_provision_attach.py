"""Тесты хелпера привязки книги-зеркала (`scripts/provision_sheets._extract_book_id`)."""

from __future__ import annotations

from scripts.provision_sheets import _extract_book_id

_BOOK_ID = "1AbC_dEf-GhIjKlMnOpQrStUvWxYz0123456789"


def test_extract_book_id_from_full_url():
    url = f"https://docs.google.com/spreadsheets/d/{_BOOK_ID}/edit#gid=0"
    assert _extract_book_id(url) == _BOOK_ID


def test_extract_book_id_from_bare_id():
    assert _extract_book_id(_BOOK_ID) == _BOOK_ID
    assert _extract_book_id(f"  {_BOOK_ID}  ") == _BOOK_ID
