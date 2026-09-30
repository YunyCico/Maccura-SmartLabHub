from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import threading
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from smartlab_core import smartlab_engine as engine


_initialized = False
_server = None
_server_port = 0


def start_embed_server(preferred: int = 8765) -> str:
    """在平台进程内启动 SmartLabHub 原生服务（界面与接口保持原样）。"""
    global _server, _server_port
    _ensure_initialized()
    if _server is not None:
        return embed_url()

    port = engine.pick_port(preferred)
    if not port:
        raise RuntimeError("没有可用的本地端口来启动 SmartLabHub 界面")

    server = engine.ThreadingHTTPServer(("127.0.0.1", port), engine.Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    _server = server
    _server_port = port
    return embed_url()


def embed_url() -> str:
    return f"http://127.0.0.1:{_server_port}/" if _server_port else ""


def embed_status() -> dict[str, Any]:
    return {
        "running": _server is not None,
        "url": embed_url(),
        "app_version": engine.APP_VERSION,
        "web_dir": engine.WEB_DIR,
    }


def initialize_engine() -> None:
    global _initialized
    if _initialized:
        return

    engine.load_state()
    try:
        before = engine.STATE.get("recognize_version")
        changed = engine.refresh_metadata()
        if before != engine.RECOGNIZE_VERSION or changed:
            engine.save_state()
    except Exception:
        pass
    engine.load_persisted_results()
    _initialized = True


def _ensure_initialized() -> None:
    initialize_engine()


def state() -> dict[str, Any]:
    _ensure_initialized()
    cfg = engine.get_config(public=True)
    datasets = [engine.ds_summary(ds) for ds in engine.STATE.get("datasets", [])]
    total_rows = sum(int(ds.get("total_rows") or 0) for ds in datasets)
    sheet_count = sum(int(ds.get("sheet_count") or 0) for ds in datasets)
    field_count = len({field["key"] for field in engine.field_dictionary()})
    return {
        "app_version": engine.APP_VERSION,
        "base_dir": engine.BASE_DIR,
        "lib_dir": engine.LIB_DIR,
        "out_dir": engine.OUT_DIR,
        "report_dir": engine.REPORT_DIR,
        "datasets": datasets,
        "config": cfg,
        "health": engine.library_health(),
        "stats": {
            "dataset_count": len(datasets),
            "sheet_count": sheet_count,
            "row_count": total_rows,
            "field_count": field_count,
            "result_count": engine.result_count(),
        },
    }


def fields() -> dict[str, Any]:
    _ensure_initialized()
    return {"fields": engine.field_dictionary()}


def import_upload(files: list[tuple[str, bytes]], lab: str = "", table_type: str = "") -> dict[str, Any]:
    _ensure_initialized()
    imported = []
    errors = []
    with tempfile.TemporaryDirectory(prefix="department_platform_upload_") as temp_dir:
        for index, (filename, content) in enumerate(files):
            safe_name = Path(filename or f"upload_{index}.xlsx").name
            temp_path = Path(temp_dir) / f"{index:04d}_{safe_name}"
            temp_path.write_bytes(content)
            try:
                dataset = engine.import_file(str(temp_path), copy=True, lab=lab, table_type=table_type, display_name=safe_name)
                imported.append(engine.ds_summary(dataset))
            except Exception as error:
                errors.append({"filename": safe_name, "error": str(error)})
    engine.save_state()
    return {"imported": imported, "errors": errors, "state": state()}


def import_paths(paths: list[str], lab: str = "", table_type: str = "") -> dict[str, Any]:
    _ensure_initialized()
    imported = []
    errors = []
    for path in paths:
        try:
            dataset = engine.import_file(path, copy=True, lab=lab, table_type=table_type)
            imported.append(engine.ds_summary(dataset))
        except Exception as error:
            errors.append({"path": path, "error": str(error)})
    engine.save_state()
    return {"imported": imported, "errors": errors, "state": state()}


def dataset_update(payload: dict[str, Any]) -> dict[str, Any]:
    _ensure_initialized()
    dataset_id = payload.get("id")
    dataset = next((item for item in engine.STATE["datasets"] if item.get("id") == dataset_id), None)
    if dataset is None:
        raise ValueError("数据源不存在")

    for key in ("lab", "table_type", "note", "enabled"):
        if key in payload:
            dataset[key] = payload[key]

    sheet_updates = {item.get("name"): item for item in payload.get("sheets") or [] if item.get("name")}
    if sheet_updates:
        for sheet in dataset.get("sheets", []):
            update = sheet_updates.get(sheet.get("name"))
            if not update:
                continue
            if "kind" in update:
                sheet["kind"] = update["kind"]
            if "header_rows" in update:
                sheet["header_rows"] = str(update["header_rows"] or "1")
                sheet["user_header"] = bool(update["header_rows"])
        engine.reparse_dataset(dataset)
    engine.save_state()
    return {"dataset": engine.ds_summary(dataset), "state": state()}


def dataset_delete(ids: list[str], delete_files: bool = True) -> dict[str, Any]:
    _ensure_initialized()
    remaining = []
    removed = []
    for dataset in engine.STATE["datasets"]:
        if dataset.get("id") in ids:
            removed.append(dataset.get("name"))
            if delete_files:
                path = engine.ds_abs_path(dataset)
                try:
                    Path(path).unlink(missing_ok=True)
                except Exception:
                    pass
                # 大表模式：连专属 SQLite 一起删
                try:
                    dbp = engine._ds_db_path(dataset, (dataset.get("sheets") or [{}])[0])
                    if dataset.get("large") and os.path.isfile(dbp):
                        os.remove(dbp)
                except Exception:
                    pass
        else:
            remaining.append(dataset)
    engine.STATE["datasets"] = remaining
    engine.save_state()
    return {"removed": removed, "state": state()}


def dataset_clear() -> dict[str, Any]:
    _ensure_initialized()
    engine.STATE["datasets"] = []
    engine.save_state()
    return {"state": state()}


def preview(dataset_id: str, sheet: str) -> dict[str, Any]:
    _ensure_initialized()
    dataset = next((item for item in engine.STATE["datasets"] if item.get("id") == dataset_id), None)
    if dataset is None:
        raise ValueError("数据源不存在")
    table = engine.source_table(dataset, sheet, None, "auto")
    rows = table.get("records")
    if rows is None and table.get("large"):
        sh = next((s for s in dataset.get("sheets", []) if s.get("name") == sheet), None) or \
             (dataset.get("sheets") or [{}])[0]
        rows = engine._large_page(dataset, sh, 0, 500)
    return {
        "columns": table.get("columns", []),
        "rows": (rows or [])[:500],
        "total": table.get("rows", 0),
    }


def raw_preview(dataset_id: str, sheet: str = "", offset: int = 0, size: int = 100, edit: bool = False) -> dict[str, Any]:
    _ensure_initialized()
    if edit:
        return engine.raw_preview_edit(dataset_id, sheet, offset, size)

    dataset = next((item for item in engine.STATE["datasets"] if item.get("id") == dataset_id), None)
    if dataset is None:
        raise ValueError("数据源不存在")
    path = engine.ds_abs_path(dataset)
    names = engine.sheet_names(path)
    active_sheet = sheet if sheet in names else (names[0] if names else "")
    rows = engine.cached_raw(path, active_sheet)
    size = max(1, min(int(size), engine.MAX_PAGE_ROWS))
    offset = max(0, int(offset))
    page = rows[offset:offset + size]
    return {
        "rows": [[engine.cell_to_text(value) for value in row] for row in page],
        "sheets": names,
        "sheet": active_sheet,
        "offset": offset,
        "size": size,
        "total": len(rows),
        "cols": max((len(row) for row in rows), default=0),
    }


def aggregate(payload: dict[str, Any], peek: bool = False) -> dict[str, Any]:
    _ensure_initialized()
    result = engine.aggregate(payload)
    if peek:
        result["rows"] = result.get("rows", [])[:engine.PEEK_ROWS]
        return result

    rid = engine.put_result(result.get("columns", []), result.get("rows", []), {
        "title": payload.get("title") or "汇总结果",
        "mode": result.get("mode"),
        "detail": result.get("detail"),
        "skipped": result.get("skipped"),
        "truncated": result.get("truncated"),
    })
    return {
        "result_id": rid,
        "columns": result.get("columns", []),
        "rows": result.get("rows", [])[:engine.PREVIEW_ROWS],
        "total": len(result.get("rows", [])),
        "detail": result.get("detail", []),
        "skipped": result.get("skipped", []),
        "truncated": result.get("truncated", False),
        "mode": result.get("mode", "union"),
    }


def get_result(result_id: str, offset: int = 0, size: int = 500) -> dict[str, Any]:
    _ensure_initialized()
    result = engine.get_result(result_id)
    if not result:
        raise ValueError("结果不存在或已过期")
    rows = result.get("rows", [])
    size = max(1, min(int(size), engine.MAX_PAGE_ROWS))
    offset = max(0, int(offset))
    return {
        "columns": result.get("columns", []),
        "rows": rows[offset:offset + size],
        "total": len(rows),
        "offset": offset,
        "size": size,
        "meta": result.get("meta", {}),
    }


def profile(result_id: str) -> dict[str, Any]:
    _ensure_initialized()
    result = engine.get_result(result_id)
    if not result:
        raise ValueError("结果不存在或已过期")
    return engine.profile_result(result.get("columns", []), result.get("rows", []))


def pivot(payload: dict[str, Any]) -> dict[str, Any]:
    _ensure_initialized()
    result = engine.get_result(payload.get("result_id", ""))
    if not result:
        raise ValueError("结果不存在或已过期")
    return engine.pivot_result(
        result.get("columns", []),
        result.get("rows", []),
        payload.get("row_dims"),
        payload.get("col_dim", ""),
        payload.get("value", ""),
        payload.get("agg", "count"),
        bool(payload.get("show_total", True)),
        bool(payload.get("drop_blank_keys", False)),
    )


def export_result(result_id: str, fmt: str = "xlsx", name: str = "") -> dict[str, Any]:
    _ensure_initialized()
    path = engine.export_result(result_id, fmt, name)
    return outgoing_file_payload(path)


def export_pivot(payload: dict[str, Any]) -> dict[str, Any]:
    _ensure_initialized()
    result = pivot(payload)
    path = engine._write_table_file(result.get("columns", []), result.get("rows", []), payload.get("name") or "透视结果", payload.get("fmt", "xlsx"))
    return outgoing_file_payload(path)


def report_templates(result_id: str = "") -> dict[str, Any]:
    _ensure_initialized()
    templates = engine.report_templates()
    result = engine.get_result(result_id) if result_id else None
    checks = []
    for template in templates:
        check = engine.report_check(result.get("columns", []) if result else [], template["id"])
        checks.append({"id": template["id"], **check})
    return {"templates": templates, "checks": checks, "reports": engine.report_list()}


def report_generate(payload: dict[str, Any]) -> dict[str, Any]:
    _ensure_initialized()
    return engine.report_generate(payload.get("result_id", ""), payload.get("template_id", ""), payload.get("opts"), payload.get("name", ""))


def report_delete(filename: str) -> dict[str, Any]:
    _ensure_initialized()
    path = Path(engine.REPORT_DIR) / filename
    if path.parent != Path(engine.REPORT_DIR).resolve() or not path.is_file():
        raise ValueError("报告不存在")
    path.unlink()
    return {"reports": engine.report_list()}


def exports() -> dict[str, Any]:
    _ensure_initialized()
    return engine.newest_export(50)


def delete_exports(names: list[str]) -> dict[str, Any]:
    _ensure_initialized()
    engine.delete_exports(names)
    return engine.newest_export(50)


def clear_exports() -> dict[str, Any]:
    _ensure_initialized()
    engine.clear_exports()
    return engine.newest_export(50)


def outgoing_file_payload(path: str) -> dict[str, Any]:
    file_path = Path(path)
    return {
        "file": file_path.name,
        "path": str(file_path),
        "size": file_path.stat().st_size,
        "download_url": f"/api/data-analysis/download?name={file_path.name}",
    }


def update_config(payload: dict[str, Any]) -> dict[str, Any]:
    _ensure_initialized()
    config = engine.STATE.setdefault("config", {})
    for key, value in payload.items():
        if key == "ai" and isinstance(value, dict):
            ai = dict(config.get("ai") or {})
            ai.update(value)
            if not ai.get("api_key"):
                ai.pop("api_key", None)
            config["ai"] = ai
        elif key == "dingtalk" and isinstance(value, dict):
            dingtalk = dict(config.get("dingtalk") or {})
            dingtalk.update(value)
            if not dingtalk.get("token"):
                dingtalk.pop("token", None)
            config["dingtalk"] = dingtalk
        else:
            config[key] = value
    engine.save_state()
    return {"config": engine.get_config(public=True)}


def ai_test() -> dict[str, Any]:
    _ensure_initialized()
    reply = engine.ai_chat([{"role": "user", "content": "请回复 OK"}])
    return {"reply": reply}


def ai_plan(question: str) -> dict[str, Any]:
    _ensure_initialized()
    return engine.ai_plan(question)


def ai_analyze(result_id: str, question: str) -> dict[str, Any]:
    _ensure_initialized()
    return engine.ai_analyze(result_id, question)


def dingtalk_status() -> dict[str, Any]:
    _ensure_initialized()
    return engine.dingtalk_status()


def dingtalk_scan() -> dict[str, Any]:
    _ensure_initialized()
    return {"files": engine.dingtalk_scan()}


def raw_edit_apply(payload: dict[str, Any]) -> dict[str, Any]:
    _ensure_initialized()
    return engine.raw_edit_apply(payload.get("dataset_id", ""), payload.get("sheet", ""), payload.get("ops") or [])


def raw_edit_reset(payload: dict[str, Any]) -> dict[str, Any]:
    _ensure_initialized()
    return engine.raw_edit_reset(payload.get("dataset_id", ""), payload.get("sheet", ""), bool(payload.get("all_sheets", False)))


def raw_edit_export(payload: dict[str, Any]) -> dict[str, Any]:
    _ensure_initialized()
    path = engine.raw_edit_export(payload.get("dataset_id", ""), payload.get("sheet", ""), payload.get("fmt", "xlsx"), payload.get("name", ""))
    return outgoing_file_payload(path)


def raw_edit_save(payload: dict[str, Any]) -> dict[str, Any]:
    _ensure_initialized()
    return engine.raw_edit_save(payload.get("dataset_id", ""), payload.get("sheet", ""), bool(payload.get("backup", True)))
