"""Write-сервис отправлений (Фаза 4) — создание ТТН. Без aiogram.

Порядок **NP-first**: сначала заводим контрагента-получателя и сохраняем ТТН в
НП, и только при успехе пишем `Shipment` в БД. Резерв — **выводимый**: строка со
статусом `created` уже учитывается `reserved_by_sku`, отдельного шага не нужно.
При любой ошибке НП в БД ничего не пишется → ничего не резервируется.

Read-side (список/карточка/отмена) остаётся в `services/shipments.py`; карточку и
гарды переиспользуем оттуда.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.db.models.enums import ShipmentStatus, StockMovementType
from app.db.models.sender_profile import SenderProfile
from app.db.models.user import User
from app.db.repositories import (
    AuditRepository,
    SenderProfileRepository,
    ShipmentItemDraft,
    ShipmentRepository,
    StockMovementRepository,
)
from app.novaposhta import methods
from app.novaposhta.client import NovaPoshtaClient
from app.novaposhta.exceptions import NovaPoshtaError, NovaPoshtaNotFound
from app.novaposhta.schemas import ParcelSpec, RecipientSpec, SenderIdentity, TTNDraft
from app.services import inventory, notifications, shipments
from app.services.exceptions import (
    InsufficientStock,
    SenderDispatchNotConfigured,
    SenderProfileIncomplete,
    SenderProfileNotConfigured,
    SenderProfileNotValidated,
    ShipmentNotFound,
    TtnCancelFailed,
    TtnCreationFailed,
)
from app.services.notifications import Notifier
from app.sheets import InventorySheetReader
from app.utils.sla import shipment_sla_deadline

logger = structlog.get_logger(__name__)


def compute_shipment_fee(items_count: int) -> Decimal:
    if items_count <= 0:
        return Decimal("0")
    return Decimal("20") + Decimal(max(items_count - 1, 0))


def ensure_sender_dispatchable(profile: SenderProfile, settings: Settings) -> None:
    """Убедиться, что у ФОП есть все данные отправителя для `save_ttn` в НП.

    Гейт боевого пути: иначе `create_shipment` собрал бы `SenderIdentity` с пустыми
    `contact_ref`/`phone`/`city_ref`/`warehouse_ref`, и НП отклонила бы ТТН уже на
    save. Порядок проверок — от «ключ» к «профиль» к «системный конфиг»:
    - нет `np_sender_ref` → `SenderProfileNotValidated` (ключ не подтверждён);
    - нет `np_contact_ref`/`sender_phone` → `SenderProfileIncomplete` (дозаполнить ФОП);
    - пуст город/відділення отправителя → `SenderDispatchNotConfigured` (конфиг `.env`).
    """
    if not profile.np_sender_ref:
        raise SenderProfileNotValidated("ключ ФОП не підтверджено в НП")
    if not profile.np_contact_ref or not (profile.sender_phone or "").strip():
        raise SenderProfileIncomplete("немає контакту/телефону відправника")
    warehouse_ref = profile.np_sender_warehouse or settings.np_sender_warehouse_ref
    if not settings.np_sender_city_ref or not warehouse_ref:
        raise SenderDispatchNotConfigured("склад відправника не налаштований")


async def _resolve_sender(
    session: AsyncSession, client: User, sender_profile_id: uuid.UUID | None, settings: Settings
) -> SenderProfile:
    """Найти ФОП клиента (явный или дефолтный) и убедиться, что он готов к відправленню."""
    repo = SenderProfileRepository(session)
    if sender_profile_id is not None:
        profile = await repo.get_by_id(sender_profile_id)
        if profile is None or profile.client_id != client.id:
            raise SenderProfileNotConfigured("ФОП не знайдено")
    else:
        profile = await repo.get_default_for_client(client.id)
        if profile is None:
            raise SenderProfileNotConfigured("ФОП ще не налаштований, зверніться до менеджера")
    ensure_sender_dispatchable(profile, settings)
    return profile


async def resolve_default_sender_id(
    session: AsyncSession, *, client: User, settings: Settings | None = None
) -> uuid.UUID:
    """ID дефолтного ФОП клиента, готового к відправленню — для раннего гейта UI.

    Бросает то же доменное исключение, что и `create_shipment` (NotConfigured /
    NotValidated / Incomplete / DispatchNotConfigured), чтобы вход в FSM и сабмит
    вели себя одинаково. Без побочных эффектов и обращений к НП."""
    profile = await _resolve_sender(session, client, None, settings or get_settings())
    return profile.id


async def _resolve_items(
    session: AsyncSession,
    client: User,
    items: list[tuple[str, int]],
    reader: InventorySheetReader | None,
) -> list[ShipmentItemDraft]:
    """Сверить корзину с остатком (`available`) и собрать позиции с названиями/ценой."""
    snapshot = await inventory.get_inventory_snapshot(session, client=client, reader=reader)
    by_sku = {item.sku: item for item in snapshot}
    # Агрегируем дубли строк корзины по sku — иначе две строки по 6 при остатке 10
    # обе прошли бы проверку (6≤10) и зарезервировали 12 (oversell).
    requested: dict[str, int] = {}
    for sku, qty in items:
        requested[sku] = requested.get(sku, 0) + qty
    drafts: list[ShipmentItemDraft] = []
    for sku, qty in requested.items():
        inv = by_sku.get(sku)
        available = inv.available if inv else 0
        if qty <= 0 or qty > available:
            raise InsufficientStock(sku, qty, available)
        drafts.append(
            ShipmentItemDraft(
                sku=sku, name=inv.name, quantity=qty, category=inv.category, unit_price=inv.price
            )
        )
    if not drafts:
        raise InsufficientStock("—", 0, 0)  # пустая корзина — нечего создавать
    return drafts


async def create_shipment(
    session: AsyncSession,
    *,
    client: User,
    items: list[tuple[str, int]],
    recipient_kind: str,
    recipient_name: str,
    recipient_phone: str,
    recipient_city_ref: str,
    recipient_city_name: str,
    recipient_warehouse_ref: str,
    recipient_warehouse_name: str,
    weight: Decimal,
    size_preset: str,
    description: str,
    insured_amount: Decimal,
    np_client: NovaPoshtaClient,
    payer_type: str = "Recipient",
    payment_method: str = "prepay",
    cod_amount: Decimal | None = None,
    recipient_edrpou: str | None = None,
    sender_profile_id: uuid.UUID | None = None,
    seats_amount: int = 1,
    notifier: Notifier | None = None,
    reader: InventorySheetReader | None = None,
    settings: Settings | None = None,
) -> shipments.ShipmentCard:
    """Создать ТТН (NP-first) и записать `Shipment` + резерв.

    Предусловие: при `payment_method == "cod"` передаётся `cod_amount` (валидирует
    FSM на шаге накладеного платежу).
    """
    shipments._require_active_client(client)
    settings = settings or get_settings()
    profile = await _resolve_sender(session, client, sender_profile_id, settings)
    drafts = await _resolve_items(session, client, items, reader)

    is_cod = payment_method == "cod"
    if is_cod and (cod_amount is None or cod_amount <= 0):
        # Защита от «тихого» сброса COD: ТТН с payment_method=cod без суммы создала
        # бы обычную (предоплатную) посылку — деньги бы не собрали.
        raise TtnCreationFailed("сума накладеного платежу обовʼязкова для накладеного платежу")
    effective_cod = cod_amount if is_cod else None
    api_key = profile.np_api_key  # EncryptedString расшифровывает при чтении

    # NP-first: контрагент-получатель → сохранение ТТН. Любой сбой НП → ничего в БД.
    try:
        recipient_ref, contact_ref = await methods.ensure_recipient(
            np_client,
            api_key=api_key,
            kind=recipient_kind,
            name=recipient_name,
            phone=recipient_phone,
            edrpou=recipient_edrpou,
        )
        draft = TTNDraft(
            sender=SenderIdentity(
                counterparty_ref=profile.np_sender_ref,
                contact_ref=profile.np_contact_ref or "",
                city_ref=settings.np_sender_city_ref,
                warehouse_ref=profile.np_sender_warehouse or settings.np_sender_warehouse_ref,
                phone=profile.sender_phone or "",
            ),
            recipient=RecipientSpec(
                kind=recipient_kind,
                name=recipient_name,
                phone=recipient_phone,
                city_ref=recipient_city_ref,
                warehouse_ref=recipient_warehouse_ref,
                counterparty_ref=recipient_ref,
                contact_ref=contact_ref or "",
                edrpou=recipient_edrpou,
            ),
            parcel=ParcelSpec(weight=weight, seats_amount=seats_amount),
            description=description,
            cost=insured_amount,
            payer_type=payer_type,
            cod_amount=effective_cod,
        )
        result = await methods.save_ttn(np_client, api_key=api_key, draft=draft)
    except NovaPoshtaError as exc:
        raise TtnCreationFailed(str(exc)) from exc

    # Успех НП → запись в БД (последний awaited-шаг). status=created → резерв активен.
    repo = ShipmentRepository(session)
    shipment = await repo.create(
        client_id=client.id,
        sender_profile_id=profile.id,
        recipient_name=recipient_name,
        recipient_phone=recipient_phone,
        recipient_city=recipient_city_name,
        recipient_warehouse=recipient_warehouse_name,
        recipient_kind=recipient_kind,
        payer_type=payer_type,
        payment_method=payment_method,
        cod_amount=effective_cod,
        insured_amount=insured_amount,
        size_preset=size_preset,
        weight=weight,
        status=ShipmentStatus.created,
        description=description,
        ttn_number=result.int_doc_number,
        np_ref=result.ref,
        items=drafts,
    )
    shipment.sla_deadline = shipment_sla_deadline(shipment.created_at, settings=settings)
    shipment.fee_amount = compute_shipment_fee(sum(item.quantity for item in drafts))
    stock_movements = StockMovementRepository(session)
    for draft in drafts:
        await stock_movements.create(
            client_id=client.id,
            shipment_id=shipment.id,
            sku=draft.sku,
            movement_type=StockMovementType.ttn_reserve,
            quantity_delta=-draft.quantity,
            quantity_before=0,
            quantity_after=-draft.quantity,
            comment=f"Резерв під ТТН {result.int_doc_number}",
        )
    await AuditRepository(session).log(
        "shipment_created",
        user_id=client.id,
        affected_entity=f"shipment:{shipment.id}",
        after={
            "ttn_number": result.int_doc_number,
            "items": len(drafts),
            "sla_deadline": shipment.sla_deadline.isoformat() if shipment.sla_deadline else None,
            "fee_amount": str(shipment.fee_amount or Decimal("0")),
        },
    )
    if notifier is not None:
        # Пуш персоналу — best-effort: ТТН в НП уже существует, поэтому сбой пуша
        # НЕ должен ронять create_shipment (иначе откат транзакции осиротит ТТН).
        try:
            await notifications.notify_shipment_created(
                session, notifier, client=client, ttn_number=result.int_doc_number
            )
        except Exception:
            logger.warning("shipment_push_failed", shipment_id=str(shipment.id))
    # Перечитываем с joinedload(items) — иначе `_to_card` ленивой загрузкой
    # коллекции упадёт в async (MissingGreenlet).
    fresh = await repo.get_by_id(shipment.id)
    return shipments._to_card(fresh)


async def _cancel_api_key(session: AsyncSession, shipment) -> str:
    """Ключ НП ФОП отправления (для `InternetDocument.delete`)."""
    profile = await SenderProfileRepository(session).get_by_id(shipment.sender_profile_id)
    if profile is None:
        raise TtnCancelFailed("ФОП відправлення не знайдено")
    return profile.np_api_key  # EncryptedString расшифровывает при чтении


async def cancel_shipment(
    session: AsyncSession,
    *,
    client: User,
    shipment_id: uuid.UUID,
    np_client: NovaPoshtaClient,
) -> shipments.ShipmentCard:
    """Отменить ТТН **NP-first**: сначала `InternetDocument.delete` в НП, и только
    при успехе снимаем статус в БД (резерв выводится из статуса → освободится).

    Иначе (сними мы резерв до удаления в НП) при сбое НП получили бы «живую» ТТН в
    НП с освобождённым резервом → риск oversell. «Уже удалено» в НП
    (`NovaPoshtaNotFound`) — идемпотентный успех. Прочие ошибки НП → `TtnCancelFailed`
    (статус не трогаем, отмену можно повторить).
    """
    shipments._require_active_client(client)
    shipment = await ShipmentRepository(session).get_by_id(shipment_id)
    if shipment is None or shipment.client_id != client.id:
        raise ShipmentNotFound(str(shipment_id))
    # Удаляем в НП только для отменяемых статусов с реальным np_ref; статус-гейт
    # (ShipmentActionForbidden для dispatched и далее) отдаём делегату ниже.
    if shipment.np_ref and shipment.status in shipments.CANCELABLE_STATUSES:
        api_key = await _cancel_api_key(session, shipment)
        try:
            await methods.delete_ttn(np_client, api_key=api_key, doc_ref=shipment.np_ref)
        except NovaPoshtaNotFound:
            logger.info("ttn_already_deleted_in_np", shipment_id=str(shipment.id))
        except NovaPoshtaError as exc:
            raise TtnCancelFailed(str(exc)) from exc
    card = await shipments.cancel_shipment(session, client=client, shipment_id=shipment_id)
    movements = StockMovementRepository(session)
    for item in shipment.items:
        await movements.create(
            client_id=client.id,
            shipment_id=shipment.id,
            sku=item.sku,
            movement_type=StockMovementType.ttn_cancel,
            quantity_delta=item.quantity,
            quantity_before=0,
            quantity_after=item.quantity,
            comment=f"Скасування ТТН {shipment.ttn_number or '—'}",
        )
    return card
