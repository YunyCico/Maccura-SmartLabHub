from fastapi import APIRouter

from app.services import smartlab_service
from app.services.import_service import recent_imports

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


@router.get("/summary")
def dashboard_summary() -> dict[str, object]:
    imports = recent_imports()
    recent_tasks = [
        {
            "id": item["id"],
            "filename": item["original_filename"],
            "rows": item["total_rows"],
            "sheets": item["sheet_count"],
            "created_at": item["created_at"],
            "status": "已确认",
        }
        for item in imports[:5]
    ]
    try:
        stats = smartlab_service.state()["stats"]
    except Exception:
        stats = {
            "dataset_count": 0,
            "sheet_count": 0,
            "row_count": 0,
            "field_count": 0,
            "result_count": 0,
        }
    return {
        "dataset_count": stats["dataset_count"],
        "sheet_count": stats["sheet_count"],
        "row_count": stats["row_count"],
        "field_count": stats["field_count"],
        "result_count": stats["result_count"],
        "recent_tasks": recent_tasks,
    }
