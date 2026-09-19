import os
import io
import re
import json
import base64
import tempfile
import traceback
from datetime import datetime
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
import requests
from markitdown import MarkItDown

# Kiểm tra thư viện hỗ trợ PDF
try:
    import pypdfium2 as pdfium
    PDFIUM_AVAILABLE = True
except Exception:
    pdfium = None
    PDFIUM_AVAILABLE = False

app = FastAPI(title="MarkItDown Cloud API")

# Bật CORS 100% để gọi được từ vanbandang.vercel.app và mọi thiết bị
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

md_converter = MarkItDown()

class ConvertRequest(BaseModel):
    url: Optional[str] = ""
    fileName: Optional[str] = "document.pdf"
    base64: Optional[str] = ""
    mimeType: Optional[str] = ""

def extract_drive_id(url: str) -> Optional[str]:
    m1 = re.search(r'/d/([a-zA-Z0-9_-]+)', url)
    if m1: return m1.group(1)
    m2 = re.search(r'[?&]id=([a-zA-Z0-9_-]+)', url)
    if m2: return m2.group(1)
    return None

def process_file_bytes(file_bytes: bytes, file_name: str) -> dict:
    ext = os.path.splitext(file_name)[1].lower()
    if not ext or ext == '.bin':
        if file_bytes.startswith(b'%PDF'): ext = '.pdf'
        elif file_bytes.startswith(b'PK\x03\x04'): ext = '.docx'
        else: ext = '.pdf'

    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=ext, dir='/tmp') as tmp:
            tmp.write(file_bytes)
            tmp_path = tmp.name

        start_time = datetime.now()
        result = md_converter.convert(tmp_path)
        elapsed_ms = int((datetime.now() - start_time).total_seconds() * 1000)

        raw_md = (result.text_content or '').strip()
        char_count = len(raw_md)
        total_pages = 1
        is_scanned = False
        page_images = []

        if ext == '.pdf' and PDFIUM_AVAILABLE:
            try:
                pdf_doc = pdfium.PdfDocument(file_bytes)
                total_pages = len(pdf_doc)
                pages_to_render = [0]
                if total_pages > 1:
                    pages_to_render.append(total_pages - 1)
                for p_idx in pages_to_render:
                    pil_img = pdf_doc[p_idx].render(scale=2).to_pil()
                    buf = io.BytesIO()
                    pil_img.save(buf, format='JPEG', quality=85)
                    b64_str = base64.b64encode(buf.getvalue()).decode('utf-8')
                    page_images.append({
                        "page": p_idx + 1,
                        "data": b64_str,
                        "mimeType": "image/jpeg"
                    })
            except Exception:
                pass

        if ext == '.pdf':
            avg_chars = char_count / max(1, total_pages)
            if char_count < 50 or avg_chars < 200:
                is_scanned = True

        return {
            "success": True,
            "fileName": file_name,
            "fileType": ext.replace('.', ''),
            "markdown": raw_md,
            "charCount": char_count,
            "pageCount": total_pages,
            "hasText": (not is_scanned and char_count > 50),
            "isScanned": is_scanned,
            "pageImages": page_images,
            "elapsedMs": elapsed_ms
        }
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass

@app.get("/api/health")
@app.get("/health")
def health():
    return {"status": "online", "service": "Vercel Python MarkItDown", "timestamp": datetime.now().isoformat()}

@app.post("/api/convert")
@app.post("/convert")
async def convert(req: ConvertRequest):
    try:
        file_bytes = b""
        file_name = req.fileName or "document.pdf"

        if req.url and req.url.strip():
            url = req.url.strip()
            dl_url = url
            drive_id = extract_drive_id(url)
            if drive_id:
                dl_url = f"https://drive.google.com/uc?export=download&id={drive_id}&confirm=t"
                if not file_name or file_name == "document.pdf":
                    file_name = f"drive_{drive_id}.pdf"

            resp = requests.get(dl_url, allow_redirects=True, timeout=20)
            if resp.status_code != 200:
                raise HTTPException(status_code=400, detail=f"Không thể tải file (Lỗi {resp.status_code})")
            file_bytes = resp.content
        elif req.base64 and req.base64.strip():
            file_bytes = base64.b64decode(req.base64)
        else:
            raise HTTPException(status_code=400, detail="Thiếu dữ liệu (cần URL hoặc Base64)")

        return process_file_bytes(file_bytes, file_name)
    except Exception as e:
        traceback.print_exc()
        return {"success": False, "error": str(e)}

@app.get("/", response_class=HTMLResponse)
@app.get("/api", response_class=HTMLResponse)
def home():
    return """<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"><title>MarkItDown Online API</title></head>
<body style="font-family:system-ui;padding:40px;text-align:center;background:#f0fdf4;">
    <h1 style="color:#15803d;">🚀 Dịch vụ Microsoft MarkItDown Đang Hoạt Động (Cloud Vercel)!</h1>
    <p style="color:#475569;">Sẵn sàng bóc tách tài liệu cho Web App Quản lý văn bản Đảng.</p>
</body>
</html>"""
