"""Price table and cost calculation.

Prices are USD per million tokens, as published by each provider. They live in
code, so changing one is a deploy. Each usage row stores the cost computed at the
time, which keeps history correct after a price changes.
"""

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

PER_MILLION = Decimal(1_000_000)
CENT_OF_A_MICRODOLLAR = Decimal("0.00000001")  # 8 decimal places, like the column


@dataclass(frozen=True)
class Price:
    input_per_mtok: Decimal
    output_per_mtok: Decimal
    #: Some models charge more when the prompt is long. Above this many prompt
    #: tokens the whole request is billed at the long-prompt rates.
    long_prompt_above: int | None = None
    long_input_per_mtok: Decimal | None = None
    long_output_per_mtok: Decimal | None = None

    def __post_init__(self) -> None:
        long_fields = (
            self.long_prompt_above,
            self.long_input_per_mtok,
            self.long_output_per_mtok,
        )
        if any(f is not None for f in long_fields) and any(
            f is None for f in long_fields
        ):
            raise ValueError("long-prompt pricing needs a threshold and both rates")


@dataclass(frozen=True)
class Cost:
    amount: Decimal
    #: False when the model has no price on file; amount is then 0, not a guess.
    priced: bool


D = Decimal

#: Keys are model-name prefixes. Providers return dated names such as
#: "gpt-4.1-mini-2025-04-14", so a request matches the longest key it starts with.
PRICES: dict[str, Price] = {
    # https://developers.openai.com (checked Nov 2026)
    "gpt-4.1-mini": Price(D("0.40"), D("1.60")),
    # Anthropic bills the whole request at the higher rates above 100K prompt tokens.
    "claude-haiku-5-5": Price(
        D("0.10"),
        D("0.50"),
        long_prompt_above=100_000,
        long_input_per_mtok=D("0.50"),
        long_output_per_mtok=D("2.50"),
    ),
    # The built-in fake provider is free.
    "mock": Price(D(0), D(0)),
}


def find_price(model: str, prices: dict[str, Price] | None = None) -> Price | None:
    table = PRICES if prices is None else prices
    name = model.lower()
    best: str | None = None
    for key in table:
        # "gpt-4.1-mini" matches "gpt-4.1-mini-2025-04-14" but not "gpt-4.1-minimal".
        if (name == key or name.startswith(key + "-")) and (
            best is None or len(key) > len(best)
        ):
            best = key
    return table[best] if best is not None else None


def compute_cost(
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    prices: dict[str, Price] | None = None,
) -> Cost:
    price = find_price(model, prices)
    if price is None:
        return Cost(amount=D(0), priced=False)

    input_rate, output_rate = price.input_per_mtok, price.output_per_mtok
    if (
        price.long_prompt_above is not None
        and price.long_input_per_mtok is not None
        and price.long_output_per_mtok is not None
        and prompt_tokens > price.long_prompt_above
    ):
        input_rate, output_rate = price.long_input_per_mtok, price.long_output_per_mtok

    raw = (prompt_tokens * input_rate + completion_tokens * output_rate) / PER_MILLION
    return Cost(
        amount=raw.quantize(CENT_OF_A_MICRODOLLAR, rounding=ROUND_HALF_UP), priced=True
    )
