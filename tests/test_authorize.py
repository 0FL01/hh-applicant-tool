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
