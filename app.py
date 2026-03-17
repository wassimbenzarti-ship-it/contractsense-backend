from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
import anthropic
import json
import os
import io
import uuid
from docx import Document
from docx.oxml.ns import qn
from docx.oxml import OxmlElement
from lxml import etree
import datetime

app = Flask(__name__)
CORS(app)

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

def extract_text_from_docx(file_bytes):
    doc = Document(io.BytesIO(file_bytes))
    text = []
    for para in doc.paragraphs:
        if para.text.strip():
            text.append(para.text)
    return "\n".join(text)

def analyze_contract(contract_text, lang, contract_type):
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    
    system = f"""Tu es un juriste expert. Analyse ce contrat et propose des modifications pour protéger la partie bénéficiaire.
LANGUE: Réponds UNIQUEMENT en {'anglais' if lang == 'en' else 'français'}.
Type de contrat: {contract_type}

Retourne UNIQUEMENT du JSON valide, sans markdown:
{{"modifications":[{{"id":1,"clause_name":"nom court","risk":"high|medium|low","reason":"Une phrase expliquant le risque.","original":"texte exact copié du contrat","proposed":"clause complète et professionnelle"}}]}}

Règles:
- Exactement 5 modifications
- original: copie mot pour mot du contrat, max 50 mots
- proposed: clause complète et professionnelle, bien rédigée
- reason: 1 phrase claire
- clause_name: max 5 mots
- Priorités: responsabilité, résiliation, propriété intellectuelle, pénalités, confidentialité"""

    message = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=4000,
        system=system,
        messages=[{"role": "user", "content": f"Contrat:\n\n{contract_text[:50000]}\n\nRetourne le JSON."}]
    )
    
    raw = message.content[0].text
    # Extract JSON
    import re
    match = re.search(r'\{[\s\S]*\}', raw)
    if not match:
        raise ValueError("Réponse invalide")
    return json.loads(match.group(0))

def apply_track_changes(file_bytes, modifications, decisions):
    doc = Document(io.BytesIO(file_bytes))
    author = "ContractSense"
    date = datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%SZ")
    rev_id = 1

    accepted = [m for m in modifications if decisions.get(str(m["id"])) == "accepted"]

    for para in doc.paragraphs:
        for mod in accepted:
            original = mod.get("original", "").strip()
            proposed = mod.get("proposed", "").strip()
            if not original or not proposed:
                continue
            if original in para.text:
                # Clear paragraph runs
                for run in para.runs:
                    run.text = ""

                # Build track change XML
                p = para._p

                # Delete element (red strikethrough)
                del_elem = OxmlElement('w:del')
                del_elem.set(qn('w:id'), str(rev_id))
                del_elem.set(qn('w:author'), author)
                del_elem.set(qn('w:date'), date)
                del_run = OxmlElement('w:r')
                del_rpr = OxmlElement('w:rPr')
                del_run.append(del_rpr)
                del_text = OxmlElement('w:delText')
                del_text.set('{http://www.w3.org/XML/1998/namespace}space', 'preserve')
                del_text.text = original
                del_run.append(del_text)
                del_elem.append(del_run)
                p.append(del_elem)
                rev_id += 1

                # Insert element (green)
                ins_elem = OxmlElement('w:ins')
                ins_elem.set(qn('w:id'), str(rev_id))
                ins_elem.set(qn('w:author'), author)
                ins_elem.set(qn('w:date'), date)
                ins_run = OxmlElement('w:r')
                ins_text = OxmlElement('w:t')
                ins_text.set('{http://www.w3.org/XML/1998/namespace}space', 'preserve')
                ins_text.text = proposed
                ins_run.append(ins_text)
                ins_elem.append(ins_run)
                p.append(ins_elem)
                rev_id += 1
                break

    output = io.BytesIO()
    doc.save(output)
    output.seek(0)
    return output

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})

@app.route("/analyze", methods=["POST"])
def analyze():
    try:
        file = request.files.get("file")
        lang = request.form.get("lang", "fr")
        contract_type = request.form.get("type", "generic")

        if not file:
            return jsonify({"error": "Fichier manquant"}), 400

        file_bytes = file.read()
        filename = file.filename.lower()

        if filename.endswith(".docx") or filename.endswith(".doc"):
            contract_text = extract_text_from_docx(file_bytes)
        else:
            contract_text = file_bytes.decode("utf-8", errors="ignore")

        result = analyze_contract(contract_text, lang, contract_type)
        return jsonify(result)

    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/export", methods=["POST"])
def export():
    try:
        file = request.files.get("file")
        modifications = json.loads(request.form.get("modifications", "[]"))
        decisions = json.loads(request.form.get("decisions", "{}"))

        if not file:
            return jsonify({"error": "Fichier manquant"}), 400

        file_bytes = file.read()
        output = apply_track_changes(file_bytes, modifications, decisions)

        return send_file(
            output,
            mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            as_attachment=True,
            download_name="contrat-track-changes.docx"
        )

    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
