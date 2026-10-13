from fastapi import APIRouter, File, HTTPException, UploadFile

from app.services.import_service import confirm_import, recent_imports

router = APIRouter(prefix="/imports", tags=["imports"])


@router.post("/confirm")
async def confirm_uploaded_import(file: UploadFile = File(...)) -> dict[str, object]:
    if not file.filename:
        raise HTTPException(status_code=400, detail="缺少文件名")

    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="文件内容为空")

    try:
        return {"status": "imported", "data": confirm_import(content, file.filename)}
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except Exception as error:
        raise HTTPException(status_code=422, detail=f"导入失败：{error}") from error


@router.get("/recent")
def get_recent_imports() -> dict[str, object]:
    return {"items": recent_imports()}
