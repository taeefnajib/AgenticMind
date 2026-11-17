"""Regenerate published JSON Schemas. Run after changing IR models:

    uv run python scripts/generate_schemas.py
"""

from agenticmind_ir.schema import published_schema_path, render_schema


def main() -> None:
    path = published_schema_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_schema(), encoding="utf-8", newline="\n")
    print(f"wrote {path}")


if __name__ == "__main__":
    main()
