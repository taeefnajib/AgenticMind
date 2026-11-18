"""Regenerate examples/data/sales.sqlite (deterministic fictional sales data).

uv run python scripts/build_sample_db.py
"""

from pathlib import Path

from agenticmind_nodes_semantic.sample import build_sample_database


def main() -> None:
    path = build_sample_database(Path(__file__).resolve().parents[1] / "examples" / "data" / "sales.sqlite")
    print(f"wrote {path} ({path.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
