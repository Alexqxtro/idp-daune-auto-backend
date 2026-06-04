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

# Limita de upload pentru a evita blocarea serverului pe fisiere foarte mari
app.config["MAX_CONTENT_LENGTH"] = 80 * 1024 * 1024  # 80 MB

client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))

ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "pdf", "zip"}

# Setari pentru reducerea consumului de RAM
MAX_FILES_PER_REQUEST = 35
MAX_TEXT_CHARS_PER_PDF = 2200
MAX_PDF_TEXT_PAGES = 4
MAX_IMAGE_DIMENSION = 1200
IMAGE_JPEG_QUALITY = 65
MAX_SCANNED_PDF_PAGES_AS_IMAGE = 2


def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def compress_image_to_data_url(file_bytes, max_dimension=MAX_IMAGE_DIMENSION, quality=IMAGE_JPEG_QUALITY):
    """
    Primeste bytes de imagine si returneaza data URL JPEG comprimat.
    Reduce memoria si dimensiunea payloadului trimis catre OpenAI.
    """
    try:
        img = Image.open(BytesIO(file_bytes))

        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")
        elif img.mode != "RGB":
            img = img.convert("RGB")

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
    Extrage text din PDF si il limiteaza ca sa nu depasim limita de tokeni.
    Pentru polite CASCO, taloane si documente text, textul extras e mai sigur decat imaginea.
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
    Folosit pentru PDF-uri scanate sau fara text extractabil.
    Randam primele pagini si le comprimam ca JPEG.
    """
    images = []

    try:
        pdf = fitz.open(stream=pdf_bytes, filetype="pdf")

        for page_index in range(min(len(pdf), max_pages)):
            page = pdf[page_index]

            # Zoom redus pentru consum mai mic de memorie
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
        print(f"Eroare conversie PDF scanat in imagine: {e}")

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
    1. incearca sa extraga text;
    2. daca exista text, trimite textul;
    3. daca nu exista text, trimite primele pagini ca imagini comprimate.
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
            "text": f"PDF scanat fara text extractabil: {filename}. Analizeaza imaginile primelor pagini."
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
        return jsonify({"error": "Nu ai trimis niciun fisier."}), 400

    files = request.files.getlist("files")
    if not files or files[0].filename == "":
        return jsonify({"error": "Lista de fisiere este goala."}), 400

    content = [
        {
            "type": "text",
            "text": f"""
Analizeaza documentele extrase din dosarul de dauna auto incarcat.
Data curenta este: {datetime.date.today().isoformat()}.

Trebuie sa identifici pentru FIECARE document gasit in lista:
- tipul documentului: buletin, permis_conducere, talon_auto, polita_casco, contract_cesiune, contract_mandat, imputernicire_proprietar, fotografie_parbriz_avariat, foto_cod_parbriz_avariat, foto_serie_vin, foto_parbriz_inlocuit_cu_cod_nou;
- daca documentul este lizibil;
- daca exista o data de expirare;
- daca documentul este valid sau expirat raportat la data curenta;
- observatii clare pentru utilizator in limba romana.

REGULI IMPORTANTE PENTRU POLITA CASCO:
- Pentru polita CASCO, data de expirare trebuie extrasa din campuri precum „Perioada asigurata”, „Valabilitate”, „de la ... la ...”.
- Daca apare o perioada de forma „de la 30.09.2025 la 29.09.2028”, atunci data de expirare este 29.09.2028.
- Nu confunda data emiterii politei, data contractului, data platii sau data inceputului valabilitatii cu data expirarii.
- Nu marca polita CASCO drept expirata daca data curenta este inainte de data finala a perioadei asigurate.
- Daca textul extras din PDF contine perioada de valabilitate, acorda prioritate textului extras fata de interpretarea vizuala a imaginii.

REGULI DE VALIDARE:
- Daca documentul are o perioada de valabilitate cu data de inceput si data de sfarsit, data de expirare este data de sfarsit.
- Daca data curenta este inainte sau egala cu data de expirare, documentul este valid.
- Daca data curenta este dupa data de expirare, documentul este invalid/expirat.
- Daca nu exista data de expirare clara, pune expiration_date: null si explica in observations.

REGULI PENTRU DOCUMENTE SCANATE:
- Daca documentul este scanat si textul nu este extractabil, foloseste imaginile primelor pagini pentru identificare.
- Daca documentul nu poate fi identificat cu certitudine, foloseste document_type: "unknown" si validity_status: "unknown".
- Pentru documentele scanate clare, poti marca documentul valid daca este lizibil si corespunde tipului identificat.

IMPORTANT:
- Analizeaza doar fisierele primite.
- Pentru missing_documents foloseste doar cheile obligatorii din lista.
- Daca un document nu se potriveste exact listei obligatorii, identifica-l cat mai clar in document_type, dar nu il considera document obligatoriu indeplinit.
- Returneaza strict JSON valid, fara formatare markdown.

Structura JSON obligatorie:
{{
  "overall_status": "valid" sau "invalid",
  "summary": "rezumat scurt in romana",
  "documents": [
    {{
      "file_name": "nume fisier original",
      "document_type": "tip_detectat_exact_din_lista_de_sus_sau_unknown",
      "validity_status": "valid" sau "invalid" sau "unknown",
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

        # Procesare arhiva ZIP
        if extension == "zip":
            try:
                with zipfile.ZipFile(BytesIO(file_bytes)) as z:
                    for zip_info in z.infolist():
                        if total_processed >= MAX_FILES_PER_REQUEST:
                            skipped_files.append(zip_info.filename)
                            continue

                        # Ignoram folderele goale si fisierele de sistem ascunse
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
                            "text": f"Fisier extras din ZIP: {z_filename}"
                        })

                        if z_ext in ["png", "jpg", "jpeg"]:
                            data_url = compress_image_to_data_url(z_bytes)
                            add_image_content(content, data_url)

                        elif z_ext == "pdf":
                            add_pdf_content(content, z_filename, z_bytes)

            except Exception as e:
                return jsonify({
                    "success": False,
                    "error": f"Arhiva ZIP nevalida: {str(e)}"
                }), 400

        # Procesare imagini directe
        elif extension in ["png", "jpg", "jpeg"]:
            processed_files_names.append(filename)
            total_processed += 1

            content.append({
                "type": "text",
                "text": f"Fisier: {filename}"
            })

            data_url = compress_image_to_data_url(file_bytes)
            add_image_content(content, data_url)

        # Procesare PDF direct
        elif extension == "pdf":
            processed_files_names.append(filename)
            total_processed += 1

            content.append({
                "type": "text",
                "text": f"Fisier: {filename}"
            })

            add_pdf_content(content, filename, file_bytes)

    if not processed_files_names:
        return jsonify({
            "error": "Nu s-a gasit niciun document valid in fisierele trimise."
        }), 400

    try:
        response = client.chat.completions.create(
            model="gpt-4o",
            response_format={"type": "json_object"},
            messages=[
                {
                    "role": "system",
                    "content": "Esti un sistem IDP pentru dosare de dauna auto. Raspunzi exclusiv in JSON valid."
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
