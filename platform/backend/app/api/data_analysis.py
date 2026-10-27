from __future__ import annotations

from typing import Any

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

from app.services import lis_service
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


class LisMappingItem(BaseModel):
    source: str
    target_key: str = ""
    target_label: str = ""
    rule: str = ""


class LisConfirmPayload(BaseModel):
    session_id: str
    mapping: list[LisMappingItem]
    header_row: int
    lab: str = ""


class LisPreviewStandardPayload(BaseModel):
    session_id: str
    mapping: list[LisMappingItem]
    header_row: int
    lab: str = ""
    page: int = 1
    page_size: int = 50


def wrap_error(error: Exception, status_code: int = 400) -> HTTPException:
    return HTTPException(status_code=status_code, detail=str(error))


@router.get("/lis/download/{batch_id}")
def lis_download(batch_id: str) -> FileResponse:
    try:
        path, filename = lis_service.output_file(batch_id)
        return FileResponse(path, filename=filename)
    except Exception as error:
        raise wrap_error(error, 404) from error


@router.get("/lis/template")
def lis_template() -> dict[str, Any]:
    try:
        return lis_service.template()
    except Exception as error:
        raise wrap_error(error) from error


# ---------------------------------------------------------------- 标准字段设置
@router.get("/lis/fields")
def lis_fields_get() -> dict[str, Any]:
    """生效的标准字段定义（出厂定义 + 本机自定义）。"""
    try:
        return lis_service.field_definitions_status()
    except Exception as error:
        raise wrap_error(error) from error


@router.post("/lis/fields")
def lis_fields_save(payload: dict[str, Any]) -> dict[str, Any]:
    """保存本机的标准字段定义（可改名 / 改分组 / 改必填 / 改类型 / 新增 / 删除）。"""
    try:
        result = lis_service.save_field_definitions(payload)
        result["status"] = lis_service.field_definitions_status()
        return result
    except Exception as error:
        raise wrap_error(error) from error


@router.post("/lis/fields/reset")
def lis_fields_reset() -> dict[str, Any]:
    """恢复出厂标准字段。"""
    try:
        result = lis_service.reset_field_definitions()
        result["status"] = lis_service.field_definitions_status()
        return result
    except Exception as error:
        raise wrap_error(error) from error


@router.get("/lis/imported-sources")
def lis_imported_sources() -> dict[str, Any]:
    try:
        return {"sources": lis_service.imported_sources()}
    except Exception as error:
        raise wrap_error(error) from error


@router.get("/lis/taxonomy")
def lis_taxonomy() -> dict[str, Any]:
    try:
        return {"items": lis_service.taxonomy_items()}
    except Exception as error:
        raise wrap_error(error) from error


@router.post("/lis/classify")
def lis_classify(payload: dict[str, Any]) -> dict[str, Any]:
    """按检测项目名称 / 项目英文名称批量判定专业与组套。

    前端预览直接调这个接口，规则与后端保存时完全一致，避免两套实现漂移。
    """
    try:
        rows = payload.get("rows")
        if not isinstance(rows, list):
            raise ValueError("rows 必须是数组")
        results = []
        for item in rows:
            if not isinstance(item, dict):
                results.append({lis_service.SPECIALTY_KEY: "", lis_service.SUITE_KEY: ""})
                continue
            results.append(
                lis_service.classify_project(
                    item.get("test_name", ""),
                    item.get("test_en_name", ""),
                )
            )
        return {"results": results}
    except Exception as error:
        raise wrap_error(error) from error


@router.post("/lis/preview")
async def lis_preview(file: UploadFile = File(...), header_row: int | None = None) -> dict[str, Any]:
    try:
        content = await file.read(lis_service.MAX_UPLOAD_BYTES + 1)
        return lis_service.preview_upload(file.filename or "lis.xlsx", content, header_row)
    except Exception as error:
        raise wrap_error(error) from error


@router.post("/lis/preview-imported")
def lis_preview_imported(dataset_id: str, sheet: str = "", header_row: int | None = None) -> dict[str, Any]:
    try:
        return lis_service.preview_imported_dataset(dataset_id, sheet, header_row)
    except Exception as error:
        raise wrap_error(error) from error


@router.post("/lis/confirm")
def lis_confirm(payload: LisConfirmPayload) -> dict[str, Any]:
    try:
        return lis_service.confirm_save(payload.session_id, [item.model_dump() for item in payload.mapping], payload.header_row, payload.lab)
    except Exception as error:
        raise wrap_error(error) from error


@router.post("/lis/preview-standard")
def lis_preview_standard(payload: LisPreviewStandardPayload) -> dict[str, Any]:
    """按当前字段映射生成标准化预览，可翻页浏览全部数据行。

    与保存走同一份构建逻辑（_standard_payload），预览所见即保存所得。
    """
    try:
        return lis_service.preview_standard(
            payload.session_id, [item.model_dump() for item in payload.mapping],
            payload.header_row, payload.lab, payload.page, payload.page_size,
        )
    except Exception as error:
        raise wrap_error(error) from error


@router.post("/lis/export")
def lis_export(payload: LisPreviewStandardPayload) -> FileResponse:
    """按当前字段映射导出标准化 Excel（不写入"已整理"记录）。"""
    try:
        path, filename = lis_service.export_standard(
            payload.session_id, [item.model_dump() for item in payload.mapping], payload.header_row, payload.lab,
        )
        return FileResponse(path, filename=filename)
    except Exception as error:
        raise wrap_error(error) from error


# ---------------------------------------------------------------- TAT
@router.get("/tat/standards")
def tat_standards_get() -> dict[str, Any]:
    """实验室 TAT 标准：生化/免疫/血常规/凝血 × 平诊/急诊。"""
    try:
        return service.tat_standards()
    except Exception as error:
        raise wrap_error(error) from error


@router.post("/tat/standards")
def tat_standards_post(payload: dict[str, Any]) -> dict[str, Any]:
    """保存实验室 TAT 标准。"""
    try:
        return service.tat_standards(payload)
    except Exception as error:
        raise wrap_error(error) from error


@router.post("/tat/analyze")
def tat_analyze_api(payload: dict[str, Any]) -> dict[str, Any]:
    """TAT 分析：主指标 + 四个分段 + 实验室内 TAT + ToTAT + 超标判定。"""
    try:
        return service.tat_analyze_result(payload)
    except Exception as error:
        raise wrap_error(error) from error


@router.get("/lis/saved-batches")
def lis_saved_batches() -> dict[str, Any]:
    """已整理并保存的数据表格列表。"""
    try:
        return {"batches": lis_service.saved_batches()}
    except Exception as error:
        raise wrap_error(error) from error


@router.delete("/lis/saved-batches/{batch_id}")
def lis_delete_saved_batch(batch_id: str) -> dict[str, Any]:
    """删除一条已整理记录（连带删除标准化 Excel 文件）。"""
    try:
        return lis_service.delete_saved_batch(batch_id)
    except Exception as error:
        raise wrap_error(error, 404) from error


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
