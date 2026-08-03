"""Тесты хелперов провижна/привязки книги-зеркала (`scripts/provision_sheets`)."""

from __future__ import annotations

import inspect

from scripts.provision_sheets import (
    _extract_book_id,
    readonly_summary_cells,
    side_summary_cells,
    write_readonly_summary,
)

_BOOK_ID = "1AbC_dEf-GhIjKlMnOpQrStUvWxYz0123456789"


def test_extract_book_id_from_full_url():
    url = f"https://docs.google.com/spreadsheets/d/{_BOOK_ID}/edit#gid=0"
    assert _extract_book_id(url) == _BOOK_ID


def test_extract_book_id_from_bare_id():
    assert _extract_book_id(_BOOK_ID) == _BOOK_ID
    assert _extract_book_id(f"  {_BOOK_ID}  ") == _BOOK_ID


# --- D1: основная панель, «За товаром» поиск по назві -----------------------


def test_side_summary_tovar_resolves_article_via_regexextract():
    rows = side_summary_cells()
    assert len(rows) == 19
    # J13 — комбинированный селектор «Товар», J14 — резолв-артикул из него.
    assert rows[12][0] == "Товар"
    assert rows[13][0] == "Артикул"
    assert "REGEXEXTRACT" in rows[13][1]
    assert "$J$13" in rows[13][1]  # резолв читает селектор


def test_side_summary_tovar_lookups_use_resolved_article_not_selector():
    rows = side_summary_cells()
    # Строки «Назва/Категорія/Кількість/Ціна/Вартість» ищут по резолв-артикулу J14,
    # а не по сырому комбинированному селектору J13.
    for label_idx in range(14, 19):
        formula = rows[label_idx][1]
        assert "$J$14" in formula
        assert "$J$13" not in formula


# --- D2: read-only-панель зеркала, статичный разрез по категориям ------------


def test_readonly_summary_row_per_category_with_totals():
    rows = readonly_summary_cells(["Кава", "Чай"])
    assert rows[6] == ["Категорія", "Позицій", "Одиниць", "Вартість, ₴"]
    assert rows[7] == [
        "Кава",
        '=SUMPRODUCT((C2:C="Кава")*(A2:A<>""))',
        '=SUMPRODUCT((C2:C="Кава")*D2:D)',
        '=SUMPRODUCT((C2:C="Кава")*D2:D*E2:E)',
    ]
    assert rows[8][0] == "Чай"
    # Итоговая строка «Разом» по всему листу.
    assert rows[-1] == ["Разом", "=COUNTA(A2:A)", "=SUM(D2:D)", "=SUMPRODUCT(D2:D;E2:E)"]
    assert len(rows) == 7 + 2 + 1


def test_readonly_summary_empty_categories_still_valid():
    rows = readonly_summary_cells([])
    assert rows[-1][0] == "Разом"
    assert len(rows) == 8  # 7 строк шапки/«Всього» + «Разом», без категорий


def test_readonly_summary_escapes_quotes_in_category_literal():
    rows = readonly_summary_cells(['Кабель "USB"'])
    assert rows[7][1] == '=SUMPRODUCT((C2:C="Кабель ""USB""")*(A2:A<>""))'


def test_readonly_summary_category_metrics_exact_match_not_wildcard():
    # Категория с '*' не должна трактоваться как шаблон (COUNTIF/SUMIF трактуют) —
    # все метрики через SUMPRODUCT с точным '='.
    rows = readonly_summary_cells(["USB*C"])
    assert "COUNTIF" not in rows[7][1] and "SUMIF" not in rows[7][2]
    assert rows[7][1] == '=SUMPRODUCT((C2:C="USB*C")*(A2:A<>""))'


def test_readonly_summary_panel_has_no_dropdowns():
    # Read-only книга: панель зеркала не создаёт ни одного дропдауна. Валидацию она
    # только СНИМАЕТ (setDataValidation без rule при зачистке), поэтому проверяем, что
    # нет типов-условий дропдауна — ни списком, ни диапазоном.
    src = inspect.getsource(write_readonly_summary)
    assert "ONE_OF_LIST" not in src
    assert "ONE_OF_RANGE" not in src


# --- Скоуп книги-зеркала: аккаунт, а не пользователь -------------------------


async def test_accounts_without_view_book_finds_account_and_sheet_key(db_session, monkeypatch):
    """Книга-зеркало принадлежит бизнес-аккаунту, как и лист склада.

    Миграция `d4e5f6a7b8c0` (2026-07-15) снесла `users.stock_sheet_key` и
    `users.stock_view_book_id`, а запрос в скрипте остался по `User` — и с тех пор
    `--client-books` падал `AttributeError` ещё на построении запроса, до первого
    обращения к Google. То есть путь был мёртв полтора месяца и молча.

    Мутация: вернуть выборку по `User` — тест покраснеет `AttributeError`.
    """
    from app.db.models.client_account import ClientAccount
    from scripts import provision_sheets

    account = ClientAccount(name="Магазин Кава", stock_sheet_key="Магазин Кава")
    db_session.add(account)
    await db_session.commit()

    monkeypatch.setattr(provision_sheets, "get_sessionmaker", lambda: _one_shot(db_session))
    rows = await provision_sheets.accounts_without_view_book()

    assert (str(account.id), "Магазин Кава", "Магазин Кава") in rows


async def test_accounts_with_book_are_skipped(db_session, monkeypatch):
    """Аккаунт с уже записанной книгой в выборку не попадает — иначе дубли-сироты."""
    from app.db.models.client_account import ClientAccount
    from scripts import provision_sheets

    done = ClientAccount(name="Вже є", stock_sheet_key="Вже є", stock_view_book_id="book-1")
    db_session.add(done)
    await db_session.commit()

    monkeypatch.setattr(provision_sheets, "get_sessionmaker", lambda: _one_shot(db_session))
    rows = await provision_sheets.accounts_without_view_book()

    assert all(r[0] != str(done.id) for r in rows)


async def test_save_view_book_id_actually_persists(db_session, monkeypatch):
    """Запись должна доезжать до БД.

    Прежний код ставил `user.stock_view_book_id` — немапленный атрибут на ORM-инстансе:
    `commit()` не писал ничего, скрипт рапортовал успех, а следующий прогон выбирал
    тот же аккаунт снова и создавал вторую книгу. Проверяем именно факт записи.

    Мутация: писать в немапленное поле — значение не переживёт `refresh`.
    """
    from app.db.models.client_account import ClientAccount
    from scripts import provision_sheets

    account = ClientAccount(name="Запис", stock_sheet_key="Запис")
    db_session.add(account)
    await db_session.commit()

    monkeypatch.setattr(provision_sheets, "get_sessionmaker", lambda: _one_shot(db_session))
    await provision_sheets._save_view_book_id(str(account.id), "book-42")

    await db_session.refresh(account)
    assert account.stock_view_book_id == "book-42"


def _one_shot(session):
    """Фабрика сессий, отдающая ОДНУ уже открытую тестовую сессию.

    `async with sm() as s` в скрипте закрыл бы настоящую сессию теста, поэтому
    закрытие подавляем — данные должны пережить вызов.
    """
    import contextlib

    @contextlib.asynccontextmanager
    async def _cm():
        yield session

    return lambda: _cm()


def test_view_book_helpers_are_account_scoped():
    """Ни один хелпер книги-зеркала не должен ходить в `User` за складскими полями.

    Дешёвая страховка от повторения: поля живут на `ClientAccount`, и обращение к
    ним через `User` не ловится ни типами, ни линтером — только падением в рантайме.
    """
    source = inspect.getsource(_provision_module())
    for forbidden in ("User.stock_sheet_key", "User.stock_view_book_id"):
        assert forbidden not in source, f"{forbidden} снова в скрипте"


def _provision_module():
    from scripts import provision_sheets

    return provision_sheets


def test_drive_quota_error_is_recognised():
    """Отказ «у SA нет Drive» отличается от прочих сбоев Google.

    Он общий для всех аккаунтов, и повторить его двадцать раз подряд значит
    спрятать причину за шумом. Остальные ошибки — про конкретный аккаунт, и цикл
    обязан идти дальше.

    Мутация: вернуть `return False` — тест покраснеет.
    """
    from scripts.provision_sheets import _is_drive_quota_error

    quota = Exception("APIError: [403]: The user's Drive storage quota has been exceeded.")
    other = Exception("APIError: [500]: Internal error")

    assert _is_drive_quota_error(quota) is True
    assert _is_drive_quota_error(other) is False
