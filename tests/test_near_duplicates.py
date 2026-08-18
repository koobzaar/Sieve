from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

from promo_bot.models import Promotion
from promo_bot.store import SQLiteStateStore


class Clock:
    def __init__(self, value: float = 1_000_000) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


def users(store: SQLiteStateStore):
    admin = store.bootstrap_admin(telegram_user_id=101, telegram_chat_id=201)
    token = store.create_invitation(admin.id)
    member = store.redeem_invitation(
        token, telegram_user_id=102, telegram_chat_id=202, chat_type="private"
    )
    return admin, member


def test_equivalent_destination_url_is_per_user_and_lower_price_is_preserved(
    tmp_path,
) -> None:
    store = SQLiteStateStore(tmp_path / "state.db")
    first_user, second_user = users(store)
    first = Promotion(
        id="1",
        source="group-a",
        title="Notebook",
        price=Decimal("3000"),
        url="https://shop.test/item?utm_source=a&sku=1",
    )
    repost = Promotion(
        id="2",
        source="group-b",
        title="Notebook sale",
        price=Decimal("3000"),
        url="https://shop.test/item?sku=1&utm_source=b",
    )
    changed_price = Promotion(
        id="3",
        source="group-c",
        title="Notebook cheaper",
        price=Decimal("2800"),
        url="https://shop.test/item?sku=1",
    )

    assert store.check_near_duplicate(first_user.id, first) is None
    assert store.check_near_duplicate(first_user.id, repost) == "near_duplicate:url"
    assert store.check_near_duplicate(second_user.id, repost) is None
    assert store.check_near_duplicate(first_user.id, changed_price) is None
    store.close()


def test_product_price_fingerprint_requires_strong_equal_model_and_80_percent_overlap(
    tmp_path,
) -> None:
    store = SQLiteStateStore(tmp_path / "state.db")
    admin, _ = users(store)
    original = Promotion(
        id="1",
        source="a",
        title="Notebook Lenovo IdeaPad 3i 82MD0007BR 16GB SSD",
        price=Decimal("2999"),
    )
    repost = Promotion(
        id="2",
        source="b",
        title="Lenovo Notebook IdeaPad 3i 82MD0007BR 16GB SSD promoção",
        price=Decimal("2999"),
    )
    different_model = Promotion(
        id="3",
        source="c",
        title="Notebook Lenovo IdeaPad 3i 82MD0008BR 16GB SSD",
        price=Decimal("2999"),
    )
    different_capacity = Promotion(
        id="4",
        source="d",
        title="Notebook Lenovo IdeaPad 3i 82MD0007BR 8GB SSD",
        price=Decimal("2999"),
    )

    assert store.check_near_duplicate(admin.id, original) is None
    assert (
        store.check_near_duplicate(admin.id, repost)
        == "near_duplicate:product_price"
    )
    assert store.check_near_duplicate(admin.id, different_model) is None
    assert store.check_near_duplicate(admin.id, different_capacity) is None
    store.close()


def test_generic_same_price_promotions_are_never_fuzzy_suppressed(tmp_path) -> None:
    store = SQLiteStateStore(tmp_path / "state.db")
    admin, _ = users(store)
    first = Promotion(id="1", source="a", title="Oferta de supermercado", price=Decimal("99"))
    second = Promotion(id="2", source="b", title="Oferta de supermercado hoje", price=Decimal("99"))
    assert store.check_near_duplicate(admin.id, first) is None
    assert store.check_near_duplicate(admin.id, second) is None
    store.close()


def test_fingerprint_expires_survives_restart_and_records_reason(tmp_path) -> None:
    clock = Clock()
    path = tmp_path / "state.db"
    store = SQLiteStateStore(path, clock=clock)
    admin, _ = users(store)
    first = Promotion(
        id="1", source="a", title="SSD Kingston NV3 SNV3S 1TB", price=Decimal("399")
    )
    repost = Promotion(
        id="2", source="b", title="SSD Kingston NV3 SNV3S 1TB", price=Decimal("399")
    )
    assert store.check_near_duplicate(admin.id, first) is None
    store.close()

    reopened = SQLiteStateStore(path, clock=clock)
    assert (
        reopened.check_near_duplicate(admin.id, repost)
        == "near_duplicate:product_price"
    )
    reopened.record_near_duplicate(admin.id, repost, "near_duplicate:product_price")
    row = reopened._connection.execute(
        "SELECT reason FROM near_duplicate_suppressions ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row["reason"] == "near_duplicate:product_price"
    clock.value += 600
    assert reopened.check_near_duplicate(admin.id, repost) is None
    reopened.close()


def test_concurrent_reposts_create_one_fingerprint_and_one_winner(tmp_path) -> None:
    store = SQLiteStateStore(tmp_path / "state.db")
    admin, _ = users(store)
    promotion = Promotion(
        id="same",
        source="group",
        title="Monitor LG 27GP850-B 27 inch",
        price=Decimal("1999"),
    )

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(
            pool.map(
                lambda _: store.check_near_duplicate(admin.id, promotion),
                range(8),
            )
        )

    assert results.count(None) == 1
    assert results.count("near_duplicate:product_price") == 7
    assert store._connection.execute(
        "SELECT COUNT(*) FROM near_duplicate_fingerprints WHERE user_id=?",
        (admin.id,),
    ).fetchone()[0] == 1
    store.close()


def test_url_reposts_must_keep_beating_the_ten_minute_minimum(tmp_path) -> None:
    store = SQLiteStateStore(tmp_path / "state.db")
    admin, _ = users(store)

    def offer(identifier: str, price: str) -> Promotion:
        return Promotion(
            id=identifier,
            source="telegram" if identifier.startswith("t") else "pelando",
            title="Notebook Lenovo IdeaPad 3i",
            price=Decimal(price),
            url="https://shop.test/notebook?utm_source=" + identifier,
        )

    assert store.check_near_duplicate(admin.id, offer("t1", "3000")) is None
    assert store.check_near_duplicate(admin.id, offer("p2", "3000")) == (
        "near_duplicate:url"
    )
    assert store.check_near_duplicate(admin.id, offer("t3", "3200")) == (
        "near_duplicate:url"
    )
    assert store.check_near_duplicate(admin.id, offer("p4", "2800")) is None
    assert store.check_near_duplicate(admin.id, offer("t5", "2900")) == (
        "near_duplicate:url"
    )
    assert store.check_near_duplicate(admin.id, offer("p6", "2799")) is None
    store.close()


def test_lower_price_passes_in_either_arrival_order_but_higher_never_does(
    tmp_path,
) -> None:
    state = SQLiteStateStore(tmp_path / "state.db")
    admin, member = users(state)
    high = Promotion(
        id="high",
        source="telegram",
        title="Monitor LG 27GP850-B 27 inch",
        price=Decimal("1999"),
    )
    low = Promotion(
        id="low",
        source="pelando",
        title="LG Monitor 27GP850-B 27 inch promocao",
        price=Decimal("1799"),
    )

    assert state.check_near_duplicate(admin.id, high) is None
    assert state.check_near_duplicate(admin.id, low) is None
    assert state.check_near_duplicate(member.id, low) is None
    assert state.check_near_duplicate(member.id, high) == (
        "near_duplicate:product_price"
    )
    state.close()


def test_exact_url_promotes_known_price_over_unknown_and_suppresses_unknown_repost(
    tmp_path,
) -> None:
    state = SQLiteStateStore(tmp_path / "state.db")
    admin, member = users(state)
    unknown = Promotion(
        id="unknown",
        source="telegram",
        title="SSD Kingston NV3 SNV3S 1TB",
        url="https://shop.test/ssd",
    )
    known = Promotion(
        id="known",
        source="pelando",
        title="SSD Kingston NV3 SNV3S 1TB",
        price=Decimal("399"),
        url="https://shop.test/ssd",
    )

    assert state.check_near_duplicate(admin.id, unknown) is None
    assert state.check_near_duplicate(admin.id, known) is None
    assert state.check_near_duplicate(member.id, known) is None
    assert state.check_near_duplicate(member.id, unknown) == "near_duplicate:url"
    state.close()


def test_fuzzy_matching_stays_conservative_when_either_price_is_unknown(
    tmp_path,
) -> None:
    state = SQLiteStateStore(tmp_path / "state.db")
    admin, member = users(state)
    unknown = Promotion(
        id="unknown",
        source="telegram",
        title="SSD Kingston NV3 SNV3S 1TB",
    )
    known = Promotion(
        id="known",
        source="pelando",
        title="Kingston SSD NV3 SNV3S 1TB promocao",
        price=Decimal("399"),
    )

    assert state.check_near_duplicate(admin.id, unknown) is None
    assert state.check_near_duplicate(admin.id, known) is None
    assert state.check_near_duplicate(member.id, known) is None
    assert state.check_near_duplicate(member.id, unknown) is None
    state.close()


def test_ten_minute_boundary_is_exclusive(tmp_path) -> None:
    clock = Clock()
    state = SQLiteStateStore(tmp_path / "state.db", clock=clock)
    admin, member = users(state)
    offer = Promotion(
        id="one",
        source="telegram",
        title="Monitor LG 27GP850-B 27 inch",
        price=Decimal("1999"),
    )
    repost = Promotion(
        id="two",
        source="pelando",
        title="LG Monitor 27GP850-B 27 inch",
        price=Decimal("1999"),
    )

    assert state.check_near_duplicate(admin.id, offer) is None
    clock.value += 599.999
    assert state.check_near_duplicate(admin.id, repost) == (
        "near_duplicate:product_price"
    )
    assert state.check_near_duplicate(member.id, offer) is None
    clock.value += 600
    assert state.check_near_duplicate(member.id, repost) is None
    state.close()
