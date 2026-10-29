from dataclasses import dataclass
from typing import Dict, Tuple

SGTIN96_HEADER = 0x30
MAX_SERIAL = (1 << 38) - 1

# partition: (company_prefix_bits, item_ref_bits, company_prefix_digits, item_ref_digits)
PARTITIONS: Dict[int, Tuple[int, int, int, int]] = {
    0: (40, 4, 12, 1),
    1: (37, 7, 11, 2),
    2: (34, 10, 10, 3),
    3: (30, 14, 9, 4),
    4: (27, 17, 8, 5),
    5: (24, 20, 7, 6),
    6: (20, 24, 6, 7),
}

PREFIX_DIGITS_TO_PARTITION = {v[2]: p for p, v in PARTITIONS.items()}


@dataclass(frozen=True)
class SGTIN96Result:
    epc_hex: str
    binary: str
    partition: int
    company_prefix: str
    item_reference: str
    serial: int
    gtin14: str


def digits_only(value: str) -> str:
    return "".join(ch for ch in str(value) if ch.isdigit())


def gtin_check_digit(body: str) -> int:
    """Return GS1 check digit for a GTIN body that excludes the check digit."""
    body = digits_only(body)
    if not body:
        raise ValueError("GTIN body is empty.")
    total = 0
    # Starting at the rightmost body digit: weights alternate 3,1.
    for idx, ch in enumerate(reversed(body)):
        total += int(ch) * (3 if idx % 2 == 0 else 1)
    return (10 - (total % 10)) % 10


def normalize_gtin14(value: str) -> str:
    raw = digits_only(value)
    if len(raw) not in (8, 12, 13, 14):
        raise ValueError("GTIN must contain 8, 12, 13, or 14 digits.")
    return raw.zfill(14)


def validate_gtin(value: str) -> Tuple[bool, str]:
    try:
        gtin14 = normalize_gtin14(value)
    except ValueError as exc:
        return False, str(exc)
    expected = gtin_check_digit(gtin14[:-1])
    actual = int(gtin14[-1])
    if expected != actual:
        return False, f"Invalid GTIN check digit. Expected {expected}, found {actual}."
    return True, gtin14


def derive_fields_from_gtin(gtin: str, company_prefix_digits: int) -> Tuple[str, str, int, str]:
    """
    Convert a GTIN-8/12/13/14 to SGTIN partition fields.

    In SGTIN, Item Reference stores the GTIN indicator digit followed by the
    item-reference digits that come after the GS1 Company Prefix.
    """
    valid, detail = validate_gtin(gtin)
    if not valid:
        raise ValueError(detail)
    gtin14 = detail
    if company_prefix_digits not in PREFIX_DIGITS_TO_PARTITION:
        raise ValueError("Company Prefix digits must be between 6 and 12 for SGTIN-96.")

    partition = PREFIX_DIGITS_TO_PARTITION[company_prefix_digits]
    _, _, cp_digits, item_digits = PARTITIONS[partition]

    body = gtin14[:-1]
    indicator = body[0]
    company_prefix = body[1:1 + cp_digits]
    item_tail = body[1 + cp_digits:]
    item_reference = indicator + item_tail

    if len(company_prefix) != cp_digits:
        raise ValueError("Unable to derive Company Prefix from GTIN.")
    if len(item_reference) != item_digits:
        raise ValueError("Unable to derive Item Reference from GTIN.")
    return company_prefix, item_reference, partition, gtin14


def rebuild_gtin14(company_prefix: str, item_reference: str, partition: int) -> str:
    cp_bits, item_bits, cp_digits, item_digits = PARTITIONS[partition]
    del cp_bits, item_bits
    cp = digits_only(company_prefix).zfill(cp_digits)
    ir = digits_only(item_reference).zfill(item_digits)
    if len(cp) != cp_digits or len(ir) != item_digits:
        raise ValueError("Company Prefix or Item Reference digit length is invalid for the selected partition.")
    body = ir[0] + cp + ir[1:]
    return body + str(gtin_check_digit(body))


def encode_sgtin96(
    gtin: str,
    company_prefix_digits: int,
    serial: int,
    filter_value: int = 3,
) -> SGTIN96Result:
    if not (0 <= int(filter_value) <= 7):
        raise ValueError("Filter must be between 0 and 7.")
    serial = int(serial)
    if not (0 <= serial <= MAX_SERIAL):
        raise ValueError(f"Serial must be between 0 and {MAX_SERIAL:,}.")

    company_prefix, item_reference, partition, gtin14 = derive_fields_from_gtin(
        gtin, int(company_prefix_digits)
    )
    cp_bits, item_bits, cp_digits, item_digits = PARTITIONS[partition]

    cp_value = int(company_prefix)
    item_value = int(item_reference)
    if cp_value >= (1 << cp_bits):
        raise ValueError("Company Prefix does not fit the SGTIN partition bit allocation.")
    if item_value >= (1 << item_bits):
        raise ValueError("Item Reference does not fit the SGTIN partition bit allocation.")

    value = SGTIN96_HEADER
    value = (value << 3) | int(filter_value)
    value = (value << 3) | partition
    value = (value << cp_bits) | cp_value
    value = (value << item_bits) | item_value
    value = (value << 38) | serial

    binary = f"{value:096b}"
    epc_hex = f"{value:024X}"

    rebuilt = rebuild_gtin14(company_prefix, item_reference, partition)
    if rebuilt != gtin14:
        raise ValueError(f"SGTIN round-trip validation failed: {rebuilt} != {gtin14}.")

    return SGTIN96Result(
        epc_hex=epc_hex,
        binary=binary,
        partition=partition,
        company_prefix=company_prefix.zfill(cp_digits),
        item_reference=item_reference.zfill(item_digits),
        serial=serial,
        gtin14=gtin14,
    )
