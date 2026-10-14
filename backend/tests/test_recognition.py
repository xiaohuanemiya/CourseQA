import hashlib
import io
import json
from types import SimpleNamespace

import httpx
import pytest
from PIL import Image
from pypdf import PdfReader, PdfWriter

from app.config import settings
from app.services import paddle_ocr as ocr
from .conftest import login, post


def png():
    buffer = io.BytesIO()
    Image.new("RGB", (200, 100), "white").save(buffer, format="PNG")
    return buffer.getvalue()


def record():
    return {"errorCode":0, "result":{"layoutParsingResults":[{
        "inputImage":"https://test.bcebos.com/page.png",
        "prunedResult":{"width":200,"height":100,"parsing_res_list":[{
            "block_label":"display_formula", "block_bbox":[20,10,160,70],
            "block_content":r" $$ \\sqrt{x^{2}}=\\left|x\\right| $$ "}]}}]}}


def document(tmp_path):
    file = tmp_path / "scan.png"
    file.write_bytes(png())
    return SimpleNamespace(id="test-doc", path=str(file), filename=file.name,
        checksum=hashlib.sha256(file.read_bytes()).hexdigest(), parse_options={"mode":"scan"})


def mock_cloud(monkeypatch, states=None, status=200):
    requests = []
    states = iter(states or ["running", "done"])
    original = httpx.Client

    def handler(request):
        requests.append(request)
        if request.url.host.endswith("bcebos.com"):
            assert "authorization" not in request.headers
            content = png() if request.url.path.endswith("png") else json.dumps(record()).encode()
            return httpx.Response(200, content=content)
        assert request.headers["Authorization"] == "bearer test-token"
        if request.method=="POST":
            assert b"PaddleOCR-VL-1.6" in request.content
            return httpx.Response(status,json={"data":{"jobId":"job-123"}})
        return httpx.Response(200,json={"data":{"state":next(states),
            "extractProgress":{"totalPages":1,"extractedPages":1},
            "resultUrl":{"jsonUrl":"https://test.bcebos.com/results.jsonl"}}})

    monkeypatch.setattr(ocr.httpx,"Client", lambda **kw:original(transport=httpx.MockTransport(handler),**kw))
    cfg = settings()
    monkeypatch.setattr(cfg,"paddleocr_token","test-token")
    monkeypatch.setattr(cfg,"paddleocr_poll_interval",0)
    return requests


def test_cloud_job_result_crops_and_retry_cache(client, tmp_path, monkeypatch):
    requests = mock_cloud(monkeypatch)
    doc = document(tmp_path)
    progress = []
    report = ocr.recognize(doc, progress.append)
    block = report["blocks"][0]
    assert report["parser"] == "PaddleOCR-VL-1.6" and "requires_review" not in report
    assert block["latex"] == r"\sqrt{x^{2}}=\left|x\right|"
    assert block["bbox"] == [.1,.1,.8,.7] and block["confidence"] is None
    folder = settings().storage_dir / "recognition" / doc.id
    with Image.open(folder/block["asset_id"]) as crop:
        assert crop.size == (140,60)
    assert not (folder/"upload.png").exists()
    assert "test-token" not in json.dumps(report) and "https://" not in json.dumps(report)
    count = len(requests)
    assert ocr.recognize(doc) == report and len(requests)==count
    doc.parse_options["splitting"] = {"method": "semantic", "model_profile_id": "ds-flash"}
    assert ocr.recognize(doc) == report and len(requests) == count
    assert progress


def test_timeout_resumes_job_without_second_upload(client, tmp_path, monkeypatch):
    requests = mock_cloud(monkeypatch, ["pending","done"])
    doc = document(tmp_path)
    clock = iter([0,0,0,4000])
    with monkeypatch.context() as context:
        context.setattr(ocr.time,"monotonic",lambda: next(clock))
        with pytest.raises(ValueError, match="识别超时"):
            ocr.recognize(doc)
    assert ocr.recognize(doc)["counts"] == {"formula":1}
    assert sum(r.method=="POST" for r in requests)==1


@pytest.mark.parametrize("status", [401,429,503])
def test_provider_errors_are_sanitized(client, tmp_path, monkeypatch, status):
    mock_cloud(monkeypatch,status=status)
    with pytest.raises(ValueError) as error:
        ocr.recognize(document(tmp_path))
    assert "test-token" not in str(error.value) and "https" not in str(error.value)
    assert not (tmp_path/"recognition/test-doc/manifest.json").exists()


def test_page_range_applies_before_cloud_upload_and_tiff_keeps_frames(tmp_path):
    path = tmp_path/"source.pdf"
    writer = PdfWriter()
    for width in [100,200,300]:
        writer.add_blank_page(width,height=100)
    with path.open("wb") as file:
        writer.write(file)
    target, page_map = ocr.prepare_upload(path,tmp_path,{"page_start":2,"page_end":2})
    assert page_map==[2]
    reader = PdfReader(target)
    assert len(reader.pages)==1 and reader.pages[0].mediabox.width==200
    with pytest.raises(ValueError,match="页码范围"):
        ocr.prepare_upload(path,tmp_path,{"page_start":4})
    tiff = tmp_path/"frames.tiff"
    Image.new("RGB",(100,100),"white").save(tiff,save_all=True,append_images=[Image.new("RGB",(100,100),"black")])
    target, page_map = ocr.prepare_upload(tiff,tmp_path,{})
    assert page_map==[1,2] and len(PdfReader(target).pages)==2


def test_reject_incomplete_pages_and_untrusted_download_urls(tmp_path):
    with httpx.Client() as client:
        for url in ["http://test.bcebos.com/a", "https://127.0.0.1/a", "https://evil.com/a", "https://test.bcebos.com:8000/a"]:
            with pytest.raises(ValueError,match="结果地址"):
                ocr.download(client,url)
        with pytest.raises(ValueError,match="页面不完整"):
            ocr.parse_pages(client,[],tmp_path,[1],True)


def test_formula_row_breaks_and_table_content():
    assert ocr.clean_content(r"a & b \\ c & d") == r"a & b \\ c & d"
    table=ocr.TableText()
    table.feed('<table><tr><th>积分</th><th>次数</th></tr><tr><td>分部积分</td><td>8</td></tr></table>')
    assert "| 分部积分 | 8 |" in table.markdown()


def test_ocr_assets_are_application_scoped_and_withdrawn(client, platform, monkeypatch):
    cfg=settings()
    monkeypatch.setattr(cfg,"ocr_enabled",True)
    mock_cloud(monkeypatch)
    a,b = platform["bundles"]
    response = client.post(f"/api/admin/knowledge-bases/{a['kb']['id']}/documents",
        data={"mode":"scan"}, files={"file":("scan.png",png(),"image/png")})
    assert response.status_code==200 and response.json()["task"]["state"]=="succeeded", response.text
    doc=response.json()["document"]
    report=client.get(f"/api/admin/documents/{doc['id']}/recognition").json()
    asset=report["blocks"][0]["asset_id"]
    contents=client.get(f"/api/admin/knowledge-bases/{a['kb']['id']}/contents").json()
    node=next(n for n in contents["knowledge"] if n["sources"][0].get("document_id")==doc["id"])
    assert not node["reviewed"] and node["kind"]=="formula"
    payload={k:node[k] for k in ["kind","title","content","attributes","sources"]}
    payload["attributes"]["conditions"]="x 为实数"
    payload.update(reviewed=True)
    response=client.put(f"/api/admin/knowledge-bases/{a['kb']['id']}/knowledge/{node['id']}",json=payload)
    assert response.status_code==200,response.text
    assert post(client,f"/admin/knowledge-bases/{a['kb']['id']}/publish")["state"]=="succeeded"
    post(client,"/auth/register",{"username":"ocr-student","password":"test-student-password"})
    assert client.get(f"/api/admin/documents/{doc['id']}/recognition").status_code==403
    url=f"/api/apps/{a['app']['id']}/documents/{doc['id']}/assets/{asset}"
    assert client.get(url).status_code==200
    assert client.get(url.replace(a['app']['id'],b['app']['id'])).status_code==404
    assert client.get(url.replace(asset,"unknown.png")).status_code==404
    login(client)
    assert client.delete(f"/api/admin/knowledge-bases/{a['kb']['id']}/documents/{doc['id']}").status_code==200
    assert client.get(url).status_code==404


def test_invalid_upload_options_rejected(client, platform):
    url=f"/api/admin/knowledge-bases/{platform['bundles'][0]['kb']['id']}/documents"
    for options in [{"mode":"text"},{"mode":"scan","page_start":3,"page_end":1}]:
        assert client.post(url,data=options,files={"file":("scan.png",png(),"image/png")}).status_code==422


def test_long_pdf_joins_original_pages_only_after_every_batch_succeeds(client,tmp_path,monkeypatch):
    path=tmp_path/'book.pdf'
    writer=PdfWriter()
    for _ in range(25):
        writer.add_blank_page(100,100)
    with path.open('wb') as file:
        writer.write(file)
    doc=SimpleNamespace(id='book',path=str(path),filename='book.pdf',checksum='test',
        parse_options={'page_start':3,'page_end':25})
    fail=True
    def batch(part,progress):
        first,last=part.parse_options['page_start'],part.parse_options['page_end']
        if first==23 and fail:
            raise ValueError('second batch failed')
        output=settings().storage_dir/'recognition'/part.id
        output.mkdir(parents=True,exist_ok=True)
        pages=list(range(first,last+1))
        assets=[f'page-{p:05}.png' for p in pages]
        for asset in assets:
            (output/asset).write_bytes(png())
        return {'blocks':[{'page':p,'label':'text'} for p in pages],
            'pages':[{'page':p} for p in pages],'assets':assets}
    monkeypatch.setattr(ocr,'recognize_batch',batch)
    with pytest.raises(ValueError,match='second batch failed'):
        ocr.recognize(doc)
    assert not (settings().storage_dir/'recognition/book/manifest.json').exists()
    fail=False
    report=ocr.recognize(doc)
    assert [p['page'] for p in report['pages']]==list(range(3,26))
    assert len(report['assets'])==23 and report['counts']=={'text':23}
