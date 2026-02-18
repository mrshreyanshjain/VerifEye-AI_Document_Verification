import os
import json
import re
import cv2
import asyncio
import gc  # Garbage Collector
import shutil
from pathlib import Path
from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from ultralytics import YOLO
import easyocr

# ==========================================
# 1. CONFIGURATION & SETUP
# ==========================================

# Base directory for absolute paths
BASE_DIR = Path(__file__).resolve().parent
UPLOAD_FOLDER = os.path.join(BASE_DIR, 'uploads')
MAX_FILE_SIZE = 16 * 1024 * 1024  # 16MB

# Create uploads folder if it doesn't exist
Path(UPLOAD_FOLDER).mkdir(exist_ok=True)

app = FastAPI(title="VerifEye: AI Identity Verification System", version="2.0")

# CORS Configuration
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Add cache control headers
@app.middleware("http")
async def add_cache_control(request, call_next):
    response = await call_next(request)
    if request.url.path.endswith(('.css', '.js', '.png', '.jpg', '.ico')):
        response.headers["Cache-Control"] = "public, max-age=3600, must-revalidate"
    else:
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response

# Mount static files
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")

# ==========================================
# 2. MODEL MANAGEMENT (LAZY LOADING)
# ==========================================

MODEL_PATHS = {
    "AADHAAR": "trained_models/aadhar_best/weights/best.pt",
    "PAN": "trained_models/pan_best/weights/best.pt",
    "VOTER": "trained_models/voter_id_best/weights/best.pt",
    "DRIVING": "trained_models/driving_licence_best/weights/best.pt",
}
MODEL_NAME = "llama3"

# Check for Ollama (Optional)
try:
    import ollama
    OLLAMA_AVAILABLE = True
except ImportError:
    OLLAMA_AVAILABLE = False
    print("⚠️ 'ollama' library missing. AI extraction disabled.")

def run_lazy_ocr(image_path):
    """
    Loads EasyOCR, extracts text, then immediately removes it from RAM.
    This prevents OOM errors on Render Free Tier.
    """
    reader = None
    try:
        # Load Reader (CPU only)
        reader = easyocr.Reader(['en'], gpu=False, verbose=False)
        
        # Read Image
        img_cv = cv2.imread(image_path)
        img_cv = cv2.fastNlMeansDenoisingColored(img_cv, None, 10, 10, 7, 21)
        
        # Extract
        result = reader.readtext(img_cv, detail=0)
        return " ".join(result)
    except Exception as e:
        print(f"❌ OCR Error: {e}")
        return ""
    finally:
        # CRITICAL: Delete model and clear memory
        if reader: 
            del reader
        gc.collect()

def get_yolo_confidence(doc_type, image_path):
    """
    Loads specific YOLO model, checks confidence, then deletes it.
    """
    if doc_type not in MODEL_PATHS or not os.path.exists(MODEL_PATHS[doc_type]):
        return None

    model = None
    try:
        model = YOLO(MODEL_PATHS[doc_type])
        results = model(image_path, verbose=False)
        
        conf = 0.0
        if results:
            if results[0].boxes:
                conf = results[0].boxes.conf.max().item()
            elif results[0].probs:
                conf = results[0].probs.top1conf.item()
        
        return f"{conf * 100:.2f}%"
    except Exception as e:
        return None
    finally:
        # CRITICAL: Delete model and clear memory
        if model:
            del model
        gc.collect()

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

def analyze_with_ollama(text, doc_type, is_back):
    if not OLLAMA_AVAILABLE:
        return {}

    instr = "Extract details."
    if doc_type == "AADHAAR":
        if is_back:
            instr = "Extract Address, VID Number. Ignore Name and DOB."
        else:
            instr = "Extract Name, DOB, Gender, ID Number. Ignore Address."
    elif doc_type == "PAN":
        instr = "Extract ONLY: Name, Parent Name, DOB, ID Number. Ignore 'Income Tax Department', 'Govt of India', and Signature labels."
    elif doc_type == "DRIVING":
        instr = "Extract Name, Parent Name (Father/Husband), DOB, ID Number (License No), Address, Issue Date (DOI), Validity (Non-Transport/Transport). Distinguish clearly between DOB and Validity."

    prompt = f"""Extract {doc_type} data. {instr} Raw Text: "{text}"
    Return JSON with keys: Name, Parent Name, DOB, Gender, ID Number, VID Number, Address, Validity, Issue Date.
    Important: 
    1. If a field is not found, return null.
    2. The "Address" field must be a single string.
    3. For Driving License, "Parent Name" is usually labeled as S/O, W/O, or D/O.
    4. "Validity" is the expiry date (Valid Till)."""

    try:
        resp = ollama.chat(model=MODEL_NAME, messages=[{'role': 'user', 'content': prompt}])
        content = resp['message']['content']
        # JSON extraction logic
        start = content.find('{')
        end = content.rfind('}') + 1
        if start != -1 and end != -1:
            return json.loads(content[start:end])
        return {}
    except:
        return {}

def post_process(data, doc_type, is_back):
    clean = {}

    # --- DEFINE ALLOWED FIELDS PER SIDE/DOC ---
    allowed_fields = []
    if doc_type == "AADHAAR":
        if is_back:
            allowed_fields = ["Address", "ID Number", "VID Number"]
        else:
            allowed_fields = ["Name", "DOB", "Gender", "ID Number", "VID Number"]
    elif doc_type == "PAN":
        allowed_fields = ["Name", "Parent Name", "DOB", "ID Number"]
    elif doc_type == "VOTER":
        allowed_fields = ["Name", "Parent Name", "DOB", "ID Number", "Gender"]
    elif doc_type == "DRIVING":
        allowed_fields = ["Name", "Parent Name", "ID Number", "DOB", "Issue Date", "Validity", "Address"]
    else:
        allowed_fields = list(data.keys())

    # Copy only allowed fields
    for k in allowed_fields:
        v = data.get(k, "")
        if v and str(v).lower() not in ['null', 'none', 'n/a', 'unspecified']:
            if isinstance(v, dict) and k == "Address":
                # Flatten address dictionary if Ollama returns it as such
                addr_parts = []
                for key, val in v.items():
                    if val and str(val).lower() not in ['null', 'none', 'n/a']:
                        addr_parts.append(str(val).strip())
                clean[k] = ", ".join(addr_parts)
            else:
                clean[k] = str(v).strip()

    # --- SPECIFIC CLEANING LOGIC ---
    if doc_type == "DRIVING":
        if "DOB" in clean:
            dob_year_match = re.search(r'\d{4}', clean["DOB"])
            if dob_year_match:
                year = int(dob_year_match.group())
                if year > 2025: # Future date likely Validity
                    if "Validity" not in clean:
                        clean["Validity"] = clean["DOB"]
                    del clean["DOB"]
        if "ID Number" in clean:
            clean["ID Number"] = re.sub(r'(?i)(DL\s*No|Licence\s*No)[:\.\s-]*', '', clean["ID Number"]).strip()

    if doc_type == "AADHAAR":
        if "ID Number" in clean:
            clean["ID Number"] = re.sub(r'[^\d\s]', '', clean["ID Number"])[:14]
        if "VID Number" in clean:
            vid = re.sub(r'\D', '', clean["VID Number"])
            if len(vid) >= 16: clean["VID Number"] = vid[:16]
        if is_back and "Address" in clean:
            addr = clean["Address"]
            addr = re.sub(r'^(Address|Addr|To|Date).*?[:,-]', '', addr, flags=re.IGNORECASE)
            clean["Address"] = addr.strip(" ,;:-")

    if doc_type == "PAN":
        noise_words = ["INCOME", "TAX", "DEPARTMENT", "GOVT", "INDIA", "SIGNATURE", "CHAIRMAN"]
        for field in ["Name", "Parent Name"]:
            if field in clean:
                val = clean[field]
                for noise in noise_words:
                    if noise.lower() in val.lower():
                        val = re.sub(f"(?i){noise}", "", val).strip()
                if len(val) < 3 or not re.search(r'[a-zA-Z]', val):
                    del clean[field]
                else:
                    clean[field] = val

    return clean

def _save_document_data(data, doc_type):
    """Helper to save or update document data in JSON files"""
    if not data or not doc_type:
        return False
        
    filename = f"{doc_type.upper()}.json"
    filepath = os.path.join(BASE_DIR, filename)
    
    existing_records = []
    if os.path.exists(filepath):
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                existing_records = json.load(f)
                if not isinstance(existing_records, list):
                    existing_records = [existing_records]
        except:
            existing_records = []
    
    id_number = data.get("ID Number")
    updated = False
    
    if id_number:
        for i, record in enumerate(existing_records):
            if record.get("ID Number") == id_number:
                record.update(data)
                updated = True
                break
    else:
        for record in existing_records:
            if record == data:
                updated = True 
                break
    
    if not updated:
        existing_records.append(data)
        
    with open(filepath, 'w', encoding='utf-8') as f:
        json.dump(existing_records, f, indent=4, ensure_ascii=False)
    
    return updated

def process_document(image_path, target_doc_type=None):
    try:
        # 1. OCR (LAZY LOADED)
        # This function loads EasyOCR, runs it, and deletes it immediately
        raw_text = run_lazy_ocr(image_path)
        
        if not raw_text.strip():
            return {
                "success": False,
                "error": "Could not extract text. Image might be too blurry."
            }

        # 2. Detect document type
        detected_type = detect_document_type(raw_text)
        is_back = is_back_side(raw_text)

        # 3. Check Mismatch
        if target_doc_type and detected_type != "Unknown_Document" and detected_type != target_doc_type:
            print(f"[DEBUG] Mismatch Detected: Expected {target_doc_type}, Got {detected_type}")
            return {
                "success": False,
                "error": f"Document mismatch detected!", 
                "data": {},
                "doc_type": detected_type,
                "is_back": is_back
            }

        # 4. Extract data (Only runs if no mismatch)
        regex_data = extract_regex_data(raw_text, detected_type)
        ai_data = analyze_with_ollama(raw_text, detected_type, is_back)

        final_data = ai_data.copy()
        if regex_data.get("ID Number"):
            final_data["ID Number"] = regex_data["ID Number"]
        if regex_data.get("VID Number"):
            final_data["VID Number"] = regex_data["VID Number"]
        
        dob_match = re.search(r'\b\d{2}/\d{2}/\d{4}\b', raw_text)
        if dob_match and (not final_data.get("DOB") or "1008" in final_data.get("DOB", "")):
            final_data["DOB"] = dob_match.group()

        # 5. Post-process
        final_data = post_process(final_data, detected_type, is_back)
        
        # 6. Get YOLO Confidence (LAZY LOADED)
        # Only run if we actually know what model to look for
        if detected_type in MODEL_PATHS:
            conf = get_yolo_confidence(detected_type, image_path)
            if conf:
                final_data["AI Confidence"] = conf

        return {
            "success": True,
            "data": final_data,
            "doc_type": detected_type,
            "is_back": is_back,
            "raw_text": raw_text
        }
    except Exception as e:
        print(f"[ERROR] Processing failed: {e}")
        return {
            "success": False,
            "error": str(e)
        }
    finally:
        # Force garbage collection again just to be safe
        gc.collect()

# ==========================================
# 4. ROUTES
# ==========================================

@app.get("/", response_class=FileResponse)
async def root():
    """Serve the main HTML file"""
    return FileResponse(os.path.join(BASE_DIR, 'templates', 'index.html'))

@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return FileResponse(os.path.join(BASE_DIR, 'static', 'favicon.ico')) if os.path.exists(os.path.join(BASE_DIR, 'static', 'favicon.ico')) else HTMLResponse(status_code=204)

@app.post("/api/upload")
async def upload(file: UploadFile = File(...), doc_type: str = Form(...)):
    """Upload and process a document image"""
    if not file.filename:
        raise HTTPException(status_code=400, detail="No file selected")

    if file.content_type not in ["image/jpeg", "image/png", "image/jpg"]:
        raise HTTPException(status_code=400, detail="Invalid file type. Use JPG or PNG only.")

    if doc_type in ["null", "undefined", ""]:
        doc_type = None

    try:
        # Save uploaded file
        filepath = os.path.join(UPLOAD_FOLDER, file.filename)
        with open(filepath, "wb") as f:
            contents = await file.read()
            if len(contents) > MAX_FILE_SIZE:
                raise HTTPException(status_code=400, detail="File too large. Max 16MB.")
            f.write(contents)

        # Process document
        result = await asyncio.to_thread(process_document, filepath, doc_type)
        print("[DEBUG] Extraction result:", result) 

        # If extraction returns empty data, treat as error
        if result.get("success") and (not result.get("data") or not any(result["data"].values())):
            result = {
                "success": False,
                "error": "No details could be extracted. Please try a clearer image.",
                "doc_type": result.get("doc_type", doc_type),
                "data": {},
            }

        # Auto-save data if successful
        if result.get("success"):
            actual_doc_type = result.get("doc_type", doc_type)
            if actual_doc_type and actual_doc_type != "Unknown_Document":
                _save_document_data(result["data"], actual_doc_type)

        return result

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        # Cleanup file and memory
        if os.path.exists(filepath):
            os.remove(filepath)
        gc.collect()

@app.post("/api/save")
async def save_data(payload: dict):
    """Save or update extracted data manually."""
    try:
        data = payload.get("data")
        doc_type = payload.get("doc_type", "GENERAL")
        
        if not data:
            raise HTTPException(status_code=400, detail="No data provided")
            
        updated = _save_document_data(data, doc_type)
            
        action = "updated" if updated else "saved"
        return {"success": True, "message": f"Data {action} successfully"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/health")
async def health():
    """Health check endpoint"""
    return {
        "status": "healthy",
        "mode": "lazy_loading_enabled",
    }
