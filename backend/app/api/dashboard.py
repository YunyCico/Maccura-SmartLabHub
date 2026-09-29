from fastapi import APIRouter

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
    return {
        "service_request_count": 0,
        "completed_count": 0,
        "average_satisfaction": "0.00",
        "pending_import_count": 0,
        "recent_tasks": recent_tasks,
    }
