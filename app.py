import os
import json
import base64
import datetime
import zipfile
from io import BytesIO

import fitz  # PyMuPDF
from flask import Flask, request, jsonify
from flask_cors import CORS
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
CORS(app)

client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))

ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "pdf", "zip"}


def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def image_to_base64(file_bytes):
    return base64.b64encode(file_bytes).decode("utf-8")


def pdf_to_base64_images(pdf_bytes, max_pages=1):
    images = []
    try:
        pdf = fitz.open(stream=pdf_bytes, filetype="pdf")
        for page_index in range(min(len(pdf), max_pages)):
            page = pdf[page_index]
            pix = page.get_pixmap(matrix=fitz.Matrix(2, 2))
            png_bytes = pix.tobytes("png")
            images.append(base64.b64encode(png_bytes).decode("utf-8"))
    except Exception as e:
        print(f"Eroare conversie PDF: {e}")
    return images


def pdf_to_text(pdf_bytes, max_pages=3, max_chars=2500):
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

Returnează strict JSON valid, fără formatare markdown.

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

    for file in files:
        filename = file.filename

        if not allowed_file(filename):
            continue

        file_bytes = file.read()
        extension = filename.rsplit(".", 1)[1].lower()

        # Procesare arhivă ZIP
        if extension == "zip":
            try:
                with zipfile.ZipFile(BytesIO(file_bytes)) as z:
                    for zip_info in z.infolist():
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
                            continue

                        z_bytes = z.read(zip_info.filename)
                        processed_files_names.append(z_filename)

                        content.append({
                            "type": "text",
                            "text": f"Fișier extras din ZIP: {z_filename}"
                        })

                        if z_ext in ["png", "jpg", "jpeg"]:
                            mime = "image/png" if z_ext == "png" else "image/jpeg"
                            b64 = base64.b64encode(z_bytes).decode("utf-8")

                            content.append({
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:{mime};base64,{b64}"
                                }
                            })

                        elif z_ext == "pdf":
                            pdf_text = pdf_to_text(z_bytes, max_pages=3, max_chars=2500)

                            if pdf_text:
                                content.append({
                                    "type": "text",
                                    "text": f"Text extras din PDF {z_filename}:\n{pdf_text}"
                                })
                            else:
                                pdf_imgs = pdf_to_base64_images(z_bytes, max_pages=1)
                                for idx, img_b64 in enumerate(pdf_imgs):
                                    content.append({
                                        "type": "image_url",
                                        "image_url": {
                                            "url": f"data:image/png;base64,{img_b64}"
                                        }
                                    })

            except Exception as e:
                return jsonify({
                    "success": False,
                    "error": f"Arhiva ZIP nevalidă: {str(e)}"
                }), 400

        # Procesare imagini directe externe
        elif extension in ["png", "jpg", "jpeg"]:
            processed_files_names.append(filename)
            mime = "image/png" if extension == "png" else "image/jpeg"
            b64 = image_to_base64(file_bytes)

            content.append({
                "type": "text",
                "text": f"Fișier: {filename}"
            })

            content.append({
                "type": "image_url",
                "image_url": {
                    "url": f"data:{mime};base64,{b64}"
                }
            })

        # Procesare PDF direct extern
        elif extension == "pdf":
            processed_files_names.append(filename)

            content.append({
                "type": "text",
                "text": f"Fișier: {filename}"
            })

            pdf_text = pdf_to_text(file_bytes, max_pages=3, max_chars=2500)

            if pdf_text:
                content.append({
                    "type": "text",
                    "text": f"Text extras din PDF {filename}:\n{pdf_text}"
                })
            else:
                pdf_imgs = pdf_to_base64_images(file_bytes, max_pages=1)
                for idx, img_b64 in enumerate(pdf_imgs):
                    content.append({
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/png;base64,{img_b64}"
                        }
                    })

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
