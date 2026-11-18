"""通道恢复偏移及续传清单接口。

同时服务三个调用方：
* 数据接入通道：上传数据文件、生成/确认/续传清单、查看恢复偏移；
* 格式解析服务（另一个入口）：生成清单并回写待核对台账、只读取未完成分片生成字段包、核对同步；
* 相邻业务面：读取待核对台账，清单状态变化时联动更新。
"""
from __future__ import annotations

import csv
import io
from typing import Any

from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.services.recovery import (
    STRATEGY_ORIGINAL,
    STRATEGY_REVISION,
    recovery_service,
)

router = APIRouter(prefix="/api/recovery", tags=["通道恢复偏移及续传清单"])


class FileRegisterPayload(BaseModel):
    filename: str
    channel_code: str
    total_shards: int = Field(gt=0)
    shard_size: int = Field(gt=0)
    version: str = "v1"
    revision_of: str | None = None
    content_hint: str = ""
    failed_shard: int | None = None
    failed_line: int | None = None
    fail_ratio: float | None = Field(default=None, ge=0, le=1)


class RevisionPayload(BaseModel):
    failed_shard: int | None = None


class ManifestCreatePayload(BaseModel):
    file_id: str
    strategy: str = STRATEGY_ORIGINAL
    client_token: str | None = None
    revision_file_id: str | None = None
    failed_shards: list[int] | None = None


class ConfirmPayload(BaseModel):
    client_token: str | None = None


class ImportPayload(BaseModel):
    manifest: dict[str, Any]


class CsvImportPayload(BaseModel):
    csv_text: str


def _ok(message: str, **extra: Any) -> dict[str, Any]:
    return {"ok": True, "message": message, **extra}


def _fail(message: str, **extra: Any) -> dict[str, Any]:
    return {"ok": False, "message": message, **extra}


# ---------------------------------------------------------------- 数据文件
@router.get("/files")
def list_files() -> dict[str, Any]:
    return {"items": recovery_service.list_files(), "total": len(recovery_service.list_files())}


@router.post("/files")
def register_file(payload: FileRegisterPayload) -> dict[str, Any]:
    """上传接入通道数据文件并按分片大小生成失败分片记录。"""
    try:
        data_file = recovery_service.register_file(
            filename=payload.filename,
            channel_code=payload.channel_code,
            total_shards=payload.total_shards,
            shard_size=payload.shard_size,
            version=payload.version,
            revision_of=payload.revision_of,
            content_hint=payload.content_hint,
            failed_shard=payload.failed_shard,
            failed_line=payload.failed_line,
            fail_ratio=payload.fail_ratio,
        )
    except ValueError as exc:
        return _fail(str(exc))
    return _ok("数据文件已登记，分片计划与失败偏移已生成", file=data_file)


@router.post("/files/{file_id}/revisions")
def register_revision(file_id: str, payload: RevisionPayload) -> dict[str, Any]:
    """中段失败后供应商上传同文件的新修订版本。"""
    try:
        revision = recovery_service.register_revision(file_id, failed_shard=payload.failed_shard)
    except ValueError as exc:
        return _fail(str(exc))
    return _ok("修订文件已登记，可选择修订版本前置做法重算偏移", file=revision)


# ---------------------------------------------------------------- 续传清单
@router.get("/manifests")
def list_manifests(channel_code: str | None = None) -> dict[str, Any]:
    items = recovery_service.list_manifests(channel_code)
    return {"items": items, "total": len(items)}


@router.get("/manifests/{manifest_id}")
def get_manifest(manifest_id: str) -> dict[str, Any]:
    manifest = recovery_service.get_manifest(manifest_id)
    if manifest is None:
        return _fail(f"续传清单 {manifest_id} 不存在")
    return _ok("ok", manifest=manifest)


@router.post("/manifests")
def create_manifest(payload: ManifestCreatePayload) -> dict[str, Any]:
    """生成通道恢复偏移及续传清单（接入通道与格式解析服务共用入口）。"""
    manifest, message = recovery_service.create_manifest(
        file_id=payload.file_id,
        strategy=payload.strategy,
        client_token=payload.client_token,
        revision_file_id=payload.revision_file_id,
        failed_shards=payload.failed_shards,
    )
    if manifest is None:
        return _fail(message)
    duplicated = "幂等拦截" in message
    return {"ok": not duplicated, "message": message, "manifest": manifest,
            "strategy_options": [
                {"value": STRATEGY_ORIGINAL, "label": "原文件偏移前置"},
                {"value": STRATEGY_REVISION, "label": "修订版本前置"},
            ]}


@router.post("/manifests/{manifest_id}/confirm")
def confirm_manifest(manifest_id: str, payload: ConfirmPayload) -> dict[str, Any]:
    """并发提交两次仅确认一份，另一份拒绝。"""
    manifest, message, accepted = recovery_service.confirm_manifest(
        manifest_id, payload.client_token)
    if manifest is None:
        return _fail(message)
    return {"ok": accepted, "message": message, "manifest": manifest, "accepted": accepted}


@router.post("/manifests/{manifest_id}/resume")
def resume_manifest(manifest_id: str) -> dict[str, Any]:
    """从失败偏移恢复通道导入：只补传未完成分片，已完成分片不重复解析。"""
    manifest, message = recovery_service.resume_manifest(manifest_id)
    if manifest is None:
        return _fail(message)
    return _ok(message, manifest=manifest)


@router.get("/manifests/{manifest_id}/export")
def export_manifest(manifest_id: str) -> StreamingResponse:
    """导出续传清单，下载后可直接再次提交导入。"""
    result = recovery_service.export_rows(manifest_id)
    if result is None:
        return StreamingResponse(
            iter([f"续传清单 {manifest_id} 不存在"]),
            media_type="text/plain; charset=utf-8",
            status_code=404,
        )
    header, rows = result
    output = io.StringIO()
    output.write("﻿")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(header)
    for row in rows:
        writer.writerow(row)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="resume-manifest-{manifest_id}.csv"'},
    )


@router.post("/manifests/import")
def import_manifest(payload: ImportPayload) -> dict[str, Any]:
    """续传清单上传导入与校验；失败文件保留错误位置并逐处给出原因。"""
    manifest, errors = recovery_service.import_manifest(payload.manifest)
    if errors:
        return _fail("导入校验未通过，失败文件已保留错误位置", errors=errors,
                     rejected_manifest=manifest)
    return _ok("续传清单导入校验通过，已进入待核对台账", manifest=manifest)


@router.post("/manifests/import-csv")
def import_manifest_csv(payload: CsvImportPayload) -> dict[str, Any]:
    """导出的 CSV 续传清单可直接再次提交：先还原结构再走同一套导入校验。"""
    try:
        parsed = recovery_service.parse_csv_manifest(payload.csv_text)
    except (KeyError, ValueError) as exc:
        return _fail(f"CSV 清单解析失败：{exc}")
    manifest, errors = recovery_service.import_manifest(parsed)
    if errors:
        return _fail("CSV 导入校验未通过，失败文件已保留错误位置", errors=errors,
                     rejected_manifest=manifest)
    return _ok("CSV 续传清单导入校验通过，可直接再次提交", manifest=manifest)


# ---------------------------------------------------------- 格式解析服务
@router.post("/manifests/{manifest_id}/field-packages")
def generate_field_packages(manifest_id: str) -> dict[str, Any]:
    """格式解析服务按续传清单只读取未完成分片并生成字段包。"""
    manifest, message = recovery_service.generate_field_packages(manifest_id)
    if manifest is None:
        return _fail(message)
    return _ok(message, manifest=manifest)


@router.post("/manifests/{manifest_id}/reconcile")
def reconcile_manifest(manifest_id: str) -> dict[str, Any]:
    """格式解析服务核对清单，并把可用状态同步给数据接入通道。"""
    manifest, message = recovery_service.reconcile_manifest(manifest_id)
    if manifest is None:
        return _fail(message)
    return _ok(message, manifest=manifest)


@router.get("/ledger")
def get_ledger(manifest_id: str | None = None) -> dict[str, Any]:
    """待核对台账：相邻业务面读取后按清单状态联动更新内容。"""
    items = recovery_service.get_ledger(manifest_id)
    return {"items": items, "total": len(items)}
