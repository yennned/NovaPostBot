"""Табличные тесты чистого маппинга полей НП (PR2) — без сети.

Здесь пинятся решения по `InternetDocument.save` (PayerType/Cost/COD/габариты) —
открытый вопрос Фазы 0 изолирован в `mapping.py`, правка одним файлом.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from app.novaposhta.mapping import (
    PAYMENT_METHOD,
    billable_weight_kg,
    money,
    split_full_name,
    to_price_props,
    to_recipient_counterparty_props,
    to_save_props,
)
from app.novaposhta.schemas import (
    ParcelSpec,
    RecipientSpec,
    SenderIdentity,
    TrackingStatus,
    TTNDraft,
)
from app.novaposhta.tracking import dispatch_scan_time

_SENDER = SenderIdentity(
    counterparty_ref="sender-cp",
    contact_ref="sender-contact",
    city_ref="sender-city",
    warehouse_ref="sender-wh",
    phone="380501112233",
)
_RECIPIENT = RecipientSpec(
    kind="person",
    name="Іван Петренко",
    phone="380671234567",
    city_ref="rcpt-city",
    warehouse_ref="rcpt-wh",
    counterparty_ref="rcpt-cp",
    contact_ref="rcpt-contact",
)


def _draft(**over) -> TTNDraft:
    base = {
        "sender": _SENDER,
        "recipient": _RECIPIENT,
        "parcel": ParcelSpec(weight=Decimal("2.5")),
        "description": "Кава мелена",
        "cost": Decimal("500"),
    }
    base.update(over)
    return TTNDraft(**base)


def test_save_props_base_fields():
    props = to_save_props(_draft())
    assert props["PayerType"] == "Recipient"  # дефолт
    assert props["PaymentMethod"] == PAYMENT_METHOD == "Cash"
    assert props["ServiceType"] == "WarehouseWarehouse"
    assert props["CargoType"] == "Cargo"
    assert props["SeatsAmount"] == "1"
    assert props["Weight"] == "2.5"  # строки, не числа
    assert len(props["OptionsSeat"]) == 1
    assert props["Cost"] == "500"
    assert props["Description"] == "Кава мелена"
    # отправитель
    assert props["CitySender"] == "sender-city"
    assert props["Sender"] == "sender-cp"
    assert props["SenderAddress"] == "sender-wh"
    assert props["ContactSender"] == "sender-contact"
    assert props["SendersPhone"] == "380501112233"
    # получатель
    assert props["CityRecipient"] == "rcpt-city"
    assert props["RecipientAddress"] == "rcpt-wh"
    assert props["Recipient"] == "rcpt-cp"
    assert props["ContactRecipient"] == "rcpt-contact"
    assert props["RecipientsPhone"] == "380671234567"


def test_save_props_payer_sender():
    props = to_save_props(_draft(payer_type="Sender"))
    assert props["PayerType"] == "Sender"


def test_save_props_prepay_has_no_afterpayment():
    props = to_save_props(_draft())  # cod_amount=None
    assert "AfterpaymentOnGoodsCost" not in props
    # Класична Післяплата (BackwardDeliveryData) не используется в принципе.
    assert "BackwardDeliveryData" not in props


def test_save_props_cod_uses_afterpayment_on_goods_cost():
    # COD ФОП = «Контроль оплати» → скаляр AfterpaymentOnGoodsCost, НЕ BackwardDeliveryData.
    props = to_save_props(_draft(cod_amount=Decimal("750.50")))
    assert props["AfterpaymentOnGoodsCost"] == "750.50"
    assert "BackwardDeliveryData" not in props


def test_save_props_volume_optional():
    assert "VolumeGeneral" not in to_save_props(_draft())
    props = to_save_props(
        _draft(parcel=ParcelSpec(weight=Decimal("1"), volume_general=Decimal("0.004")))
    )
    assert props["VolumeGeneral"] == "0.004"
    assert props["OptionsSeat"][0]["volumetricVolume"] == "0.004096"
    assert props["OptionsSeat"][0]["volumetricLength"] == "16"


def test_save_props_rejects_partial_dimensions():
    with pytest.raises(ValueError, match="задані разом"):
        to_save_props(
            _draft(
                parcel=ParcelSpec(
                    weight=Decimal("1"),
                    length_cm=20,
                )
            )
        )


def test_save_props_rejects_inconsistent_explicit_volume():
    with pytest.raises(ValueError, match="не відповідає"):
        to_save_props(
            _draft(
                parcel=ParcelSpec(
                    weight=Decimal("1"),
                    volume_general=Decimal("0.005"),
                    length_cm=20,
                    width_cm=20,
                    height_cm=10,
                )
            )
        )


def test_save_props_rejects_non_positive_dimensions():
    with pytest.raises(ValueError, match="більше 0"):
        to_save_props(
            _draft(
                parcel=ParcelSpec(
                    weight=Decimal("1"),
                    length_cm=0,
                    width_cm=20,
                    height_cm=10,
                )
            )
        )


def test_save_props_always_includes_options_seat():
    props = to_save_props(
        _draft(
            parcel=ParcelSpec(
                weight=Decimal("4"),
                seats_amount=2,
                length_cm=20,
                width_cm=30,
                height_cm=10,
            )
        )
    )
    assert len(props["OptionsSeat"]) == 2
    assert props["OptionsSeat"][0] == {
        "volumetricVolume": "0.006",
        "volumetricWidth": "30",
        "volumetricLength": "20",
        "volumetricHeight": "10",
        "weight": "2",
    }


def test_save_props_seats_amount():
    props = to_save_props(_draft(parcel=ParcelSpec(weight=Decimal("10"), seats_amount=3)))
    assert props["SeatsAmount"] == "3"
    assert props["OptionsSeat"][0]["weight"] == "3.333"


def test_save_props_seat_weight_never_rounds_to_zero():
    """Вес места квантуется до 0.001 — очень лёгкая посылка не должна дать 0.

    `OptionsSeat[].weight = 0` при непустом верхнеуровневом `Weight` НП отклоняет,
    поэтому вес места клемпится к минимуму, а не округляется вниз.
    """
    props = to_save_props(_draft(parcel=ParcelSpec(weight=Decimal("0.0004"))))
    assert props["OptionsSeat"][0]["weight"] == "0.001"
    assert props["Weight"] == "0.0004"  # верхнеуровневый вес — как заявлен


def test_save_props_seat_weight_floor_applies_per_seat():
    # 0.002 / 3 = 0.000666… → квантование дало бы 0.001, но на грани делимости
    # важно, что ни одно место не уходит в 0.
    props = to_save_props(_draft(parcel=ParcelSpec(weight=Decimal("0.001"), seats_amount=3)))
    assert all(seat["weight"] == "0.001" for seat in props["OptionsSeat"])


def test_money_formats_decimal_as_string():
    assert money(Decimal("500.00")) == "500.00"
    assert money(Decimal("0.5")) == "0.5"
    assert money(3) == "3"
    assert money("12.30") == "12.30"


def test_money_does_not_leak_float_noise():
    # 199.99 не представимо в float точно — через str() шум не попадает в НП.
    assert money(199.99) == "199.99"
    assert money(1.5) == "1.5"


def test_price_props_without_cod():
    props = to_price_props(
        city_sender_ref="A", city_recipient_ref="B", weight_kg=Decimal("2"), cost=Decimal("300")
    )
    assert props == {
        "CitySender": "A",
        "CityRecipient": "B",
        "Weight": "2",
        "ServiceType": "WarehouseWarehouse",
        "Cost": "300",
        "CargoType": "Cargo",
        "SeatsAmount": "1",
    }


def test_split_full_name_ukrainian_order():
    assert split_full_name("Петренко Іван Богданович") == ("Петренко", "Іван", "Богданович")
    assert split_full_name("Петренко Іван") == ("Петренко", "Іван", "")
    assert split_full_name("Петренко") == ("Петренко", "", "")
    assert split_full_name("  ") == ("", "", "")


def test_recipient_counterparty_props_person():
    props = to_recipient_counterparty_props(
        kind="person", name="Петренко Іван", phone="380671234567"
    )
    assert props == {
        "CounterpartyType": "PrivatePerson",
        "CounterpartyProperty": "Recipient",
        "FirstName": "Іван",
        "MiddleName": "",
        "LastName": "Петренко",
        "Phone": "380671234567",
    }


def test_recipient_counterparty_props_organization():
    props = to_recipient_counterparty_props(
        kind="organization", name="ТОВ Ромашка", phone="380441112233", edrpou="12345678"
    )
    assert props["CounterpartyType"] == "Organization"
    assert props["CounterpartyProperty"] == "Recipient"
    assert props["EDRPOU"] == "12345678"


def test_billable_weight_uses_volumetric_when_box_is_bulky():
    """Пресет «Велика» 40×40×30 = 0.048 м³ → 12 кг объёмных (коэффициент НП 250).

    Клиент ставит 2 кг вручную — НП посчитает по 12, значит и оценка обязана.
    """
    assert billable_weight_kg(
        Decimal("2"), length_cm="40", width_cm="40", height_cm="30"
    ) == Decimal("12")


def test_billable_weight_keeps_actual_when_it_is_heavier():
    # Дефолтные веса пресетов (2/10/30) заведомо больше объёмных (1/4.5/12) —
    # на них поведение не меняется.
    assert billable_weight_kg(
        Decimal("30"), length_cm="40", width_cm="40", height_cm="30"
    ) == Decimal("30")


def test_billable_weight_without_dimensions_is_actual():
    assert billable_weight_kg(Decimal("2.5")) == Decimal("2.5")


def test_billable_weight_scales_with_seats():
    # Каждое место — своя коробка тех же габаритов, объёмный вес суммируется.
    assert billable_weight_kg(
        Decimal("2"), length_cm="40", width_cm="40", height_cm="30", seats_amount=2
    ) == Decimal("24")


def test_billable_weight_rejects_partial_dimensions():
    with pytest.raises(ValueError, match="разом"):
        billable_weight_kg(Decimal("2"), length_cm="40")


def test_price_props_uses_volumetric_weight():
    props = to_price_props(
        city_sender_ref="A",
        city_recipient_ref="B",
        weight_kg=Decimal("2"),
        cost=Decimal("300"),
        length_cm="40",
        width_cm="40",
        height_cm="30",
    )
    assert props["Weight"] == "12"
    # Габариты влияют только на вес — сами в getDocumentPrice не уходят.
    assert "OptionsSeat" not in props
    assert "VolumeGeneral" not in props


def test_price_props_with_cod_adds_redelivery():
    props = to_price_props(
        city_sender_ref="A",
        city_recipient_ref="B",
        weight_kg=1,
        cost=100,
        cod_amount=Decimal("250"),
    )
    assert props["RedeliveryCalculate"] == {"CargoType": "Money", "Amount": "250"}


def test_status_code_two_is_deleted_not_confirmed():
    """Код 2 у НП — «Видалено», а не «створено».

    Раньше он вёл в `confirmed`: накладная, удалённая в кабинете НП, навсегда
    оставалась «підтверджена» — висела в очереди менеджера и держала резерв
    склада под посылку, которой уже нет.
    """
    from app.db.models.enums import ShipmentStatus
    from app.novaposhta.schemas import TrackingStatus
    from app.novaposhta.tracking import is_deleted_in_np, map_tracking_status

    status = TrackingStatus(number="20451500870149", status="Видалено", status_code="2", raw={})

    assert is_deleted_in_np(status) is True
    assert map_tracking_status(status) is ShipmentStatus.cancelled


def test_status_code_one_is_not_deleted():
    from app.novaposhta.schemas import TrackingStatus
    from app.novaposhta.tracking import is_deleted_in_np

    status = TrackingStatus(
        number="20451500871350",
        status="Відправник самостійно створив цю накладну",
        status_code="1",
        raw={},
    )

    assert is_deleted_in_np(status) is False


# --- Время сканирования: чем закрывается SLA ------------------------------------


def test_dispatch_scan_time_parses_the_real_np_format():
    """Боевой формат `DateScan` — время впереди даты и без секунд.

    Снят живым пробником (`scripts/e2e/tracking_probe.py`) на боевых ТТН
    2026-08-02: `'20:05 01.08.2026'`. Первая версия кода знала только «разумные»
    написания и не разобрала бы ни одного реального ответа — вердикт SLA молча
    выродился бы в «не знаем» на всех накладных сразу. Формат пиним тестом,
    чтобы эта ошибка не вернулась.
    """
    parsed = dispatch_scan_time(
        TrackingStatus(
            number="1", status="Відправлено", status_code="3", raw={"DateScan": "20:05 01.08.2026"}
        )
    )
    # Киев летом — UTC+3.
    assert parsed == datetime(2026, 8, 1, 17, 5, tzinfo=UTC)


def test_dispatch_scan_time_parses_fallback_formats():
    for text in ("20.06.2026 11:50:00", "20-06-2026 11:50:00", "2026-06-20 11:50:00"):
        parsed = dispatch_scan_time(
            TrackingStatus(
                number="1", status="Відправлено", status_code="3", raw={"DateScan": text}
            )
        )
        assert parsed == datetime(2026, 6, 20, 8, 50, tzinfo=UTC), text


def test_dispatch_scan_time_is_none_without_field():
    assert (
        dispatch_scan_time(
            TrackingStatus(number="1", status="Відправлено", status_code="3", raw={})
        )
        is None
    )


def test_dispatch_scan_time_ignores_unparsable_value():
    assert (
        dispatch_scan_time(
            TrackingStatus(
                number="1", status="Відправлено", status_code="3", raw={"DateScan": "хтозна"}
            )
        )
        is None
    )


def test_dispatch_scan_time_never_falls_back_to_date_created():
    """`DateCreated` — момент создания ТТН, то есть СТАРТ отсчёта SLA.

    Подставив её как время отправки, мы признавали бы успевшими все накладные
    подряд, включая реально просроченные. Проверяем, что её не берут даже когда
    другого поля нет.
    """
    assert (
        dispatch_scan_time(
            TrackingStatus(
                number="1",
                status="Відправлено",
                status_code="3",
                raw={"DateCreated": "20.06.2026 09:00:00"},
            )
        )
        is None
    )


# --- Description: белый список НП --------------------------------------------
#
# Живой прогон 2026-08-03 дал 3 отказа `Description is not valid` из 60 ТТН, все
# три — на названиях с `100%`. Символы ниже сняты пробником по боевому
# `InternetDocument.save`, а не выведены из документации: у НП его нет.


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # Символы, на которых прогон реально умер.
        ("Кава Ferarra 100% Arabica Мелена 250 г", "Кава Ferarra 100 відс. Arabica Мелена 250 г"),
        # Прочее отвергнутое НП, у чего есть эквивалент.
        ("Ноутбук 15–17 дюймів", "Ноутбук 15-17 дюймів"),
        ("Коробка 10×20", "Коробка 10x20"),
        ("Ціна 500₴", "Ціна 500 грн"),
        ("Термос 90°", "Термос 90 град"),
        # Отвергнутое без эквивалента — выбрасывается, текст остаётся читаемым.
        ("Кава {преміум} ~ 250 г", "Кава преміум 250 г"),
        ("Кава ☕ 250 г", "Кава 250 г"),
        # Диакритика разлагается, а не выбрасывается: «Cafe» читается, «Caf» нет.
        ("Café Crème", "Cafe Creme"),
        ("Straße 250 г", "Strasse 250 г"),
        # Украинские буквы НП принимает — их разлагать нельзя.
        ("Їжа ґудзик ёлка", "Їжа ґудзик ёлка"),
        # Принимаемая пунктуация не трогается.
        ('Кава "Львівська" (мелена) №1, 250 г/уп.', 'Кава "Львівська" (мелена) №1, 250 г/уп.'),
        # Пустой результат — не пустое поле: НП отвергнет и его.
        ("中文", "Товари"),
        ("", "Товари"),
    ],
)
def test_description_matches_np_whitelist(raw, expected):
    from app.novaposhta.mapping import description

    assert description(raw) == expected


def test_description_collapses_spaces_left_by_dropped_chars():
    """Выброшенный символ не должен оставлять после себя двойной пробел."""
    from app.novaposhta.mapping import description

    assert description("Кава ☕ ☕ 250 г") == "Кава 250 г"


def test_description_truncates_to_np_limit():
    """Срез по лимиту НП, и без хвостового пробела от разрезанного слова."""
    from app.novaposhta.mapping import NP_DESCRIPTION_LIMIT, description

    result = description("Кава " * 100)
    assert len(result) <= NP_DESCRIPTION_LIMIT
    assert result == result.strip()
    # Именно срез, а не «влезло целиком».
    assert len(result) > NP_DESCRIPTION_LIMIT - 10


def test_to_save_props_cleans_description():
    """Чистка обязана стоять на границе с НП, а не только в хендлере.

    Хендлер — не единственный вызывающий: воркер, скрипты и будущий код идут в
    `to_save_props` напрямую, и для них отказ НП выглядел бы необъяснимым.
    """
    props = to_save_props(_draft(description="Кава 100% arabica"))
    assert props["Description"] == "Кава 100 відс. arabica"


def test_description_keeps_chars_np_actually_accepts():
    """Белый список не должен резать больше, чем режет НП.

    Первый вариант списка выбрасывал `&` (7 боевых названий), `#` и украинский
    апостроф `ʼ` — все три НП принимает. Пробник это и поймал; здесь он закреплён.
    """
    from app.novaposhta.mapping import description

    assert description("Кава Bar & Co #1") == "Кава Bar & Co #1"
    assert description("Мʼясо копчене 250 г") == "Мʼясо копчене 250 г"
