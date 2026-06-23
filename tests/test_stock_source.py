"""Тесты фабрики источника остатков Phase 7."""

from __future__ import annotations

import pytest
from app.config import Settings
from app.sheets import CrmStockSource, GoogleSheetsStockSource, build_stock_source


def test_build_stock_source_defaults_to_google_sheets():
    settings = Settings(_env_file=None)
    source = build_stock_source(settings)
    assert isinstance(source, GoogleSheetsStockSource)


def test_build_stock_source_switches_to_crm(monkeypatch):
    monkeypatch.setenv("INVENTORY_SOURCE", "crm")
    settings = Settings(_env_file=None)
    source = build_stock_source(settings)
    assert isinstance(source, CrmStockSource)


def test_crm_stock_source_is_explicit_stub(monkeypatch):
    monkeypatch.setenv("INVENTORY_SOURCE", "crm")
    settings = Settings(_env_file=None)
    source = build_stock_source(settings)
    with pytest.raises(RuntimeError, match="INVENTORY_SOURCE=crm"):
        source.read_stock("client-1")
