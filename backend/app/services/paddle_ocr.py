"""PaddleOCR asynchronous cloud jobs; credentials and signed URLs stay on the server."""
import hashlib
import io
import json
import os
import re
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser
from pathlib import Path
from threading import Lock
from types import SimpleNamespace
from urllib.parse import urlsplit

import httpx
from PIL import Image, ImageOps, ImageSequence
from pypdf import PdfReader, PdfWriter

from ..config import settings

ADAPTER_VERSION = "paddle-jobs-v1"
MAX_RESULT_BYTES = 80 * 1024 * 1024


def save_json(path, data):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    temporary.chmod(0o600)
    temporary.replace(path)


class TableText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows, self.row, self.cell = [], [], None

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self.row = []
        if tag in {"td", "th"}:
            self.cell = []
        if tag == "br" and self.cell is not None:
            self.cell.append(" ")

    def handle_data(self, data):
        if self.cell is not None:
            self.cell.append(data)

    def handle_endtag(self, tag):
        if tag in {"td", "th"} and self.cell is not None:
            self.row.append("".join(self.cell).strip().replace("|", "\\|"))
            self.cell = None
        if tag == "tr" and self.row:
            self.rows.append(self.row)

    def markdown(self):
        if not self.rows:
            return "表格结构未能解析，请对照原图"
        width = max(map(len, self.rows))
        rows = ["| " + " | ".join(r + [""] * (width-len(r))) + " |" for r in self.rows]
        rows.insert(1, "| " + " | ".join(["---"] * width) + " |")
        return "\n".join(rows)


def clean_content(text):
    # The jobs API escapes LaTeX commands twice in prunedResult, but not in markdown.text.
    # Collapse only doubled command prefixes; preserve matrix/aligned row separators (\\).
    return re.sub(r"\\\\(?=[A-Za-z])", lambda _: "\\", text).strip()


def download(client, url):
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    # Download only the provider's object storage, never a URL mentioned in document text.
    if parsed.scheme != "https" or not host.endswith(".bcebos.com") or parsed.username or parsed.password or parsed.port not in {None, 443}:
        raise ValueError("PaddleOCR 返回了不受支持的结果地址")
    with client.stream("GET", url) as response:
        response.raise_for_status()
        data = bytearray()
        for chunk in response.iter_bytes():
            data.extend(chunk)
            if len(data) > MAX_RESULT_BYTES:
                raise ValueError("PaddleOCR 结果过大，请缩小识别页码范围")
        return bytes(data)


def prepare_upload(path, output, options):
    """Select pages BEFORE cloud upload. Return upload path and original page mapping."""
    first, last = options.get("page_start", 1), options.get("page_end")
    if path.suffix.lower() == ".pdf":
        reader = PdfReader(path)
        last = last or len(reader.pages)
        if first < 1 or first > last or last > len(reader.pages):
            raise ValueError(f"页码范围无效，文档共 {len(reader.pages)} 页")
        selected = list(range(first, last+1))
        target = output / "upload.pdf"
        writer = PdfWriter()
        for number in selected:
            writer.add_page(reader.pages[number-1])
        with target.open("wb") as file:
            writer.write(file)
        return target, selected
    with Image.open(path) as source:
        frames = []
        for frame in ImageSequence.Iterator(source):
            if frame.width * frame.height > 40_000_000:
                raise ValueError("图片超过 4000 万像素，请缩小后上传")
            if len(frames) >= 100:
                raise ValueError("多帧图片超过 100 页，请拆分后上传")
            frames.append(ImageOps.exif_transpose(frame).convert("RGB"))
        target = output / ("upload.pdf" if len(frames)>1 else "upload.png")
        if len(frames)>1:
            frames[0].save(target, save_all=True, append_images=frames[1:], resolution=140)
        else:
            frames[0].save(target)
        return target, list(range(1,len(frames)+1))


def parse_pages(client, records, output, page_map, is_pdf):
    pages, assets, blocks = [], [], []
    for record in records:
        if record.get("errorCode", 0) != 0:
            raise ValueError("PaddleOCR 部分页面识别失败；未导入不完整结果")
        for result in record["result"]["layoutParsingResults"]:
            if len(pages) >= len(page_map):
                raise ValueError("识别结果页数与上传页数不一致")
            number = page_map[len(pages)]
            pruned = result["prunedResult"]
            width, height = pruned["width"], pruned["height"]
            if width <= 0 or height <= 0:
                raise ValueError("识别结果页面尺寸无效")
            # Use the exact image whose coordinate system the provider used.
            with Image.open(io.BytesIO(download(client, result["inputImage"]))) as original:
                if original.width * original.height > 40_000_000:
                    raise ValueError("识别结果图片过大")
                picture = original.convert("RGB")
            page_asset = f"page-{number:05}.png"
            picture.save(output/page_asset)
            assets.append(page_asset)
            pages.append({"page":number, "asset_id":page_asset, "confidence":None})
            for item in pruned["parsing_res_list"]:
                raw_label = item["block_label"]
                if raw_label in {"header", "footer", "page_number"}:
                    continue
                label = "formula" if raw_label in {"display_formula", "formula"} else "picture" if raw_label in {"image", "chart", "figure"} else raw_label
                text = clean_content(item.get("block_content", ""))
                warnings = []
                if label == "table":
                    parser = TableText()
                    parser.feed(text)
                    text = parser.markdown()
                    warnings.append("请对照原图检查表格的合并单元格、行列及公式")
                if label == "picture":
                    text = "图形区域，含义需结合相邻正文人工核验。"
                    warnings.append("保留原图；未自动推断图形的数学含义")
                if not text:
                    continue
                latex = ""
                if label == "formula":
                    latex = re.sub(r"^\s*\$\$|\$\$\s*$", "", text).strip()
                    text = latex
                    warnings.append("公式无可靠正确率分数，请核验上下标、分母、积分上下限及条件")
                coordinates = item.get("block_bbox")
                asset, bbox = page_asset, None
                if coordinates and len(coordinates)==4:
                    bbox = [max(0.,min(1.,float(v)/scale)) for v,scale in zip(coordinates,[width,height,width,height])]
                    if bbox[2] <= bbox[0] or bbox[3] <= bbox[1]:
                        raise ValueError("识别结果内容块坐标无效")
                    bounds = [round(v*scale) for v,scale in zip(bbox,[picture.width,picture.height]*2)]
                    if bounds[2]>bounds[0] and bounds[3]>bounds[1]:
                        asset = f"block-{number:05}-{len(blocks)+1:06}.png"
                        picture.crop(bounds).save(output/asset)
                        assets.append(asset)
                locator = f"第 {number} 页" if is_pdf else f"图片第 {number} 帧"
                blocks.append({"locator":f"{locator} · 内容块 {len(blocks)+1}", "text":text,
                    "label":label, "latex":latex, "page":number, "bbox":bbox, "asset_id":asset,
                    "confidence":None, "warnings":warnings})
    if len(pages) != len(page_map) or not blocks:
        raise ValueError("PaddleOCR 返回的页面不完整或没有可识别内容；未导入")
    return blocks, pages, assets


def ocr_options(doc):
    # Splitting changes must not invalidate paid OCR jobs and their source images.
    defaults = {"mode": "auto", "page_start": 1, "page_end": None}
    return {key: (doc.parse_options or {}).get(key, value) for key, value in defaults.items()}


def recognize(doc, progress=None):
    """Checkpoint long textbooks in 20-page jobs, retaining the original document and page IDs."""
    if Path(doc.filename).suffix.lower() != ".pdf":
        return recognize_batch(doc,progress)
    count=len(PdfReader(doc.path).pages)
    options=ocr_options(doc)
    first,last=options["page_start"],options["page_end"] or count
    if first<1 or first>last or last>count:
        raise ValueError(f"页码范围无效，文档共 {count} 页")
    if last-first<20:
        return recognize_batch(doc,progress)
    output=settings().storage_dir/"recognition"/doc.id
    output.mkdir(parents=True,exist_ok=True)
    ranges=[(p,min(last,p+19)) for p in range(first,last+1,20)]
    values=[10]*len(ranges)
    lock=Lock()

    def process(entry):
        i,(start,end)=entry
        part=SimpleNamespace(id=f"{doc.id}/batches/{start}-{end}",filename=doc.filename,
            path=doc.path,checksum=doc.checksum,parse_options={**options,"page_start":start,"page_end":end})
        def notify(value):
            with lock:
                values[i]=value
                if progress:
                    progress(int(sum(values)/len(values)))
        report=recognize_batch(part,notify)
        for asset in report["assets"]:
            target=output/asset
            if not target.exists():
                os.link(output/f"batches/{start}-{end}"/asset,target)
        notify(85)
        return report

    with ThreadPoolExecutor(max_workers=2) as pool:
        reports=list(pool.map(process,enumerate(ranges)))
    blocks=[b for r in reports for b in r["blocks"]]
    manifest={"parser":settings().paddleocr_model,"adapter":ADAPTER_VERSION,"options":options,
        "blocks":blocks,"pages":[p for r in reports for p in r["pages"]],
        "assets":sorted({a for r in reports for a in r["assets"]}),
        "counts":dict(Counter(b["label"] for b in blocks)),
        "warnings":["全文按 20 页分批识别，保留原页号与公式位置"]}
    save_json(output/"manifest.json",manifest)
    return manifest


def recognize_batch(doc, progress=None):
    cfg = settings()
    if not cfg.paddleocr_token:
        raise ValueError("未配置 PaddleOCR Token，请在后端 .env 设置 PADDLEOCR_TOKEN 后重启 API 与 Worker")
    options = ocr_options(doc)
    output = cfg.storage_dir / "recognition" / doc.id
    output.mkdir(parents=True, exist_ok=True)
    fingerprint = hashlib.sha256(json.dumps([doc.checksum,options,cfg.paddleocr_model,ADAPTER_VERSION],sort_keys=True).encode()).hexdigest()
    manifest_path, job_path = output/"manifest.json", output/"job.json"
    if manifest_path.exists():
        cached = json.loads(manifest_path.read_text())
        if cached.get("fingerprint")==fingerprint and all((output/a).is_file() for a in cached["assets"]):
            return cached
    deadline = time.monotonic() + cfg.ocr_timeout
    upload = None
    try:
        upload, page_map = prepare_upload(Path(doc.path), output, options)
        with httpx.Client(timeout=min(120,cfg.ocr_timeout), follow_redirects=False) as client:
            headers = {"Authorization":"bearer "+cfg.paddleocr_token}
            job = json.loads(job_path.read_text()) if job_path.exists() else {}
            if job.get("fingerprint") != fingerprint:
                with upload.open("rb") as file:
                    response = client.post(cfg.paddleocr_job_url, headers=headers,
                        data={"model":cfg.paddleocr_model, "optionalPayload":json.dumps({
                            "useDocOrientationClassify":False, "useDocUnwarping":False, "useChartRecognition":False})},
                        files={"file":(upload.name,file,"application/pdf" if upload.suffix==".pdf" else "image/png")})
                response.raise_for_status()
                job = {"job_id":response.json()["data"]["jobId"], "fingerprint":fingerprint}
                save_json(job_path,job)
            if not re.fullmatch(r"[a-zA-Z0-9_-]+",job["job_id"]):
                raise ValueError("PaddleOCR 返回了无效任务编号")
            while time.monotonic() < deadline:
                response = client.get(cfg.paddleocr_job_url.rstrip("/")+"/"+job["job_id"], headers=headers)
                response.raise_for_status()
                data = response.json()["data"]
                state = data["state"]
                if progress:
                    info = data.get("extractProgress") or {}
                    total = info.get("totalPages") or len(page_map)
                    progress(min(85, 15+int(65*(info.get("extractedPages") or 0)/max(1,total))))
                if state == "done":
                    raw = download(client,data["resultUrl"]["jsonUrl"])
                    records = [json.loads(line) for line in raw.decode("utf-8").splitlines() if line.strip()]
                    blocks, pages, assets = parse_pages(client,records,output,page_map,Path(doc.filename).suffix.lower()==".pdf")
                    manifest = {"parser":cfg.paddleocr_model, "adapter":ADAPTER_VERSION, "fingerprint":fingerprint,
                        "options":options, "blocks":blocks, "pages":pages, "assets":sorted(set(assets)),
                        "counts":dict(Counter(b["label"] for b in blocks)),
                        "warnings":["图文识别保留原图；版面检测分数不代表公式正确率"]}
                    save_json(manifest_path,manifest)
                    return manifest
                if state == "failed":
                    save_json(output/"last_failure.json",{"job_id":job["job_id"],
                        "error":str(data.get("errorMsg","unknown"))[:2000].replace(cfg.paddleocr_token,"[redacted]")})
                    job_path.unlink(missing_ok=True)
                    raise ValueError("PaddleOCR 云端任务失败，请检查文件清晰度、服务额度后重试")
                if state not in {"pending", "running"}:
                    raise ValueError("PaddleOCR 返回未知任务状态")
                time.sleep(max(0,min(cfg.paddleocr_poll_interval,deadline-time.monotonic())))
            raise ValueError("PaddleOCR 识别超时；可在任务中心重试以继续查询原任务")
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        if status in {401,403}:
            message = "PaddleOCR 鉴权或结果下载授权失败，请检查 Token/额度并重试"
        elif status == 429:
            message = "PaddleOCR 服务限流，请稍后重试"
        else:
            message = f"PaddleOCR 请求失败（HTTP {status}），请稍后重试"
        raise ValueError(message) from None
    except httpx.HTTPError:
        raise ValueError("PaddleOCR 网络连接失败或超时；重试会继续查询已提交任务") from None
    except (KeyError, TypeError, json.JSONDecodeError):
        raise ValueError("PaddleOCR 响应格式不完整，请检查服务状态后重试") from None
    finally:
        if upload:
            upload.unlink(missing_ok=True)
