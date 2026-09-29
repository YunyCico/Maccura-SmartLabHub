from __future__ import annotations

from typing import Any

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

from app.services import smartlab_service as service


router = APIRouter(prefix="/data-analysis", tags=["data-analysis"])


class PathsPayload(BaseModel):
    paths: list[str]
    lab: str = ""
    table_type: str = ""


class IdPayload(BaseModel):
    id: str


class IdsPayload(BaseModel):
    ids: list[str]
    delete_files: bool = True


class DatasetUpdatePayload(BaseModel):
    id: str
    lab: str | None = None
    table_type: str | None = None
    note: str | None = None
    enabled: bool | None = None
    sheets: list[dict[str, Any]] | None = None


class PreviewPayload(BaseModel):
    dataset_id: str
    sheet: str = ""
    offset: int = 0
    size: int = 100
    edit: bool = False


class AggregatePayload(BaseModel):
    mode: str = "union"
    sources: list[dict[str, Any]] = []
    base: dict[str, Any] | None = None
    joins: list[dict[str, Any]] = []
    fields: list[str] = []
    filters: list[dict[str, Any]] = []
    add_source_col: bool = True
    dedup: bool = False
    sort: dict[str, Any] = {}
    limit: int = 0
    title: str = ""
    peek: bool = False


class ResultPayload(BaseModel):
    result_id: str
    offset: int = 0
    size: int = 500


class PivotPayload(BaseModel):
    result_id: str
    row_dims: list[str] = []
    col_dim: str = ""
    value: str = ""
    agg: str = "count"
    show_total: bool = True
    drop_blank_keys: bool = False
    fmt: str = "xlsx"
    name: str = ""


class ExportPayload(BaseModel):
    result_id: str
    fmt: str = "xlsx"
    name: str = ""


class ReportPayload(BaseModel):
    result_id: str
    template_id: str
    opts: dict[str, Any] = {}
    name: str = ""


class ReportDeletePayload(BaseModel):
    file: str


class NamesPayload(BaseModel):
    names: list[str]


class ConfigPayload(BaseModel):
    labs: list[str] | None = None
    table_types: list[str] | None = None
    add_source_col: bool | None = None
    mapping: dict[str, Any] | None = None
    ai: dict[str, Any] | None = None
    dingtalk: dict[str, Any] | None = None


class QuestionPayload(BaseModel):
    question: str


class AiAnalyzePayload(BaseModel):
    result_id: str
    question: str


class RawEditPayload(BaseModel):
    dataset_id: str
    sheet: str = ""
    ops: list[dict[str, Any]] = []
    all_sheets: bool = False
    fmt: str = "xlsx"
    name: str = ""
    backup: bool = True


def wrap_error(error: Exception, status_code: int = 400) -> HTTPException:
    return HTTPException(status_code=status_code, detail=str(error))


@router.get("/embed-status")
def embed_status() -> dict[str, Any]:
    try:
        return service.embed_status()
    except Exception as error:
        raise wrap_error(error) from error


@router.post("/embed/start")
def embed_start() -> dict[str, Any]:
    try:
        return {"url": service.start_embed_server(), **service.embed_status()}
    except Exception as error:
        raise wrap_error(error) from error


@router.get("/state")
def get_state() -> dict[str, Any]:
    return service.state()


@router.get("/fields")
def get_fields() -> dict[str, Any]:
    return service.fields()


@router.post("/import-upload")
async def import_upload(files: list[UploadFile] = File(...), lab: str = "", table_type: str = "") -> dict[str, Any]:
    payload = [(file.filename or "", await file.read()) for file in files]
    if not payload:
        raise HTTPException(status_code=400, detail="没有上传文件")
    try:
        return service.import_upload(payload, lab, table_type)
    except Exception as error:
        raise wrap_error(error) from error


@router.post("/import-paths")
def import_paths(payload: PathsPayload) -> dict[str, Any]:
    try:
        return service.import_paths(payload.paths, payload.lab, payload.table_type)
    except Exception as error:
        raise wrap_error(error) from error


@router.post("/dataset/update")
def dataset_update(payload: DatasetUpdatePayload) -> dict[str, Any]:
    try:
        return service.dataset_update(payload.model_dump(exclude_none=True))
    except Exception as error:
        raise wrap_error(error) from error


@router.post("/dataset/delete")
def dataset_delete(payload: IdsPayload) -> dict[str, Any]:
    try:
        return service.dataset_delete(payload.ids, payload.delete_files)
    except Exception as error:
        raise wrap_error(error) from error


@router.post("/dataset/clear")
def dataset_clear() -> dict[str, Any]:
    try:
        return service.dataset_clear()
    except Exception as error:
        raise wrap_error(error) from error


@router.get("/preview")
def preview(dataset_id: str, sheet: str = "") -> dict[str, Any]:
    try:
        return service.preview(dataset_id, sheet)
    except Exception as error:
        raise wrap_error(error) from error


@router.get("/raw-preview")
def raw_preview(dataset_id: str, sheet: str = "", offset: int = 0, size: int = 100, edit: bool = False) -> dict[str, Any]:
    try:
        return service.raw_preview(dataset_id, sheet, offset, size, edit)
    except Exception as error:
        raise wrap_error(error) from error


@router.post("/aggregate")
def aggregate(payload: AggregatePayload) -> dict[str, Any]:
    try:
        return service.aggregate(payload.model_dump(), peek=payload.peek)
    except Exception as error:
        raise wrap_error(error) from error


@router.get("/result")
def get_result(result_id: str, offset: int = 0, size: int = 500) -> dict[str, Any]:
    try:
        return service.get_result(result_id, offset, size)
    except Exception as error:
        raise wrap_error(error) from error


@router.post("/result/profile")
def profile(payload: ResultPayload) -> dict[str, Any]:
    try:
        return service.profile(payload.result_id)
    except Exception as error:
        raise wrap_error(error) from error


@router.post("/result/pivot")
def pivot(payload: PivotPayload) -> dict[str, Any]:
    try:
        return service.pivot(payload.model_dump())
    except Exception as error:
        raise wrap_error(error) from error


@router.post("/export")
def export(payload: ExportPayload) -> dict[str, Any]:
    try:
        return service.export_result(payload.result_id, payload.fmt, payload.name)
    except Exception as error:
        raise wrap_error(error) from error


@router.post("/pivot-export")
def pivot_export(payload: PivotPayload) -> dict[str, Any]:
    try:
        return service.export_pivot(payload.model_dump())
    except Exception as error:
        raise wrap_error(error) from error


@router.get("/report/templates")
def report_templates(result_id: str = "") -> dict[str, Any]:
    try:
        return service.report_templates(result_id)
    except Exception as error:
        raise wrap_error(error) from error


@router.post("/report/generate")
def report_generate(payload: ReportPayload) -> dict[str, Any]:
    try:
        return service.report_generate(payload.model_dump())
    except Exception as error:
        raise wrap_error(error) from error


@router.post("/report/delete")
def report_delete(payload: ReportDeletePayload) -> dict[str, Any]:
    try:
        return service.report_delete(payload.file)
    except Exception as error:
        raise wrap_error(error) from error


@router.get("/exports")
def exports() -> dict[str, Any]:
    try:
        return service.exports()
    except Exception as error:
        raise wrap_error(error) from error


@router.post("/export/delete")
def delete_exports(payload: NamesPayload) -> dict[str, Any]:
    try:
        return service.delete_exports(payload.names)
    except Exception as error:
        raise wrap_error(error) from error


@router.post("/export/clear")
def clear_exports() -> dict[str, Any]:
    try:
        return service.clear_exports()
    except Exception as error:
        raise wrap_error(error) from error


@router.get("/download")
def download(name: str) -> FileResponse:
    try:
        path, _, download_name = service.engine.outgoing_file(name)
    except Exception as error:
        raise wrap_error(error, 404) from error
    return FileResponse(path, filename=download_name)


@router.post("/config")
def update_config(payload: ConfigPayload) -> dict[str, Any]:
    try:
        return service.update_config(payload.model_dump(exclude_none=True))
    except Exception as error:
        raise wrap_error(error) from error


@router.post("/ai/test")
def ai_test() -> dict[str, Any]:
    try:
        return service.ai_test()
    except Exception as error:
        raise wrap_error(error) from error


@router.post("/ai/plan")
def ai_plan(payload: QuestionPayload) -> dict[str, Any]:
    try:
        return service.ai_plan(payload.question)
    except Exception as error:
        raise wrap_error(error) from error


@router.post("/ai/analyze")
def ai_analyze(payload: AiAnalyzePayload) -> dict[str, Any]:
    try:
        return service.ai_analyze(payload.result_id, payload.question)
    except Exception as error:
        raise wrap_error(error) from error


@router.get("/dingtalk/status")
def dingtalk_status() -> dict[str, Any]:
    try:
        return service.dingtalk_status()
    except Exception as error:
        raise wrap_error(error) from error


@router.post("/dingtalk/scan")
def dingtalk_scan() -> dict[str, Any]:
    try:
        return service.dingtalk_scan()
    except Exception as error:
        raise wrap_error(error) from error


@router.post("/raw-edit/apply")
def raw_edit_apply(payload: RawEditPayload) -> dict[str, Any]:
    try:
        return service.raw_edit_apply(payload.model_dump())
    except Exception as error:
        raise wrap_error(error) from error


@router.post("/raw-edit/reset")
def raw_edit_reset(payload: RawEditPayload) -> dict[str, Any]:
    try:
        return service.raw_edit_reset(payload.model_dump())
    except Exception as error:
        raise wrap_error(error) from error


@router.post("/raw-edit/export")
def raw_edit_export(payload: RawEditPayload) -> dict[str, Any]:
    try:
        return service.raw_edit_export(payload.model_dump())
    except Exception as error:
        raise wrap_error(error) from error


@router.post("/raw-edit/save")
def raw_edit_save(payload: RawEditPayload) -> dict[str, Any]:
    try:
        return service.raw_edit_save(payload.model_dump())
    except Exception as error:
        raise wrap_error(error) from error
