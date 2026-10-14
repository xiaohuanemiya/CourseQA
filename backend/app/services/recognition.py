import json
import re
from fastapi import HTTPException

from ..config import settings

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}
RECOGNIZABLE = IMAGE_EXTENSIONS | {".pdf"}


def run_recognition(doc, progress=None):
    from .paddle_ocr import recognize
    return current_report(recognize(doc, progress=progress))


def current_report(report):
    result = {k: v for k, v in report.items() if k != "requires_review"}
    result["warnings"] = [w.replace("所有公式与条件仍需审核", "保留公式与条件的原文位置")
        .replace("云端图文识别结果均需审核", "云端图文识别结果保留供对照") for w in result.get("warnings", [])]
    return result


def read_report(document_id):
    path=settings().storage_dir/"recognition"/document_id/"manifest.json"
    if not path.is_file():
        raise HTTPException(404,"暂无识别报告，请先完成图文识别任务")
    return current_report(json.loads(path.read_text()))


def asset_path(document_id,asset_id):
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,100}\.png",asset_id):
        raise HTTPException(404,"图片不存在")
    report=read_report(document_id)
    if asset_id not in report.get("assets",[]):
        raise HTTPException(404,"图片不属于此文档识别版本")
    folder=(settings().storage_dir/"recognition"/document_id).resolve()
    path=(folder/asset_id).resolve()
    if not path.is_relative_to(folder) or not path.is_file():
        raise HTTPException(404,"图片不存在")
    return path
