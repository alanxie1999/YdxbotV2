"""Verify required project dependencies."""

import sys


REQUIRED_DEPENDENCIES = (
    ("telethon", "telethon"),
    ("aiohttp", "aiohttp"),
    ("requests", "requests"),
    ("socks", "python-socks"),
)

OPTIONAL_DEPENDENCIES = (
    ("dashscope", "dashscope"),
)


def check_import(module_name, package_name=None):
    """Return whether a module can be imported."""
    if package_name is None:
        package_name = module_name
    try:
        __import__(module_name)
        print(f"OK {package_name}")
        return True
    except ImportError as error:
        print(f"FAIL {package_name}: {error}")
        return False


def main():
    """Run dependency checks and return a process exit code."""
    print("Checking dependencies...\n")
    all_ok = True

    for module_name, package_name in REQUIRED_DEPENDENCIES:
        all_ok &= check_import(module_name, package_name)

    print("\nOptional dependencies...")
    for module_name, package_name in OPTIONAL_DEPENDENCIES:
        if not check_import(module_name, package_name):
            print(f"OPTIONAL {package_name}: missing, related provider will stay unavailable.")

    print("\n" + "=" * 40)
    if all_ok:
        print("All core dependencies are available.")
        return 0
    print("Some dependencies are missing.")
    return 1


if __name__ == "__main__":
    # Keep module import side-effect free for tests and update health checks.
    sys.exit(main())
