import pytest

import config


@pytest.mark.parametrize("provider", ["claude", "gemini"])
def test_native_providers_are_rejected(provider):
    with pytest.raises(ValueError, match="invalid provider"):
        config._build_preset("native-test", {
            "provider": provider,
            "api_key": "key",
            "model": "model",
        })


@pytest.mark.parametrize("provider", ["relay", "openai"])
def test_openai_compatible_providers_are_allowed(provider):
    preset = config._build_preset("compat-test", {
        "provider": provider,
        "api_key": "key",
        "model": "model",
    })
    assert preset.provider == provider
