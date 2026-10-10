"""Synthetic sensitive-looking values for tests. Never real customer data.

Values are generated so validators accept them (check digits computed here).
Secret-shaped strings are assembled at runtime so repository secret scanners
do not flag the test source itself.
"""

from __future__ import annotations


def luhn_check_digit(body: str) -> str:
    total = 0
    for pos, ch in enumerate(reversed(body + "0")):
        d = int(ch)
        if pos % 2 == 1:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
    return str((10 - total % 10) % 10)


def card(prefix: str = "411111111111111") -> str:
    """A Luhn-valid test card number (Visa test range by default)."""
    return prefix + luhn_check_digit(prefix)


def saudi_id(prefix: str = "1012345678") -> str:
    """A 10-digit ID starting with 1 or 2 whose last digit satisfies the de-facto check."""
    body = prefix[:9]
    for last in "0123456789":
        candidate = body + last
        total = 0
        for i, ch in enumerate(candidate):
            d = int(ch)
            if i % 2 == 0:
                d *= 2
                d = d // 10 + d % 10
            total += d
        if total % 10 == 0:
            return candidate
    raise AssertionError("unreachable")


def saudi_iban(bank: str = "80", account: str = "000000608010167519") -> str:
    """A mod-97 valid SA IBAN (24 characters)."""
    bban = bank + account
    numeric = "".join(str(int(ch, 36)) for ch in bban + "SA00")
    check = 98 - int(numeric) % 97
    return f"SA{check:02d}{bban}"


def aws_key() -> str:
    return "AKIA" + "Q7XZ" * 4


def github_token() -> str:
    return "ghp_" + "a1B2" * 9


def pem_private_key() -> str:
    begin = "-----BEGIN " + "RSA PRIVATE KEY-----"
    end = "-----END " + "RSA PRIVATE KEY-----"
    return f"{begin}\nMIIEowIBAAKCAQEA{'x' * 40}\n{end}"


def to_arabic_indic(digits: str) -> str:
    return "".join(chr(0x0660 + int(d)) if d.isdigit() else d for d in digits)


def jwt_like() -> str:
    seg = "eyJ" + "hbGciOiJIUzI1NiJ9"
    return ".".join([seg, "eyJ" + "zdWIiOiIxMjM0In0", "c2lnbmF0dXJl" + "c2ln"])


def openbao_token() -> str:
    return "hv" + "s." + "CAESIJ" + "lU2Fsb3VkZXJ0ZXN0" + "dG9rZW4xMjM0"
