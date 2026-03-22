import os
import uuid
import subprocess
from fastapi import FastAPI, UploadFile, Form, HTTPException, File
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

app = FastAPI()

# Temporary directory for uploads
TEMP_DIR = "/tmp/cups_uploads"
os.makedirs(TEMP_DIR, exist_ok=True)

@app.get("/", response_class=HTMLResponse)
async def serve_frontend():
    with open("index.html", "r") as f:
        return f.read()

@app.get("/printers")
async def get_printers():
    try:
        # lpstat -a lists all accepting printers. 
        # We split by space and take the first item (the printer name).
        result = subprocess.run(['lpstat', '-a'], capture_output=True, text=True, check=True)
        printers = [line.split()[0] for line in result.stdout.strip().split('\n') if line]
        return {"printers": printers}
    except subprocess.CalledProcessError as e:
        return JSONResponse(status_code=503, content={"error": "Could not connect to CUPS server or no printers found."})
    except FileNotFoundError:
        return JSONResponse(status_code=500, content={"error": "CUPS client (lpstat) is not installed."})

@app.post("/print")
async def print_file(
    file: UploadFile = File(...),
    printer: str = Form(...),
    copies: int = Form(1),
    sides: str = Form("one-sided"),
    color_mode: str = Form("color")
):
    # Generate a unique secure filename
    file_id = str(uuid.uuid4())
    safe_filename = f"{file_id}_{file.filename.replace(' ', '_')}"
    file_path = os.path.join(TEMP_DIR, safe_filename)

    try:
        # Save file temporarily
        with open(file_path, "wb") as buffer:
            buffer.write(await file.read())

        # Build the lp command
        # Note: Color options can depend on PPDs, but 'ColorModel' is standard for CUPS.
        color_opt = "Color" if color_mode == "color" else "Gray"
        
        command = [
            "lp",
            "-d", printer,
            "-n", str(copies),
            "-o", f"sides={sides}",
            "-o", f"ColorModel={color_opt}",
            file_path
        ]

        # Execute
        result = subprocess.run(command, capture_output=True, text=True)

        if result.returncode != 0:
            raise HTTPException(status_code=500, detail=f"Print failed: {result.stderr}")

        return {"message": "Print job submitted successfully!", "job_id": result.stdout.strip()}

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    
    finally:
        # CRITICAL: Always clean up the uploaded file
        if os.path.exists(file_path):
            os.remove(file_path)
