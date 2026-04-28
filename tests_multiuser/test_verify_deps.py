import verify_deps


def test_main_ignores_missing_optional_dependencies(monkeypatch):
    monkeypatch.setattr(
        verify_deps,
        "REQUIRED_DEPENDENCIES",
        (("telethon", "telethon"),),
    )
    monkeypatch.setattr(
        verify_deps,
        "OPTIONAL_DEPENDENCIES",
        (("dashscope", "dashscope"),),
    )

    def fake_check_import(module_name, package_name=None):
        return module_name != "dashscope"

    monkeypatch.setattr(verify_deps, "check_import", fake_check_import)

    assert verify_deps.main() == 0


def test_main_fails_when_required_dependency_is_missing(monkeypatch):
    monkeypatch.setattr(
        verify_deps,
        "REQUIRED_DEPENDENCIES",
        (("telethon", "telethon"), ("aiohttp", "aiohttp")),
    )
    monkeypatch.setattr(verify_deps, "OPTIONAL_DEPENDENCIES", ())

    def fake_check_import(module_name, package_name=None):
        return module_name != "aiohttp"

    monkeypatch.setattr(verify_deps, "check_import", fake_check_import)

    assert verify_deps.main() == 1
