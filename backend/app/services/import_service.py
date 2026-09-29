from __future__ import annotations

from datetime import datetime
from pathlib import Path
import re
import shutil

from app.core.database import create_import_batch, list_import_batches
from smartlab_core.excel_reader import read_excel_preview


PROJECT_ROOT = Path(__file__).resolve().parents[3]
IMPORT_DIR = PROJECT_ROOT / "data" / "imports"


def _safe_filename(filename: str) -> str:
    cleaned = re.sub(r"[^0-9A-Za-z一-龥._-]+", "_", filename)
    return cleaned or "uploaded_file.xlsx"


def confirm_import(content: bytes, filename: str, imported_by: str = "local-admin") -> dict[str, object]:
    preview = read_excel_preview(content, filename)
    IMPORT_DIR.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    stored_filename = f"{timestamp}_{_safe_filename(filename)}"
    destination = IMPORT_DIR / stored_filename
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_bytes(content)
    shutil.move(str(temporary), destination)

    record = {
        "original_filename": filename,
        "stored_filename": stored_filename,
        "stored_path": str(destination.relative_to(PROJECT_ROOT)),
        "total_rows": int(preview["total_rows"]),
        "sheet_count": int(preview["sheet_count"]),
        "imported_by": imported_by,
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }
    return create_import_batch(record)


def recent_imports() -> list[dict[str, object]]:
    return list_import_batches()
