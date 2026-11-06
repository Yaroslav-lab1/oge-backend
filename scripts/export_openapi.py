"""Write the live FastAPI OpenAPI 3.1 document as JSON-compatible YAML."""

import json
from pathlib import Path

from app.main import app


def main() -> None:
    target = Path(__file__).resolve().parents[1] / "openapi.yaml"
    target.write_text(
        json.dumps(app.openapi(), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"OpenAPI written to {target}")


if __name__ == "__main__":
    main()
