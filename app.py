import os
import json
import re
import cv2
import asyncio
import gc
import time
import psutil  # New: To check memory usage
from pathlib import Path
from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from ultralytics import YOLO
import easyocr
from PIL import Image

# ==========================================
# 1. CONFIGURATION
# ==========================================
BASE_DIR = Path(__file__).resolve().parent
UPLOAD_FOLDER = os.path.join(BASE_DIR, 'uploads')
MAX_FILE_SIZE = 10 * 1024 * 1024  # 10MB Limit

Path(UPLOAD_FOLDER).mkdir(exist_ok=True)

app = FastAPI(title="VerifEye Ultra-Lite", version="3.1")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")

MODEL_PATHS = {
    "AADHAAR": "trained_models/aadhar_best/weights/best.pt",
    "PAN": "trained_models/pan_best/weights/best.pt",
    "VOTER": "trained_models/voter_id_best/weights/best.pt",
    "DRIVING": "trained_models/driving_licence_best/weights/best.pt",
}
MODEL_NAME = "llama3"

# Disable Ollama completely for Free Tier (It's too heavy)
OLLAMA_AVAILABLE = False 

# ==========================================
# 2. MEMORY MANAGEMENT & HELPERS
# ==========================================

def log_memory(stage):
    """Helper to debug memory usage on Render"""
    process = psutil.Process(os.getpid())
    mem = process.memory_info().rss / 1024 / 1024
    print(f"[MEM] {stage}: {mem:.2f} MB")

def optimize_image(image_path):
    """
    Resizes image to max 800px.
    This is CRITICAL for 512MB RAM environments.
    """
    try:
        with Image.open(image_path) as img:
            if img.mode in ("RGBA", "P"):
                img = img.convert("RGB")
            
            # Resize to max 800x800 (Safe for OCR, Low RAM)
            img.thumbnail((800, 800)) 
            img.save(image_path, optimize=True, quality=80)
    except Exception as e:
        print(f"⚠️ Image Optimization Warning: {e}")

def run_lazy_ocr(image_path):
    reader = None
    try:
        log_memory("Before OCR Load")
        # Load Reader (CPU only)
        reader = easyocr.Reader(['en'], gpu=False, verbose=False)
        
        # Read Image & Convert to Grayscale (Saves RAM)
        img_cv = cv2.imread(image_path)
        gray = cv2.cvtColor(img_cv, cv2.COLOR_BGR2GRAY)
        
        # Extract
        result = reader.readtext(gray, detail=0)
        return " ".join(result)
    except Exception as e:
        print(f"❌ OCR Error: {e}")
        return ""
    finally:
        if reader: 
            del reader
        # FORCE CLEANUP
        gc.collect()
        log_memory("After OCR Cleanup")

def get_yolo_confidence(doc_type, image_path):
    # Check available memory before loading YOLO
    # If we have less than 150MB free, SKIP YOLO to prevent crash
    vm = psutil.virtual_memory()
    if vm.available < 150 * 1024 * 1024:
        print("⚠️ Low Memory! Skipping YOLO Confidence check to prevent crash.")
        return "Skipped (Low RAM)"

    if doc_type not in MODEL_PATHS or not os.path.exists(MODEL_PATHS[doc_type]):
        return None
    model = None
    try:
        log_memory("Before YOLO Load")
        model = YOLO(MODEL_PATHS[doc_type])
        results = model(image_path, verbose=False)
        conf = 0.0
        if results and results[0].boxes:
            conf = results[0].boxes.conf.max().item()
        elif results and results[0].probs:
            conf = results[0].probs.top1conf.item()
        return f"{conf * 100:.2f}%"
    except:
        return None
    finally:
        if model: 
            del model
        gc.collect()
        log_memory("After YOLO Cleanup")

# ==========================================
# 3. EXTRACTION LOGIC
# ==========================================

def detect_document_type(text):
    t = text.upper()
    if re.search(r'\b\d{4}\s\d{4}\s\d{4}\b', text): return "AADHAAR"
    if "INCOME" in t and "TAX" in t: return "PAN"
    if "PERMANENT ACCOUNT" in t or "P.A.N" in t: return "PAN"
    if "DRIVING" in t and "LICENCE" in t: return "DRIVING"
    if "ELECTION" in t or "COMMISSION" in t: return "VOTER"
    return "Unknown_Document"

def is_back_side(text):
    t = text.upper()
    if "DOB" in t or "DATE" in t or "INCOME" in t: return False
    if "ADDRESS" in t or "PATA" in t or re.search(r'\b3\d{5}\b', text): return True
    return False

def extract_regex_data(text, doc_type):
    res = {"ID Number": None, "VID Number": None}
    if doc_type == "AADHAAR":
        match = re.search(r'\b\d{4}\s\d{4}\s\d{4}\b', text)
        if match: res["ID Number"] = match.group()
    elif doc_type == "PAN":
        m = re.search(r'[A-Z]{5}[0-9]{4}[A-Z]', text)
        if m: res["ID Number"] = m.group()
    elif doc_type == "VOTER":
        m = re.search(r'[A-Z]{3}[0-9]{7}|[A-Z]{2,3}[/]\d{2}[/]\d{3}[/]\d{6}', text)
        if m: res["ID Number"] = m.group()
    return res

def post_process(data, doc_type, is_back):
    clean = {}
    allowed_fields = []
    if doc_type == "AADHAAR":
        allowed_fields = ["Address", "ID Number", "VID Number"] if is_back else ["Name", "DOB", "Gender", "ID Number", "VID Number"]
    elif doc_type == "PAN":
        allowed_fields = ["Name", "Parent Name", "DOB", "ID Number"]
    elif doc_type == "VOTER":
        allowed_fields = ["Name", "Parent Name", "DOB", "ID Number", "Gender"]
    elif doc_type == "DRIVING":
        allowed_fields = ["Name", "Parent Name", "ID Number", "DOB", "Issue Date", "Validity", "Address"]
    else:
        allowed_fields = list(data.keys())

    for k in allowed_fields:
        v = data.get(k, "")
        if v and str(v).lower() not in ['null', 'none', 'n/a']:
            clean[k] = str(v).strip()
    
    if "ID Number" in clean and doc_type == "AADHAAR":
        clean["ID Number"] = re.sub(r'[^\d\s]', '', clean["ID Number"])[:14]

    return clean

def process_document(image_path, target_doc_type=None):
    try:
        # 1. OPTIMIZE IMAGE (Resize to 800px)
        optimize_image(image_path)
        gc.collect()

        # 2. Run Lazy OCR
        raw_text = run_lazy_ocr(image_path)
        
        # COOL DOWN PHASE (Critical for Render Free Tier)
        # Give OS 1 second to reclaim memory pages before next heavy task
        time.sleep(1) 
        
        if not raw_text.strip():
            return {"success": False, "error": "Could not extract text. Image too blurry."}

        # 3. Detect Type
        detected_type = detect_document_type(raw_text)
        is_back = is_back_side(raw_text)

        if target_doc_type and detected_type != "Unknown_Document" and detected_type != target_doc_type:
             return {"success": False, "error": "Document mismatch detected!", "doc_type": detected_type}

        # 4. Extract Data (Regex Only - Ollama Removed)
        # Since Ollama is disabled, we rely on Regex + Post processing
        final_data = extract_regex_data(raw_text, detected_type)
        
        dob_match = re.search(r'\b\d{2}/\d{2}/\d{4}\b', raw_text)
        if dob_match and not final_data.get("DOB"):
            final_data["DOB"] = dob_match.group()

        final_data = post_process(final_data, detected_type, is_back)

        # 5. Lazy YOLO Confidence (with Memory Check)
        if detected_type in MODEL_PATHS:
            conf = get_yolo_confidence(detected_type, image_path)
            if conf: final_data["AI Confidence"] = conf

        return {"success": True, "data": final_data, "doc_type": detected_type, "is_back": is_back}

    except Exception as e:
        print(f"Error: {e}")
        return {"success": False, "error": str(e)}
    finally:
        gc.collect()

# ==========================================
# 4. ROUTES
# ==========================================

@app.get("/", response_class=FileResponse)
async def root():
    return FileResponse(os.path.join(BASE_DIR, 'templates', 'index.html'))

@app.get("/api/health")
async def health():
    return {"status": "healthy"}

@app.post("/api/upload")
async def upload(file: UploadFile = File(...), doc_type: str = Form(None)):
    if not file.filename: raise HTTPException(status_code=400, detail="No file")
    if doc_type in ["null", "undefined", ""]: doc_type = None

    filepath = os.path.join(UPLOAD_FOLDER, file.filename)
    try:
        with open(filepath, "wb") as f:
            contents = await file.read()
            if len(contents) > MAX_FILE_SIZE:
                 raise HTTPException(status_code=400, detail="File too large (Max 10MB)")
            f.write(contents)

        # Process (Threaded)
        result = await asyncio.to_thread(process_document, filepath, doc_type)
        return result

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if os.path.exists(filepath): os.remove(filepath)
        gc.collect()
