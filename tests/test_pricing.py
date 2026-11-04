from decimal import Decimal

import pytest

from app.usage.pricing import PRICES, Cost, Price, compute_cost, find_price

D = Decimal


def test_a_plain_model_costs_tokens_times_rate() -> None:
    # 1,000 prompt tokens at $0.40/M plus 500 completion tokens at $1.60/M
    cost = compute_cost("gpt-4.1-mini", 1_000, 500)

    assert cost == Cost(amount=D("0.00120000"), priced=True)


def test_dated_model_names_match_their_family() -> None:
    assert compute_cost("gpt-4.1-mini-2025-04-14", 1_000, 500) == compute_cost(
        "gpt-4.1-mini", 1_000, 500
    )
    assert find_price("Claude-Haiku-5-5") is PRICES["claude-haiku-5-5"]  # any case


def test_a_name_that_merely_starts_the_same_is_not_a_match() -> None:
    assert find_price("gpt-4.1-minimal") is None
    assert find_price("gpt-4.1") is None  # a different, unpriced model


def test_the_longest_matching_prefix_wins() -> None:
    cheap = Price(D("1"), D("1"))
    pricey = Price(D("9"), D("9"))
    table = {"gpt": cheap, "gpt-4": pricey}

    assert find_price("gpt-4-turbo", table) is pricey
    assert find_price("gpt-3", table) is cheap


def test_haiku_is_billed_at_standard_rates_up_to_100k_prompt_tokens() -> None:
    # 10,000 * $0.10/M + 2,000 * $0.50/M
    assert compute_cost("claude-haiku-5-5", 10_000, 2_000).amount == D("0.00200000")
    # Exactly 100,000 is still the standard tier.
    assert compute_cost("claude-haiku-5-5", 100_000, 0).amount == D("0.01000000")


def test_haiku_long_prompts_are_billed_at_the_higher_rates_for_the_whole_request() -> (
    None
):
    # 100,001 * $0.50/M + 1,000 * $2.50/M
    assert compute_cost("claude-haiku-5-5", 100_001, 1_000).amount == D("0.05250050")


def test_a_model_with_no_price_costs_zero_and_says_so() -> None:
    assert compute_cost("some-new-model", 5_000, 5_000) == Cost(D(0), priced=False)


def test_the_mock_provider_is_free_but_still_priced() -> None:
    assert compute_cost("mock-1", 5_000, 5_000) == Cost(D(0), priced=True)


def test_zero_tokens_cost_nothing() -> None:
    assert compute_cost("gpt-4.1-mini", 0, 0).amount == 0


def test_costs_round_half_up_to_eight_decimal_places() -> None:
    table = {"m": Price(D("0.005"), D("0"))}

    # 1 token * $0.005/M = 0.000000005, which rounds up to 0.00000001
    assert compute_cost("m", 1, 0, table).amount == D("0.00000001")


def test_money_is_exact_decimal_not_float() -> None:
    # 3 tokens at $0.10/M is exactly 0.0000003; floats cannot represent that
    amount = compute_cost("claude-haiku-5-5", 3, 0).amount

    assert isinstance(amount, Decimal)
    assert amount == Decimal("0.0000003")
    assert format(amount, "f") == "0.00000030"


def test_long_prompt_pricing_needs_all_three_pieces() -> None:
    with pytest.raises(ValueError, match="long-prompt pricing"):
        Price(D("1"), D("1"), long_prompt_above=100)


def test_every_shipped_price_is_sane() -> None:
    for key, price in PRICES.items():
        assert key == key.lower()
        assert price.input_per_mtok >= 0 and price.output_per_mtok >= 0
