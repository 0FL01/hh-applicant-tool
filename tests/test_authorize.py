from __future__ import annotations

import pytest

from hh_applicant_tool.operations.authorize import Operation


def test_resolve_android_device_uses_preferred_fallback_when_default_missing():
    device_name, device = Operation._resolve_android_device(
        {
            "Pixel 7": {"user_agent": "Mozilla/5.0 Android Mobile"},
            "Galaxy S8": {"user_agent": "Mozilla/5.0 Android Mobile"},
        }
    )

    assert device_name == "Pixel 7"
    assert device == {"user_agent": "Mozilla/5.0 Android Mobile"}


def test_resolve_android_device_uses_any_android_mobile_profile_as_last_resort():
    device_name, device = Operation._resolve_android_device(
        {
            "Custom Android": {"user_agent": "Mozilla/5.0 Android Mobile"},
            "Desktop Chrome": {"user_agent": "Mozilla/5.0 X11 Linux x86_64"},
        }
    )

    assert device_name == "Custom Android"
    assert device == {"user_agent": "Mozilla/5.0 Android Mobile"}


def test_resolve_android_device_raises_when_no_android_device_exists():
    with pytest.raises(RuntimeError, match="Android mobile device presets"):
        Operation._resolve_android_device(
            {
                "Desktop Chrome": {
                    "user_agent": "Mozilla/5.0 X11 Linux x86_64"
                }
            }
        )


def test_login_selectors_cover_magritte_form_with_fallbacks():
    # hh.ru в 2026 перешёл на magritte-форму: телефон/почта раздельно.
    assert "magritte-phone-input-national-number-input" in Operation.SEL_PHONE_INPUT
    assert "credential-type-email" in Operation.SEL_EMAIL_TAB
    assert "applicant-login-input-email" in Operation.SEL_EMAIL_INPUT
    # Старые селекторы остаются как fallback.
    assert "login-input-username" in Operation.SEL_LOGIN_INPUT
    assert "login-input-password" in Operation.SEL_PASSWORD_INPUT
    assert 'input[type="password"]' in Operation.SEL_PASSWORD_INPUT
    assert "expand-login-by-password" in Operation.SEL_EXPAND_PASSWORD


@pytest.mark.parametrize(
    ("username", "expected"),
    [
        ("+7 999 123-45-67", "9991234567"),
        ("89991234567", "9991234567"),
        ("79991234567", "9991234567"),
        ("9991234567", "9991234567"),
    ],
)
def test_national_phone_strips_country_code(username, expected):
    # Вызывается только для не-email логинов (guard в _fill_username).
    assert Operation._national_phone(username) == expected
