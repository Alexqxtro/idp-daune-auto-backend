import os
import json
import base64
import datetime
import zipfile
from io import BytesIO

import fitz  # PyMuPDF
from PIL import Image
from flask import Flask, request, jsonify
from flask_cors import CORS
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
CORS(app)

# Limită de upload ca să nu cadă serverul pe fișiere foarte mari
app.config["MAX_CONTENT_LENGTH"] = 80 * 1024 * 1024  # 80 MB

client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))

ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "pdf", "zip"}

# Setări pentru reducerea consumului de RAM
MAX_FILES_PER_REQUEST = 35
MAX_TEXT_CHARS_PER_PDF = 1800
MAX_PDF_TEXT_PAGES = 4
MAX_IMAGE_DIMENSION = 1200
IMAGE_JPEG_QUALITY = 65
MAX_SCANNED_PDF_PAGES_AS_IMAGE = 1


def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def compress_image_to_data_url(file_bytes, max_dimension=MAX_IMAGE_DIMENSION, quality=IMAGE_JPEG_QUALITY):
    """
    Primește bytes de imagine și returnează data URL JPEG comprimat.
    Reduce mult memoria și dimensiunea payloadului trimis către OpenAI.
    """
    try:
        img = Image.open(BytesIO(file_bytes))

        # Convertim totul în RGB pentru JPEG
        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")
        elif img.mode != "RGB":
            img = img.convert("RGB")

        # Redimensionare proporțională
        img.thumbnail((max_dimension, max_dimension))

        output = BytesIO()
        img.save(output, format="JPEG", quality=quality, optimize=True)

        b64 = base64.b64encode(output.getvalue()).decode("utf-8")
        return f"data:image/jpeg;base64,{b64}"

    except Exception as e:
        print(f"Eroare comprimare imagine: {e}")
        return None


def pdf_to_text(pdf_bytes, max_pages=MAX_PDF_TEXT_PAGES, max_chars=MAX_TEXT_CHARS_PER_PDF):
    """
    Extrage text din PDF, dar îl limitează ca să nu depășim limita de tokeni.
    Pentru polițe CASCO și taloane, textul extras e mai sigur decât analiza imaginii.
    """
    text_parts = []

    try:
        pdf = fitz.open(stream=pdf_bytes, filetype="pdf")

        for page_index in range(min(len(pdf), max_pages)):
            page_text = pdf[page_index].get_text()
            if page_text.strip():
                text_parts.append(page_text)

        full_text = "\n".join(text_parts).strip()

        if len(full_text) > max_chars:
            full_text = full_text[:max_chars] + "\n[TEXT TRUNCHIAT PENTRU LIMITA DE TOKENI]"

        return full_text

    except Exception as e:
        print(f"Eroare extragere text PDF: {e}")
        return ""


def pdf_to_compressed_images(pdf_bytes, max_pages=MAX_SCANNED_PDF_PAGES_AS_IMAGE):
    """
    Folosit doar pentru PDF-uri scanate, fără text extractabil.
    Randăm maximum 1 pagină și o comprimăm ca JPEG.
    """
    images = []

    try:
        pdf = fitz.open(stream=pdf_bytes, filetype="pdf")

        for page_index in range(min(len(pdf), max_pages)):
            page = pdf[page_index]

            # Zoom mai mic decât înainte pentru consum redus de memorie
            pix = page.get_pixmap(matrix=fitz.Matrix(1.2, 1.2))
            png_bytes = pix.tobytes("png")

            data_url = compress_image_to_data_url(
                png_bytes,
                max_dimension=MAX_IMAGE_DIMENSION,
                quality=IMAGE_JPEG_QUALITY
            )

            if data_url:
                images.append(data_url)

    except Exception as e:
        print(f"Eroare conversie PDF scanat în imagine: {e}")

    return images


def add_image_content(content, data_url):
    if data_url:
        content.append({
            "type": "image_url",
            "image_url": {
                "url": data_url
            }
        })


def add_pdf_content(content, filename, pdf_bytes):
    """
    Pentru PDF:
    1. încearcă să extragă text;
    2. dacă există text, trimite doar textul;
    3. dacă nu există text, trimite doar prima pagină ca imagine comprimată.
    """
    pdf_text = pdf_to_text(pdf_bytes)

    if pdf_text:
        content.append({
            "type": "text",
            "text": f"Text extras din PDF {filename}:\n{pdf_text}"
        })
    else:
        content.append({
            "type": "text",
            "text": f"PDF scanat fără text extractabil: {filename}. Analizează imaginea primei pagini."
        })

        pdf_images = pdf_to_compressed_images(pdf_bytes)

        for data_url in pdf_images:
            add_image_content(content, data_url)


@app.route("/", methods=["GET"])
def home():
    return jsonify({
        "status": "online",
        "message": "IDP Daune Auto backend este activ."
    })


@app.route("/api/analyze", methods=["POST"])
def analyze_documents():
    if "files" not in request.files:
        return jsonify({"error": "Nu ai trimis niciun fișier."}), 400

    files = request.files.getlist("files")
    if not files or files[0].filename == "":
        return jsonify({"error": "Lista de fișiere este goală."}), 400

    content = [
        {
            "type": "text",
            "text": f"""
Analizează documentele extrase din dosarul de daună auto încărcat.
Data curentă este: {datetime.date.today().isoformat()}.

Trebuie să identifici pentru FIECARE document găsit în listă:
- tipul documentului: buletin, permis_conducere, talon_auto, polita_casco, contract_cesiune, contract_mandat, imputernicire_proprietar, fotografie_parbriz_avariat, foto_cod_parbriz_avariat, foto_serie_vin, foto_parbriz_inlocuit_cu_cod_nou;
- dacă documentul este lizibil;
- dacă există o dată de expirare;
- dacă documentul este valid sau expirat raportat la data curentă;
- observații clare pentru utilizator în limba română.

REGULI IMPORTANTE PENTRU POLIȚA CASCO:
- Pentru polița CASCO, data de expirare trebuie extrasă din câmpuri precum „Perioada asigurată”, „Valabilitate”, „de la ... la ...”.
- Dacă apare o perioadă de forma „de la 30.09.2025 la 29.09.2028”, atunci data de expirare este 29.09.2028.
- Nu confunda data emiterii poliței, data contractului, data plății sau data începutului valabilității cu data expirării.
- Nu marca polița CASCO drept expirată dacă data curentă este înainte de data finală a perioadei asigurate.
- Dacă textul extras din PDF conține perioada de valabilitate, acordă prioritate textului extras față de interpretarea vizuală a imaginii.

REGULI DE VALIDARE:
- Dacă documentul are o perioadă de valabilitate cu dată de început și dată de sfârșit, data de expirare este data de sfârșit.
- Dacă data curentă este înainte sau egală cu data de expirare, documentul este valid.
- Dacă data curentă este după data de expirare, documentul este invalid/expirat.
- Dacă nu există dată de expirare clară, pune expiration_date: null și explică în observations.

IMPORTANT:
- Analizează doar fișierele primite.
- Dacă un document nu se potrivește exact listei obligatorii, identifică-l cât mai clar în document_type, dar nu îl pune în missing_documents ca document obligatoriu îndeplinit.
- Pentru missing_documents folosește doar cheile obligatorii din listă.
- Returnează strict JSON valid, fără formatare markdown.

Structura JSON obligatorie:
{{
  "overall_status": "valid" sau "invalid",
  "summary": "rezumat scurt în română",
  "documents": [
    {{
      "file_name": "nume fisier original",
      "document_type": "tip_detectat_exact_din_lista_de_sus",
      "validity_status": "valid" sau "invalid",
      "expiration_date": "YYYY-MM-DD sau null",
      "is_readable": true sau false,
      "observations": "observatii clare in romana"
    }}
  ],
  "missing_documents": [
    "lista cheilor de documente obligatorii lipsa daca este cazul"
  ]
}}
"""
        }
    ]

    processed_files_names = []
    total_processed = 0
    skipped_files = []

    for file in files:
        filename = file.filename

        if not allowed_file(filename):
            skipped_files.append(filename)
            continue

        if total_processed >= MAX_FILES_PER_REQUEST:
            skipped_files.append(filename)
            continue

        file_bytes = file.read()
        extension = filename.rsplit(".", 1)[1].lower()

        # Procesare arhivă ZIP
        if extension == "zip":
            try:
                with zipfile.ZipFile(BytesIO(file_bytes)) as z:
                    for zip_info in z.infolist():
                        if total_processed >= MAX_FILES_PER_REQUEST:
                            skipped_files.append(zip_info.filename)
                            continue

                        # Ignorăm folderele goale și fișierele de sistem ascunse
                        if (
                            zip_info.is_dir()
                            or zip_info.filename.startswith("__")
                            or "/." in zip_info.filename
                            or zip_info.filename.split("/")[-1].startswith(".")
                        ):
                            continue

                        z_filename = zip_info.filename.split("/")[-1]
                        if not z_filename:
                            continue

                        z_ext = z_filename.rsplit(".", 1)[1].lower() if "." in z_filename else ""
                        if z_ext not in ["png", "jpg", "jpeg", "pdf"]:
                            skipped_files.append(z_filename)
                            continue

                        z_bytes = z.read(zip_info.filename)

                        processed_files_names.append(z_filename)
                        total_processed += 1

                        content.append({
                            "type": "text",
                            "text": f"Fișier extras din ZIP: {z_filename}"
                        })

                        if z_ext in ["png", "jpg", "jpeg"]:
                            data_url = compress_image_to_data_url(z_bytes)
                            add_image_content(content, data_url)

                        elif z_ext == "pdf":
                            add_pdf_content(content, z_filename, z_bytes)

            except Exception as e:
                return jsonify({
                    "success": False,
                    "error": f"Arhiva ZIP nevalidă: {str(e)}"
                }), 400

        # Procesare imagini directe
        elif extension in ["png", "jpg", "jpeg"]:
            processed_files_names.append(filename)
            total_processed += 1

            content.append({
                "type": "text",
                "text": f"Fișier: {filename}"
            })

            data_url = compress_image_to_data_url(file_bytes)
            add_image_content(content, data_url)

        # Procesare PDF direct
        elif extension == "pdf":
            processed_files_names.append(filename)
            total_processed += 1

            content.append({
                "type": "text",
                "text": f"Fișier: {filename}"
            })

            add_pdf_content(content, filename, file_bytes)

    if not processed_files_names:
        return jsonify({
            "error": "Nu s-a găsit niciun document valid în fișierele trimise."
        }), 400

    try:
        response = client.chat.completions.create(
            model="gpt-4o",
            response_format={"type": "json_object"},
            messages=[
                {
                    "role": "system",
                    "content": "Ești un sistem IDP pentru dosare de daună auto. Răspunzi exclusiv în JSON valid."
                },
                {
                    "role": "user",
                    "content": content
                }
            ],
            temperature=0
        )

        result_json = json.loads(response.choices[0].message.content)

        return jsonify({
            "success": True,
            "files_received": processed_files_names,
            "files_skipped": skipped_files,
            "analysis": result_json
        })

    except Exception as e:
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
