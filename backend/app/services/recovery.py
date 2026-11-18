"""通道恢复偏移与续传清单领域服务。

围绕「接入通道数据文件 + 失败分片记录」构建断点续传能力：

* 中断后按失败偏移恢复，只补传未完成分片，已完成分片不再进入解析；
* 续传清单同时记录原文件、通道与偏移，并保留失败分片的错误位置；
* 原文件偏移前置 / 修订版本前置两种做法，生成不同的恢复边界与清单；
* 并发确认只有一份生效，重复提交直接拒绝；
* 恢复完成后每个输入分片有且仅有一份可解析结果（幂等可重放）。

只依赖标准库，数据保存在进程内存中，真实项目可替换为数据库实现。
"""
from __future__ import annotations

import csv
import hashlib
import io
import threading
import uuid
from datetime import datetime
from typing import Any

from app.store import store

MANIFEST_STATUSES = ["待核对", "待续传", "续传中", "已完成", "已拒绝", "校验失败"]
SHARD_DONE = "已完成"
SHARD_TODO = "未完成"
SHARD_FAILED = "失败"
SHARD_SKIPPED = "跳过"
LEDGER_TODO = "待核对"
LEDGER_DONE = "已核对"

# 两种恢复做法：取舍不同，恢复偏移与续传清单也不同
STRATEGY_ORIGINAL = "original_first"   # 原文件身份前置：按原文件失败偏移决定续传边界
STRATEGY_REVISION = "revision_first"   # 修订版本前置：切换修订文件并重新计算偏移
STRATEGY_LABELS = {
    STRATEGY_ORIGINAL: "原文件偏移前置",
    STRATEGY_REVISION: "修订版本前置",
}


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _checksum(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


class RecoveryService:
    """续传清单的状态机与全部业务规则都收敛在这一个类里。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._files: dict[str, dict[str, Any]] = {}
        self._manifests: dict[str, dict[str, Any]] = {}
        # 客户端提交令牌 -> 清单编号，保证并发提交只确认一份
        self._tokens: dict[str, str] = {}
        # (文件编号, 分片序号) -> 解析结果编号，保证每个分片只有一份可解析结果
        self._parse_results: dict[tuple[str, int], str] = {}
        self._seed_demo_scenario()

    # ------------------------------------------------------------------ 数据文件
    def register_file(
        self,
        *,
        filename: str,
        channel_code: str,
        total_shards: int,
        shard_size: int,
        version: str = "v1",
        revision_of: str | None = None,
        content_hint: str = "",
        failed_shard: int | None = None,
        failed_line: int | None = None,
        fail_ratio: float | None = None,
    ) -> dict[str, Any]:
        """登记接入通道数据文件并生成分片计划；可顺带记录中断分片。

        fail_ratio 用于模拟「大文件在八成位置中断」：按比例定位失败分片。
        """
        if not filename.strip() or not channel_code.strip():
            raise ValueError("文件名与通道编号不能为空")
        if total_shards <= 0 or shard_size <= 0:
            raise ValueError("分片数量与分片大小必须为正整数")
        with self._lock:
            file_id = _new_id("FILE")
            size = total_shards * shard_size
            if fail_ratio is not None and failed_shard is None:
                # 八成位置：第一个越过该比例的分片即为失败分片
                failed_shard = max(1, min(total_shards, int(-(-fail_ratio * total_shards // 1))))
            shards: list[dict[str, Any]] = []
            for no in range(1, total_shards + 1):
                start = (no - 1) * shard_size
                end = min(no * shard_size, size)
                status = SHARD_DONE
                error_position: dict[str, Any] | None = None
                if failed_shard is not None and no == failed_shard:
                    status = SHARD_FAILED
                    error_position = {
                        "byte_offset": start,
                        "line": failed_line or (no * 100),
                        "reason": "分片解析中断",
                    }
                elif failed_shard is not None and no > failed_shard:
                    status = SHARD_TODO
                shards.append({
                    "shard_no": no,
                    "source_file_id": file_id,
                    "offset_start": start,
                    "offset_end": end,
                    "byte_size": end - start,
                    "checksum": _checksum(f"{filename}:{version}:{no}"),
                    "status": status,
                    "error_position": error_position,
                    "parse_result_id": None,
                })
            data_file = {
                "file_id": file_id,
                "filename": filename,
                "version": version,
                "revision_of": revision_of,
                "channel_code": channel_code,
                "total_shards": total_shards,
                "shard_size": shard_size,
                "size": size,
                "checksum": _checksum(f"{filename}:{version}:{size}"),
                "content_hint": content_hint,
                "failed_shard": failed_shard,
                "shards": shards,
                "created_at": _now(),
            }
            self._files[file_id] = data_file
            return dict(data_file)

    def list_files(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(item) for item in self._files.values()]

    def get_file(self, file_id: str) -> dict[str, Any] | None:
        with self._lock:
            item = self._files.get(file_id)
            return dict(item) if item else None

    def register_revision(self, original_file_id: str, *, failed_shard: int | None = None) -> dict[str, Any]:
        """供应商在中段失败后上传同文件的新修订版本。"""
        with self._lock:
            original = self._files.get(original_file_id)
            if original is None:
                raise ValueError(f"原文件 {original_file_id} 不存在")
            revision_version = f"rev-{uuid.uuid4().hex[:6]}"
            return self.register_file(
                filename=original["filename"],
                channel_code=original["channel_code"],
                total_shards=original["total_shards"],
                shard_size=original["shard_size"],
                version=revision_version,
                revision_of=original_file_id,
                content_hint=original.get("content_hint", ""),
                failed_shard=failed_shard,
            )

    # ------------------------------------------------------------------ 续传清单
    def create_manifest(
        self,
        *,
        file_id: str,
        strategy: str = STRATEGY_ORIGINAL,
        client_token: str | None = None,
        revision_file_id: str | None = None,
        failed_shards: list[int] | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        """依据数据文件与失败分片记录生成通道恢复偏移及续传清单。

        两个入口（接入通道 / 格式解析服务）都走这里，保证口径一致。
        同一 client_token 并发提交时只生成一份清单。
        """
        if strategy not in STRATEGY_LABELS:
            return None, f"未知恢复做法「{strategy}」，仅支持 {list(STRATEGY_LABELS)}"
        with self._lock:
            if client_token and client_token in self._tokens:
                existing_id = self._tokens[client_token]
                return self._snapshot(self._manifests[existing_id]), "相同提交令牌已生成清单，重复提交已幂等拦截"
            data_file = self._files.get(file_id)
            if data_file is None:
                return None, f"数据文件 {file_id} 不存在，无法生成续传清单"
            revision = self._files.get(revision_file_id) if revision_file_id else None
            if revision_file_id and revision is None:
                return None, f"修订文件 {revision_file_id} 不存在"

            failed_records = failed_shards or [
                s["shard_no"] for s in data_file["shards"] if s["status"] == SHARD_FAILED
            ]
            if not failed_records:
                return None, "缺少失败分片记录，无需生成续传清单"
            fail_shard_no = min(failed_records)
            fail_offset = data_file["shards"][fail_shard_no - 1]["offset_start"]

            manifest_id = _new_id("RSM")
            shards = [dict(s, error_position=dict(s["error_position"]) if s["error_position"] else None)
                      for s in data_file["shards"]]

            boundary_note = ""
            resume_file_id = file_id
            if strategy == STRATEGY_REVISION:
                if revision is None:
                    return None, "修订版本前置做法要求先上传修订文件"
                # 版本边界：从修订文件 0 号边界重新计算剩余分片偏移
                resume_file_id = revision_file_id
                revision_shards = {s["shard_no"]: s for s in revision["shards"]}
                for shard in shards:
                    if shard["shard_no"] >= fail_shard_no:
                        rev_shard = revision_shards[shard["shard_no"]]
                        shard["source_file_id"] = revision_file_id
                        shard["offset_start"] = rev_shard["offset_start"]
                        shard["offset_end"] = rev_shard["offset_end"]
                        shard["checksum"] = rev_shard["checksum"]
                        if shard["status"] == SHARD_DONE:
                            shard["status"] = SHARD_TODO
                boundary_note = (
                    f"版本边界位于原文件偏移 {fail_offset}，自该边界切换修订文件 "
                    f"{revision['version']} 并重新计算剩余分片偏移"
                )
            else:
                # 原批次连续且偏移直接可用：沿用原文件偏移，旧结果保留可回查
                boundary_note = f"原批次偏移连续可用，续传边界直接取原文件失败偏移 {fail_offset}"

            for shard in shards:
                if shard["shard_no"] < fail_shard_no:
                    shard["status"] = SHARD_DONE
                elif shard["shard_no"] == fail_shard_no and shard["status"] != SHARD_FAILED:
                    shard["status"] = SHARD_FAILED

            ledger = []
            for shard in shards:
                if shard["status"] in (SHARD_FAILED, SHARD_TODO):
                    ledger.append({
                        "ledger_id": _new_id("LED"),
                        "manifest_id": manifest_id,
                        "shard_no": shard["shard_no"],
                        "source_file_id": shard["source_file_id"],
                        "offset_start": shard["offset_start"],
                        "offset_end": shard["offset_end"],
                        "status": LEDGER_TODO,
                        "manifest_status": MANIFEST_STATUSES[0],
                        "updated_at": _now(),
                    })

            manifest = {
                "manifest_id": manifest_id,
                "channel_code": data_file["channel_code"],
                "original_file": {
                    "file_id": data_file["file_id"],
                    "filename": data_file["filename"],
                    "version": data_file["version"],
                    "checksum": data_file["checksum"],
                    "size": data_file["size"],
                },
                "revision_file": ({
                    "file_id": revision["file_id"],
                    "filename": revision["filename"],
                    "version": revision["version"],
                    "checksum": revision["checksum"],
                    "size": revision["size"],
                } if revision else None),
                "strategy": strategy,
                "strategy_label": STRATEGY_LABELS[strategy],
                "fail_shard_no": fail_shard_no,
                "fail_offset": fail_offset,
                "recovery_offset": fail_offset,
                "resume_file_id": resume_file_id,
                "version_boundary_offset": fail_offset,
                "boundary_note": boundary_note,
                "total_shards": len(shards),
                "shards": shards,
                "ledger": ledger,
                "field_packages": [],
                "status": MANIFEST_STATUSES[0],
                "confirm_token": client_token,
                "history": [{"at": _now(), "from": None, "to": MANIFEST_STATUSES[0],
                             "note": "生成续传清单并回写待核对台账"}],
                "created_at": _now(),
                "updated_at": _now(),
            }
            self._manifests[manifest_id] = manifest
            if client_token:
                self._tokens[client_token] = manifest_id
            return self._snapshot(manifest), "续传清单已生成，待核对确认"

    def list_manifests(self, channel_code: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            items = list(self._manifests.values())
            if channel_code:
                items = [m for m in items if m["channel_code"] == channel_code]
            return [self._snapshot(m) for m in items]

    def get_manifest(self, manifest_id: str) -> dict[str, Any] | None:
        with self._lock:
            manifest = self._manifests.get(manifest_id)
            return self._snapshot(manifest) if manifest else None

    # ---------------------------------------------------------- 并发确认（幂等）
    def confirm_manifest(self, manifest_id: str, client_token: str | None = None) -> tuple[dict[str, Any] | None, str, bool]:
        """确认续传清单：并发提交两次仅一份生效，另一份拒绝。"""
        with self._lock:
            manifest = self._manifests.get(manifest_id)
            if manifest is None:
                return None, f"续传清单 {manifest_id} 不存在", False
            if manifest["status"] != MANIFEST_STATUSES[0]:
                # 重复/并发确认只拒绝本次请求，不改写清单自身状态，第一份确认始终有效
                self._append_history(manifest, manifest["status"],
                                     f"重复确认已拒绝：清单已处于「{manifest['status']}」")
                return self._snapshot(manifest), "该清单已确认，并发重复提交仅保留第一份，本次请求已拒绝", False
            if client_token and client_token in self._tokens and self._tokens[client_token] != manifest_id:
                return None, "提交令牌对应其他清单，确认请求已拒绝", False
            self._transition(manifest, MANIFEST_STATUSES[1], "确认通过，等待按失败偏移续传")
            return self._snapshot(manifest), "续传清单确认成功，可执行断点续传", True

    # ---------------------------------------------------------- 按失败偏移续传
    def resume_manifest(self, manifest_id: str) -> tuple[dict[str, Any] | None, str]:
        """从失败偏移恢复通道导入：只处理未完成分片，已完成分片不再解析。"""
        with self._lock:
            manifest = self._manifests.get(manifest_id)
            if manifest is None:
                return None, f"续传清单 {manifest_id} 不存在"
            if manifest["status"] == MANIFEST_STATUSES[3]:
                return self._snapshot(manifest), "清单已恢复完成，重复触发不再写入任何分片"
            if manifest["status"] not in (MANIFEST_STATUSES[1], MANIFEST_STATUSES[2]):
                return None, f"清单状态为「{manifest['status']}」，暂不能续传"

            self._transition(manifest, MANIFEST_STATUSES[2], "按失败偏移开始续传")
            processed: list[int] = []
            skipped_completed: list[int] = []
            for shard in manifest["shards"]:
                result_key = (shard["source_file_id"], shard["shard_no"])
                # 唯一约束：同一分片只允许一份解析结果，重放不会重复写入
                if result_key not in self._parse_results:
                    self._parse_results[result_key] = _new_id("PARSE")
                shard["parse_result_id"] = self._parse_results[result_key]
                if shard["status"] == SHARD_DONE:
                    # 已完成分片保留首次解析结果、不再重新解析
                    skipped_completed.append(shard["shard_no"])
                    continue
                shard["status"] = SHARD_DONE
                shard["error_position"] = None
                processed.append(shard["shard_no"])
                manifest["recovery_offset"] = shard["offset_end"]
                for entry in manifest["ledger"]:
                    if entry["shard_no"] == shard["shard_no"]:
                        entry["status"] = LEDGER_DONE
                        entry["manifest_status"] = MANIFEST_STATUSES[2]
                        entry["updated_at"] = _now()
            self._transition(
                manifest, MANIFEST_STATUSES[3],
                f"续传完成：补传分片 {processed or '无'}，跳过已完成分片 {skipped_completed or '无'}",
            )
            for entry in manifest["ledger"]:
                entry["manifest_status"] = manifest["status"]
                entry["updated_at"] = _now()
            self._sync_channel(manifest)
            return self._snapshot(manifest), (
                f"已按失败偏移 {manifest['fail_offset']} 续传，补传 {len(processed)} 个分片，"
                f"跳过 {len(skipped_completed)} 个已完成分片，恢复偏移推进至 {manifest['recovery_offset']}"
            )

    # ---------------------------------------------------------- 格式解析服务入口
    def generate_field_packages(self, manifest_id: str) -> tuple[dict[str, Any] | None, str]:
        """格式解析服务按续传清单只读取未完成分片并生成字段包。"""
        with self._lock:
            manifest = self._manifests.get(manifest_id)
            if manifest is None:
                return None, f"续传清单 {manifest_id} 不存在"
            packages = []
            for shard in manifest["shards"]:
                if shard["status"] == SHARD_DONE:
                    continue  # 已完成分片不能再次进入解析
                result_key = (shard["source_file_id"], shard["shard_no"])
                package_id = self._parse_results.get(result_key) or _new_id("FIELD")
                if result_key not in self._parse_results:
                    self._parse_results[result_key] = package_id
                packages.append({
                    "package_id": package_id,
                    "manifest_id": manifest_id,
                    "shard_no": shard["shard_no"],
                    "source_file_id": shard["source_file_id"],
                    "offset_start": shard["offset_start"],
                    "offset_end": shard["offset_end"],
                    "fields": {
                        "通道编号": manifest["channel_code"],
                        "来源文件": self._files[shard["source_file_id"]]["filename"],
                        "分片序号": shard["shard_no"],
                        "记录区间": f"{shard['offset_start']}-{shard['offset_end']}",
                    },
                })
            # 登记到清单但不去重已存在的包编号
            known = {p["shard_no"] for p in manifest["field_packages"]}
            for package in packages:
                if package["shard_no"] not in known:
                    manifest["field_packages"].append(package)
            manifest["updated_at"] = _now()
            return self._snapshot(manifest), (
                f"已为 {len(packages)} 个未完成分片生成字段包，已完成分片未再次解析"
            )

    def reconcile_manifest(self, manifest_id: str) -> tuple[dict[str, Any] | None, str]:
        """格式解析服务核对清单，并把可用状态同步给数据接入通道。"""
        with self._lock:
            manifest = self._manifests.get(manifest_id)
            if manifest is None:
                return None, f"续传清单 {manifest_id} 不存在"
            violations = []
            seen: dict[tuple[str, int], str] = {}
            for shard in manifest["shards"]:
                key = (shard["source_file_id"], shard["shard_no"])
                result_id = self._parse_results.get(key)
                if shard["status"] == SHARD_DONE and not result_id:
                    violations.append(f"分片 {shard['shard_no']} 标记完成但缺少解析结果")
                if result_id:
                    if key in seen and seen[key] != result_id:
                        violations.append(f"分片 {shard['shard_no']} 存在多份解析结果")
                    seen[key] = result_id
            if violations:
                return self._snapshot(manifest), "核对未通过：" + "；".join(violations)
            sync = self._sync_channel(manifest)
            self._append_history(manifest, manifest["status"], "格式解析服务核对通过，可用状态已同步接入通道")
            return self._snapshot(manifest), f"核对通过：每个分片仅一份解析结果，可用状态已同步通道 {sync['channel_code']}"

    def get_ledger(self, manifest_id: str | None = None) -> list[dict[str, Any]]:
        """待核对台账：相邻业务面按清单状态变化联动更新。"""
        with self._lock:
            entries: list[dict[str, Any]] = []
            for manifest in self._manifests.values():
                if manifest_id and manifest["manifest_id"] != manifest_id:
                    continue
                entries.extend(dict(item) for item in manifest["ledger"])
            return entries

    # ---------------------------------------------------------- 上传 / 导入校验
    def parse_csv_manifest(self, csv_text: str) -> dict[str, Any]:
        """把导出的续传清单 CSV 还原成可校验的清单结构，实现「导出后直接再提交」。"""
        reader = csv.DictReader(io.StringIO(csv_text.lstrip("﻿")))
        rows = list(reader)
        if not rows:
            return {"channel_code": "", "original_file": {}, "fail_offset": None, "shards": []}
        label_to_strategy = {label: value for value, label in STRATEGY_LABELS.items()}
        first = rows[0]
        shards: list[dict[str, Any]] = []
        for row in rows:
            error_position = None
            if row.get("错误位置"):
                # 形如 7168#L812
                raw = row["错误位置"]
                byte_part, _, line_part = raw.partition("#L")
                error_position = {
                    "byte_offset": int(byte_part) if byte_part.isdigit() else 0,
                    "line": int(line_part) if line_part.isdigit() else 0,
                    "reason": "导入失败分片保留的错误位置",
                }
            shards.append({
                "shard_no": int(row["分片序号"]),
                "source_file_id": row.get("来源文件") or None,
                "offset_start": int(row["分片起始"]),
                "offset_end": int(row["分片结束"]),
                "status": row["分片状态"],
                "error_position": error_position,
                "parse_result_id": row.get("解析结果") or None,
            })
        revision_file = None
        if first.get("修订文件"):
            revision_file = {"filename": first["修订文件"], "version": first.get("修订版本") or ""}
        return {
            "manifest_id": first.get("清单编号") or None,
            "channel_code": first["通道编号"],
            "original_file": {"filename": first["原文件"], "version": first.get("原版本") or ""},
            "revision_file": revision_file,
            "strategy": label_to_strategy.get(first.get("策略", ""), STRATEGY_ORIGINAL),
            "fail_offset": int(first["失败偏移"]),
            "recovery_offset": int(first.get("恢复偏移") or first["失败偏移"]),
            "version_boundary_offset": int(first.get("版本边界") or first["失败偏移"]),
            "shards": shards,
        }

    def import_manifest(self, payload: dict[str, Any]) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
        """导入续传清单并做校验；失败文件保留错误位置，不静默丢弃。"""
        errors: list[dict[str, Any]] = []
        required = ["channel_code", "original_file", "fail_offset", "shards"]
        for field in required:
            if field not in payload:
                errors.append({"field": field, "position": None, "reason": f"缺少必填段 {field}"})
        shards = payload.get("shards") or []
        if not isinstance(shards, list) or not shards:
            errors.append({"field": "shards", "position": None, "reason": "续传分片清单为空"})
        for shard in shards:
            if not isinstance(shard, dict):
                errors.append({"field": "shards", "position": None, "reason": "分片结构不是对象"})
                continue
            if shard.get("status") == SHARD_FAILED and not shard.get("error_position"):
                # 失败文件必须保留错误位置
                errors.append({
                    "field": f"shards[{shard.get('shard_no')}]",
                    "position": shard.get("offset_start"),
                    "reason": "失败分片缺少错误位置信息",
                })
            for bound in ("offset_start", "offset_end"):
                if not isinstance(shard.get(bound), int):
                    errors.append({
                        "field": f"shards[{shard.get('shard_no')}].{bound}",
                        "position": shard.get("offset_start"),
                        "reason": "偏移必须为整数字节位",
                    })
        if errors:
            broken = {
                "manifest_id": _new_id("RSM"),
                "status": MANIFEST_STATUSES[5],
                "import_errors": errors,
                "raw_payload": payload,
                "created_at": _now(),
            }
            return broken, errors
        manifest_id = payload.get("manifest_id") or _new_id("RSM")
        manifest = {
            "manifest_id": manifest_id,
            "channel_code": payload["channel_code"],
            "original_file": payload["original_file"],
            "revision_file": payload.get("revision_file"),
            "strategy": payload.get("strategy", STRATEGY_ORIGINAL),
            "strategy_label": STRATEGY_LABELS.get(payload.get("strategy", STRATEGY_ORIGINAL), "导入清单"),
            "fail_shard_no": payload.get("fail_shard_no", shards[0].get("shard_no", 1)),
            "fail_offset": payload["fail_offset"],
            "recovery_offset": payload.get("recovery_offset", payload["fail_offset"]),
            "resume_file_id": payload.get("resume_file_id"),
            "version_boundary_offset": payload.get("version_boundary_offset", payload["fail_offset"]),
            "boundary_note": payload.get("boundary_note", "外部导入清单"),
            "total_shards": len(shards),
            "shards": [dict(s) for s in shards],
            "ledger": [],
            "field_packages": [],
            "status": MANIFEST_STATUSES[0],
            "confirm_token": None,
            "history": [{"at": _now(), "from": None, "to": MANIFEST_STATUSES[0], "note": "外部清单导入校验通过"}],
            "created_at": _now(),
            "updated_at": _now(),
        }
        for shard in manifest["shards"]:
            if shard.get("status") in (SHARD_FAILED, SHARD_TODO):
                manifest["ledger"].append({
                    "ledger_id": _new_id("LED"),
                    "manifest_id": manifest_id,
                    "shard_no": shard["shard_no"],
                    "source_file_id": shard.get("source_file_id"),
                    "offset_start": shard.get("offset_start"),
                    "offset_end": shard.get("offset_end"),
                    "status": LEDGER_TODO,
                    "manifest_status": MANIFEST_STATUSES[0],
                    "updated_at": _now(),
                })
        with self._lock:
            self._manifests[manifest_id] = manifest
        return self._snapshot(manifest), []

    def export_rows(self, manifest_id: str) -> tuple[list[str], list[list[Any]]] | None:
        """导出续传清单 CSV 行结构，导出后可直接再次提交。"""
        with self._lock:
            manifest = self._manifests.get(manifest_id)
            if manifest is None:
                return None
            header = ["清单编号", "通道编号", "策略", "原文件", "原版本", "修订文件", "修订版本",
                      "失败偏移", "恢复偏移", "版本边界", "分片序号", "来源文件", "分片起始",
                      "分片结束", "分片状态", "错误位置", "解析结果", "清单状态"]
            rows: list[list[Any]] = []
            for shard in manifest["shards"]:
                source = self._files.get(shard["source_file_id"])
                source_name = source["filename"] if source else shard["source_file_id"]
                err = shard.get("error_position")
                rows.append([
                    manifest["manifest_id"], manifest["channel_code"], manifest["strategy_label"],
                    manifest["original_file"]["filename"], manifest["original_file"]["version"],
                    manifest["revision_file"]["filename"] if manifest["revision_file"] else "",
                    manifest["revision_file"]["version"] if manifest["revision_file"] else "",
                    manifest["fail_offset"], manifest["recovery_offset"],
                    manifest["version_boundary_offset"], shard["shard_no"], source_name,
                    shard["offset_start"], shard["offset_end"], shard["status"],
                    f"{err['byte_offset']}#L{err['line']}" if err else "",
                    shard.get("parse_result_id") or "", manifest["status"],
                ])
            return header, rows

    # ------------------------------------------------------------------ 内部工具
    def _transition(self, manifest: dict[str, Any], target: str, note: str) -> None:
        previous = manifest["status"]
        manifest["status"] = target
        manifest["updated_at"] = _now()
        self._append_history(manifest, target, note, previous=previous)
        for entry in manifest["ledger"]:
            entry["manifest_status"] = target
            entry["updated_at"] = _now()

    def _append_history(self, manifest: dict[str, Any], target: str, note: str, previous: str | None = None) -> None:
        manifest["history"].append({
            "at": _now(),
            "from": previous if previous is not None else manifest["history"][-1]["to"],
            "to": target,
            "note": note,
        })

    def _sync_channel(self, manifest: dict[str, Any]) -> dict[str, Any]:
        """把清单可用状态回写到接入通道记录，供接入通道页查看。"""
        sync = {
            "channel_code": manifest["channel_code"],
            "manifest_id": manifest["manifest_id"],
            "recovery_offset": manifest["recovery_offset"],
            "fail_offset": manifest["fail_offset"],
            "original_file": manifest["original_file"]["filename"],
            "original_version": manifest["original_file"]["version"],
            "revision_file": manifest["revision_file"]["version"] if manifest["revision_file"] else None,
            "manifest_status": manifest["status"],
            "strategy": manifest["strategy_label"],
        }
        for row in store.rows("ingestion"):
            if row.get("通道编号") == manifest["channel_code"]:
                row["恢复偏移"] = manifest["recovery_offset"]
                row["失败偏移"] = manifest["fail_offset"]
                row["原文件"] = manifest["original_file"]["filename"]
                row["修订版本"] = sync["revision_file"] or "—"
                row["续传清单状态"] = manifest["status"]
                row["续传清单编号"] = manifest["manifest_id"]
        return sync

    def _snapshot(self, manifest: dict[str, Any]) -> dict[str, Any]:
        """深拷贝快照，避免调用方直接改写内存状态。"""
        def clone(value: Any) -> Any:
            if isinstance(value, dict):
                return {k: clone(v) for k, v in value.items()}
            if isinstance(value, list):
                return [clone(v) for v in value]
            return value
        return clone(manifest)

    def _seed_demo_scenario(self) -> None:
        """预置一个在八成位置中断的大文件场景，页面打开即有数据可看。"""
        try:
            channel = store.rows("ingestion")[0]["通道编号"]
        except (IndexError, KeyError):
            channel = "INGE-DEMO"
        data_file = self.register_file(
            filename="供应商主数据_2026Q4.csv",
            channel_code=channel,
            total_shards=10,
            shard_size=1024,
            version="v1",
            fail_ratio=0.8,
            failed_line=812,
        )
        manifest, _ = self.create_manifest(
            file_id=data_file["file_id"],
            strategy=STRATEGY_ORIGINAL,
            client_token="seed-demo-token",
        )
        self._demo_manifest_id = manifest["manifest_id"] if manifest else None


recovery_service = RecoveryService()
