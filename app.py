from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
import anthropic
import json
import os
import io
import re
import zipfile
import uuid
from docx import Document
from docx.oxml.ns import qn
from docx.oxml import OxmlElement
import datetime

app = Flask(__name__)
CORS(app)


def extract_text_from_docx(file_bytes):
    try:
        doc = Document(io.BytesIO(file_bytes))
        text = []
        for para in doc.paragraphs:
            if para.text.strip():
                text.append(para.text)
        return "\n".join(text)
    except Exception:
        # Fallback: extract text from XML directly
        try:
            with zipfile.ZipFile(io.BytesIO(file_bytes)) as z:
                if 'word/document.xml' in z.namelist():
                    doc_xml = z.read('word/document.xml').decode('utf-8', errors='ignore')
                    text = re.sub(r'<[^>]+>', ' ', doc_xml)
                    text = re.sub(r'\s+', ' ', text).strip()
                    return text
        except Exception as e2:
            raise ValueError(f"Impossible de lire le fichier Word: {str(e2)}")

def analyze_contract(contract_text, lang, contract_type, api_key, partie="la partie bénéficiaire"):
    if not api_key:
        raise ValueError("Clé API manquante")
    client = anthropic.Anthropic(api_key=api_key)

    system = f"""Tu es un juriste expert. Analyse ce contrat et propose des modifications pour protéger {partie}.
LANGUE OBLIGATOIRE: Détecte automatiquement la langue du contrat et réponds UNIQUEMENT dans cette même langue, sans aucun mélange. Si le contrat est en anglais, réponds en anglais. Si en français, en français. Si en arabe, en arabe. Etc.
Type de contrat: {contract_type}
Partie à protéger: {partie} — toutes les modifications doivent favoriser les intérêts de {partie}.

Retourne UNIQUEMENT du JSON valide, sans markdown, sans backticks:
{{"modifications":[{{"id":1,"clause_name":"nom court","risk":"high|medium|low","reason":"Une phrase expliquant le risque.","original":"texte exact copié du contrat","proposed":"clause complète et professionnelle bien rédigée"}}]}}

Règles STRICTES:
- Exactement 5 modifications
- original: copie mot pour mot du contrat, max 50 mots
- proposed: clause complète et professionnelle, bien rédigée en {'anglais' if lang == 'en' else 'français'}, max 60 mots
- reason: 1 phrase claire en {'anglais' if lang == 'en' else 'français'}
- clause_name: max 5 mots
- Priorités: responsabilité, résiliation, propriété intellectuelle, pénalités, confidentialité"""

    message = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=4000,
        system=system,
        messages=[{"role": "user", "content": f"Contrat:\n\n{contract_text[:50000]}\n\nRetourne le JSON."}]
    )

    raw = message.content[0].text
    match = re.search(r'\{[\s\S]*\}', raw)
    if not match:
        raise ValueError("Réponse invalide de l'IA")
    return json.loads(match.group(0))

def apply_track_changes(file_bytes, modifications, decisions):
    doc = Document(io.BytesIO(file_bytes))
    author = "ContractSense"
    date = datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%SZ")
    rev_id = 1

    accepted = [m for m in modifications if decisions.get(str(m["id"])) == "accepted"]

    for para in doc.paragraphs:
        para_text = para.text
        for mod in accepted:
            original = mod.get("original", "").strip()
            proposed = mod.get("proposed", "").strip()
            if not original or not proposed:
                continue
            if original in para_text:
                # Clear existing runs
                for run in para.runs:
                    run.text = ""

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


@app.route("/debug", methods=["GET"])
def debug():
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    return jsonify({
        "key_present": bool(key),
        "key_length": len(key),
        "key_prefix": key[:7] if key else "none",
        "all_vars": list(os.environ.keys())
    })

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})

@app.route("/identify-parties", methods=["POST"])
def identify_parties():
    try:
        file = request.files.get("file")
        api_key = os.environ.get("ANTHROPIC_API_KEY") or request.form.get("api_key", "")
        lang = request.form.get("lang", "fr")

        if not file:
            return jsonify({"error": "Fichier manquant"}), 400

        file_bytes = file.read()
        filename = file.filename.lower()

        if filename.endswith(".docx") or filename.endswith(".doc"):
            contract_text = extract_text_from_docx(file_bytes)
        else:
            contract_text = file_bytes.decode("utf-8", errors="ignore")

        client = anthropic.Anthropic(api_key=api_key)

        system = f"""Tu es un juriste expert. Identifie les parties dans ce contrat.
Réponds UNIQUEMENT en {'anglais' if lang == 'en' else 'français'} avec ce JSON exact, sans markdown:
{{"parties":[{{"id":"partie_1","name":"Nom exact de la partie 1","description":"Rôle de cette partie dans le contrat"}},{{"id":"partie_2","name":"Nom exact de la partie 2","description":"Rôle de cette partie dans le contrat"}}]}}
- Utilise les vrais noms des parties tels qu'ils apparaissent dans le contrat
- Maximum 3 parties
- description: max 10 mots"""

        message = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=500,
            system=system,
            messages=[{"role": "user", "content": f"Contrat:\n\n{contract_text[:20000]}\n\nIdentifie les parties."}]
        )

        raw = message.content[0].text
        match = re.search(r'\{[\s\S]*\}', raw)
        if not match:
            raise ValueError("Réponse invalide")
        return jsonify(json.loads(match.group(0)))

    except Exception as e:
        return jsonify({"error": str(e)}), 500


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

        if not contract_text or len(contract_text.strip()) < 50:
            return jsonify({"error": "Le fichier semble vide ou illisible"}), 400

        api_key = os.environ.get("ANTHROPIC_API_KEY") or request.form.get("api_key", "")
        result = analyze_contract(contract_text, lang, contract_type, api_key, partie)
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
        filename = file.filename.lower()

        if filename.endswith(".docx") or filename.endswith(".doc"):
            output = apply_track_changes(file_bytes, modifications, decisions)
        else:
            # For TXT/PDF: create a new DOCX with modifications
            doc = Document()
            doc.add_heading('ContractSense - Modifications acceptées', 0)

            accepted = [m for m in modifications if decisions.get(str(m["id"])) == "accepted"]
            rejected = [m for m in modifications if decisions.get(str(m["id"])) == "rejected"]

            doc.add_paragraph(f"Modifications acceptées: {len(accepted)} | Refusées: {len(rejected)}")
            doc.add_paragraph("")

            for i, m in enumerate(accepted):
                doc.add_heading(f"{i+1}. {m.get('clause_name', '')}", level=2)
                p_del = doc.add_paragraph()
                run_del = p_del.add_run(m.get("original", ""))
                run_del.font.color.rgb = None
                from docx.oxml.ns import qn as qname
                rpr = run_del._r.get_or_add_rPr()
                strike = OxmlElement('w:strike')
                rpr.append(strike)
                color = OxmlElement('w:color')
                color.set(qname('w:val'), 'FF0000')
                rpr.append(color)

                p_ins = doc.add_paragraph()
                run_ins = p_ins.add_run(m.get("proposed", ""))
                rpr2 = run_ins._r.get_or_add_rPr()
                color2 = OxmlElement('w:color')
                color2.set(qname('w:val'), '008000')
                rpr2.append(color2)
                doc.add_paragraph("")

            output = io.BytesIO()
            doc.save(output)
            output.seek(0)

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
