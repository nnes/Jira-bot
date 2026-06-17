"""Tests for app.core.pii — PII detection and masking."""
import pytest

from app.core.pii import has_pii, mask_pii


# ── mask_pii — card numbers ────────────────────────────────────────────────────

class TestMaskCardNumber:
    def test_16_digit_visa(self):
        result = mask_pii("Card number: 4111111111111111")
        assert "4111111111111111" not in result
        assert "1111" in result  # last 4 preserved
        assert "****" in result

    def test_16_digit_mastercard(self):
        result = mask_pii("Mastercard 5500005555555559")
        assert "5500005555555559" not in result
        assert "5559" in result

    def test_16_digit_with_spaces(self):
        result = mask_pii("4111 1111 1111 1111")
        assert "4111 1111 1111 1111" not in result
        assert "1111" in result

    def test_16_digit_with_dashes(self):
        result = mask_pii("4111-1111-1111-1111")
        assert "4111-1111-1111-1111" not in result
        assert "1111" in result

    def test_19_digit_card(self):
        result = mask_pii("6304000000000000001")
        assert "6304000000000000001" not in result
        assert "0001" in result

    def test_13_digit_card(self):
        result = mask_pii("4222222222222")
        assert "4222222222222" not in result
        assert "2222" in result


# ── mask_pii — PIN ─────────────────────────────────────────────────────────────

class TestMaskPin:
    def test_pin_with_keyword(self):
        result = mask_pii("Mã PIN: 1234")
        assert "1234" not in result
        assert "[PIN_REDACTED]" in result

    def test_pin_english(self):
        result = mask_pii("pin: 123456")
        assert "123456" not in result
        assert "[PIN_REDACTED]" in result

    def test_standalone_digits_no_context(self):
        # 4-digit number without any PIN keyword should NOT be masked
        result = mask_pii("Order ID: 1234")
        assert "1234" in result


# ── mask_pii — password ────────────────────────────────────────────────────────

class TestMaskPassword:
    def test_password_colon(self):
        result = mask_pii("password: MySecret@123")
        assert "MySecret@123" not in result
        assert "[REDACTED]" in result

    def test_password_equals(self):
        result = mask_pii("Password=abc123!")
        assert "abc123!" not in result
        assert "[REDACTED]" in result

    def test_password_case_insensitive(self):
        result = mask_pii("PASSWORD: hunter2")
        assert "hunter2" not in result
        assert "[REDACTED]" in result


# ── mask_pii — CVV/CVC ────────────────────────────────────────────────────────

class TestMaskCvv:
    def test_cvv_3_digits(self):
        result = mask_pii("CVV: 123")
        assert "123" not in result
        assert "[CVV_REDACTED]" in result

    def test_cvc_4_digits(self):
        result = mask_pii("cvc: 4567")
        assert "4567" not in result
        assert "[CVV_REDACTED]" in result

    def test_cvv_case_insensitive(self):
        result = mask_pii("Cvv 999")
        assert "[CVV_REDACTED]" in result


# ── mask_pii — normal text unchanged ─────────────────────────────────────────

class TestMaskNormalText:
    def test_plain_sentence_unchanged(self):
        text = "Add biometric authentication for E-wallet transactions."
        assert mask_pii(text) == text

    def test_short_number_unchanged(self):
        text = "Priority P3 item, estimated 5 points."
        assert mask_pii(text) == text

    def test_url_unchanged(self):
        text = "See https://jira.example.com/browse/EWL-123 for details."
        assert mask_pii(text) == text

    def test_empty_string(self):
        assert mask_pii("") == ""

    def test_none_like_falsy(self):
        assert mask_pii("") == ""


# ── has_pii ───────────────────────────────────────────────────────────────────

class TestHasPii:
    def test_detects_card_number(self):
        assert has_pii("Card 4111111111111111") is True

    def test_detects_password(self):
        assert has_pii("password: abc") is True

    def test_detects_cvv(self):
        assert has_pii("CVV: 123") is True

    def test_clean_text_returns_false(self):
        assert has_pii("Normal ticket description without PII") is False

    def test_empty_string_returns_false(self):
        assert has_pii("") is False
