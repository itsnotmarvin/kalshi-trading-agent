from adapters.base import Side


EXECUTABLE_DIRECTIONS = {"BUY_YES", "BUY_NO"}


def is_executable_direction(direction: str) -> bool:
    return direction in EXECUTABLE_DIRECTIONS


def infer_entry_direction(
    direction: str,
    estimated_prob: float,
    market_price: float,
) -> str | None:
    if direction in EXECUTABLE_DIRECTIONS:
        return direction
    if direction == "WAIT_FOR_ENTRY":
        return "BUY_YES" if estimated_prob >= market_price else "BUY_NO"
    return None


def direction_to_side_and_price(direction: str, yes_price: float) -> tuple[Side, float]:
    if direction == "BUY_YES":
        return Side.YES, yes_price
    if direction == "BUY_NO":
        return Side.NO, 1 - yes_price
    raise ValueError(f"Direction {direction!r} is not directly executable")
