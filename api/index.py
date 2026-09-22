import os
import io
import re
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

# Kiểm tra thư viện hỗ trợ PDF render
try:
    import pypdfium2 as pdfium
    PDFIUM_AVAILABLE = True
except Exception:
    pdfium = None
    PDFIUM_AVAILABLE = False

app = FastAPI(title="MarkItDown Cloud API - KBNN Edition", version="2.0.0")

# Bật CORS cho toàn bộ domain
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
    fileData: Optional[str] = ""  # Tương thích 100% với DocumentService.js của KBNN SmartDraft
    mimeType: Optional[str] = ""

def extract_drive_id(url: str):
    m1 = re.search(r'/d/([a-zA-Z0-9_-]+)', url)
    if m1: return m1.group(1)
    m2 = re.search(r'[?&]id=([a-zA-Z0-9_-]+)', url)
    if m2: return m2.group(1)
    return None

def clean_base64_string(b64_input: str) -> bytes:
    """Loại bỏ tiền tố data:*/*;base64, nếu có và giải mã an toàn"""
    clean_str = b64_input.strip()
    if "," in clean_str:
        clean_str = clean_str.split(",", 1)[1]
    # Bổ sung padding nếu bị thiếu
    missing_padding = len(clean_str) % 4
    if missing_padding:
        clean_str += '=' * (4 - missing_padding)
    return base64.b64decode(clean_str)

def process_file_bytes(file_bytes: bytes, file_name: str) -> dict:
    ext = os.path.splitext(file_name)[1].lower()

    # Tự động nhận diện định dạng theo magic bytes nếu thiếu extension
    if not ext or ext in ['.bin', '.tmp']:
        if file_bytes.startswith(b'%PDF'):
            ext = '.pdf'
        elif file_bytes.startswith(b'PK\x03\x04'):
            if b'word/' in file_bytes[:2000]:
                ext = '.docx'
            elif b'xl/' in file_bytes[:2000]:
                ext = '.xlsx'
            else:
                ext = '.docx'
        elif file_bytes.startswith(b'\xd0\xcf\x11\xe0'):
            ext = '.doc'  # Binary MS Office cũ
        else:
            ext = '.pdf'

    # Sử dụng tempfile tương thích cả Windows và Linux (Vercel)
    temp_dir = tempfile.gettempdir()
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=ext, dir=temp_dir) as tmp:
            tmp.write(file_bytes)
            tmp_path = tmp.name

        start_time = datetime.now()

        # Bóc tách bằng Microsoft MarkItDown
        result = md_converter.convert(tmp_path)
        elapsed_ms = int((datetime.now() - start_time).total_seconds() * 1000)

        raw_md = (result.text_content or '').strip()
        char_count = len(raw_md)
        total_pages = 1
        is_scanned = False
        page_images = []

        if ext == '.pdf':
            if PDFIUM_AVAILABLE:
                try:
                    pdf_doc = pdfium.PdfDocument(file_bytes)
                    total_pages = len(pdf_doc)
                    pages_to_render = [0]
                    if total_pages > 1:
                        pages_to_render.append(total_pages - 1)
                    for p_idx in pages_to_render:
                        pil_img = pdf_doc[p_idx].render(scale=1.5).to_pil()
                        buf = io.BytesIO()
                        pil_img.save(buf, format='JPEG', quality=80)
                        b64_str = base64.b64encode(buf.getvalue()).decode('utf-8')
                        page_images.append({
                            "page": p_idx + 1,
                            "data": b64_str,
                            "mimeType": "image/jpeg"
                        })
                except Exception:
                    pass

            # Phát hiện PDF dạng ảnh scan (trung bình dưới 150 ký tự/trang)
            avg_chars = char_count / max(1, total_pages)
            if char_count < 60 or avg_chars < 150:
                is_scanned = True

        return {
            "success": True,
            "fileName": file_name,
            "fileType": ext.replace('.', ''),
            "markdown": raw_md,
            "text": raw_md,       # Trả về cả 2 trường: "text" cho Apps Script, "markdown" cho Web
            "charCount": char_count,
            "pageCount": total_pages,
            "hasText": (not is_scanned and char_count > 60),
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
    return {
        "status": "online",
        "service": "Vercel Python Microsoft MarkItDown (KBNN SmartDraft Edition v2.0)",
        "pdfium": PDFIUM_AVAILABLE,
        "timestamp": datetime.now().isoformat()
    }

@app.post("/api/convert")
@app.post("/convert")
async def convert(req: ConvertRequest):
    try:
        file_bytes = b""
        file_name = req.fileName or "document.pdf"

        # 1. Tải qua URL (Google Drive hoặc direct link)
        if req.url and req.url.strip():
            url = req.url.strip()
            dl_url = url
            drive_id = extract_drive_id(url)
            if drive_id:
                dl_url = f"https://drive.google.com/uc?export=download&id={drive_id}&confirm=t"
                if not file_name or file_name == "document.pdf":
                    file_name = f"drive_{drive_id}.pdf"

            resp = requests.get(dl_url, allow_redirects=True, timeout=25)
            if resp.status_code != 200:
                raise HTTPException(status_code=400, detail=f"Không thể tải file từ URL (Lỗi HTTP {resp.status_code})")

            # Phát hiện trang HTML đăng nhập Google (Drive file chưa public hoặc quá lớn)
            if resp.content[:100].lstrip().startswith(b'<!DOCTYPE') or resp.content[:100].lstrip().startswith(b'<html'):
                raise HTTPException(
                    status_code=400,
                    detail="URL trỏ về trang HTML (File Google Drive chưa được chia sẻ công khai hoặc yêu cầu đăng nhập Google)"
                )

            file_bytes = resp.content

        # 2. Xử lý Base64 — hỗ trợ cả req.base64 và req.fileData (tương thích KBNN SmartDraft)
        elif (req.base64 and req.base64.strip()) or (req.fileData and req.fileData.strip()):
            raw_b64 = req.base64.strip() if (req.base64 and req.base64.strip()) else req.fileData.strip()
            file_bytes = clean_base64_string(raw_b64)
        else:
            raise HTTPException(
                status_code=400,
                detail="Thiếu dữ liệu: Cần cung cấp 'base64'/'fileData' hoặc 'url'"
            )

        return process_file_bytes(file_bytes, file_name)

    except HTTPException:
        raise
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
    <h1 style="color:#15803d;">🚀 Microsoft MarkItDown Engine Đang Hoạt Động!</h1>
    <p style="color:#475569;">Sẵn sàng bóc tách tài liệu thông minh cho <b>KBNN SmartDraft</b> — Trợ lý Tham mưu số.</p>
    <p style="color:#94a3b8; font-size:14px;">Endpoints: POST /api/convert &nbsp;|&nbsp; GET /health</p>
</body>
</html>"""
