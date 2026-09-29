from fastapi import APIRouter, File, HTTPException, UploadFile

from smartlab_core.excel_reader import read_excel_preview

router = APIRouter(prefix="/import", tags=["import"])


@router.post("/preview")
async def preview_import(file: UploadFile = File(...)) -> dict[str, object]:
    if not file.filename:
        raise HTTPException(status_code=400, detail="缺少文件名")

    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="文件内容为空")

    try:
        return {
            "status": "preview_ready",
            "data": read_excel_preview(content, file.filename),
        }
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except Exception as error:
        raise HTTPException(status_code=422, detail=f"文件解析失败：{error}") from error
