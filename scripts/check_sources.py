"""Offline source checks without importing the app, reading secrets or using GPUs."""
import ast
import json
from pathlib import Path
import tomllib

ROOT = Path(__file__).resolve().parents[1]
ALLOWED_REGISTRIES = {
    "https://pypi.org/simple",
    "https://download.pytorch.org/whl/cu130",
}


def main():
    count = 0
    for directory in ("app", "mcp_servers", "scripts", "alembic", "tests"):
        for path in (ROOT / directory).rglob("*.py"):
            ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path.relative_to(ROOT)))
            count += 1
    for path in (ROOT / "config").glob("*.json"):
        json.loads(path.read_text(encoding="utf-8-sig"))
    with (ROOT / "pyproject.toml").open("rb") as stream:
        project = tomllib.load(stream)
    with (ROOT / "uv.lock").open("rb") as stream:
        lock = tomllib.load(stream)
    name = project["project"]["name"]
    if name != "pawhelp" or not any(row["name"] == name for row in lock["package"]):
        raise ValueError("PawHelp package metadata and lockfile must agree")
    registries = {
        row["source"]["registry"]
        for row in lock["package"]
        if isinstance(row.get("source"), dict) and "registry" in row["source"]
    }
    if not registries <= ALLOWED_REGISTRIES:
        raise ValueError(f"Lockfile contains unexpected package registries: {sorted(registries - ALLOWED_REGISTRIES)}")
    print(f"Source syntax: {count} Python files; configuration, registries and package name checked.")


if __name__ == "__main__":
    main()
