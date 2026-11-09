import pytest

from veltrix.base_v2_candidate_qualification import (
    address_word, decode_address, decode_uint, selector,
)
from veltrix.base_archive_preflight import PreflightError


def test_selector_is_four_byte_hex():
    value = selector("allPairsLength()")
    assert value.startswith("0x")
    assert len(value) == 10


def test_decode_uint_requires_one_word():
    assert decode_uint("0x" + f"{17:064x}", "value") == 17
    with pytest.raises(PreflightError):
        decode_uint("0x1234", "value")


def test_decode_address_requires_canonical_abi_word():
    address = "0x" + "12" * 20
    encoded = "0x" + "00" * 12 + "12" * 20
    assert decode_address(encoded, "address") == address
    with pytest.raises(PreflightError):
        decode_address("0x" + "ff" * 12 + "12" * 20, "address")


def test_address_word_is_left_padded():
    assert address_word("0x" + "ab" * 20) == "00" * 12 + "ab" * 20


def test_address_word_rejects_malformed_address():
    with pytest.raises(PreflightError):
        address_word("not-an-address")
