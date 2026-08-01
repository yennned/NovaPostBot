"""Тесты фабрики источника остатков Phase 7."""

from __future__ import annotations

import pytest
from app.config import Settings, get_settings
from app.sheets import (
    CrmStockSource,
    GoogleSheetsStockSource,
    TtlStockSource,
    build_stock_source,
    shared_sheets_client,
)
from app.sheets.runtime import reset_sheets_runtime


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


def _google_source(source):
    """Развернуть цепочку обёрток до самого `GoogleSheetsStockSource`."""
    while not isinstance(source, GoogleSheetsStockSource):
        source = source._source
    return source


def test_default_source_reuses_one_client_per_process():
    """Раньше каждый вызов создавал новый `SheetsClient` = новый OAuth-handshake
    перед каждым чтением склада."""
    assert (
        _google_source(build_stock_source()).client is _google_source(build_stock_source()).client
    )
    assert _google_source(build_stock_source()).client is shared_sheets_client()


def test_default_source_is_shared_cache_across_updates():
    """Кэш чтений обязан быть ОДИН на процесс.

    `ServicesMiddleware` собирает источник на каждый апдейт; кэш, созданный там
    же, не пережил бы и одного экрана — то есть не решал бы задачу, ради которой
    заводится (8 чтений листа на один сценарий ТТН при квоте 60/мин на весь бот).
    """
    first = build_stock_source()
    assert isinstance(first, TtlStockSource)
    assert build_stock_source() is first


def test_zero_ttl_disables_cache(monkeypatch):
    """`STOCK_CACHE_TTL_SECONDS=0` возвращает поведение до правки — без обёртки."""
    monkeypatch.setenv("STOCK_CACHE_TTL_SECONDS", "0")
    get_settings.cache_clear()
    reset_sheets_runtime()
    assert isinstance(build_stock_source(), GoogleSheetsStockSource)


def test_explicit_settings_get_their_own_client():
    """Расшаренный клиент закэширован под `get_settings()` — иначе явная подмена
    конфигурации (воркер, тесты) молча не сработала бы."""
    settings = Settings(_env_file=None)
    assert build_stock_source(settings).client is not shared_sheets_client()


def test_reset_breaks_shared_client_identity():
    first = shared_sheets_client()
    reset_sheets_runtime()
    assert shared_sheets_client() is not first
