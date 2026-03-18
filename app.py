from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
import anthropic
import json
import os
import io
import re
import zipfile
import datetime
import numpy as np
import voyageai
from docx import Document
from docx.oxml.ns import qn
from docx.oxml import OxmlElement

app = Flask(__name__)
CORS(app)

# ── RAG: Simple JSON-based vector store ───────────────────
RAG_PATH = os.environ.get("RAG_PATH", "/data/rag.json")
os.makedirs(os.path.dirname(RAG_PATH), exist_ok=True)

def load_rag():
    try:
        with open(RAG_PATH, 'r', encoding='utf-8') as f:
            return json.load(f)
    except:
        return {"documents": []}

def save_rag(data):
    with open(RAG_PATH, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def cosine_similarity(a, b):
    a, b = np.array(a), np.array(b)
    return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-10))

def get_embedding(text, voyage_key=None):
    # Try Voyage AI for semantic embeddings
    if voyage_key:
        try:
            vo = voyageai.Client(api_key=voyage_key)
            result = vo.embed([text[:2000]], model="voyage-law-2", input_type="document")
            return result.embeddings[0]
        except:
            pass
    # Fallback to TF-IDF hashing
    import hashlib
    words = re.findall(r'\w+', text.lower())
    vec = [0.0] * 512
    for word in words:
        h = int(hashlib.md5(word.encode()).hexdigest(), 16) % 512
        vec[h] += 1.0
    for i in range(len(words)-1):
        bigram = words[i] + '_' + words[i+1]
        h = int(hashlib.sha256(bigram.encode()).hexdigest(), 16) % 512
        vec[h] += 0.5
    norm = sum(v*v for v in vec) ** 0.5
    if norm > 0:
        vec = [v/norm for v in vec]
    return vec

def search_rag(query, api_key, voyage_key=None, top_k=5, partie=None):
    data = load_rag()
    if not data["documents"]:
        return []
    query_vec = get_embedding(query, voyage_key)
    scored = []
    for doc in data["documents"]:
        if "embedding" in doc:
            score = cosine_similarity(query_vec, doc["embedding"])
            # Boost score for clauses matching the requested partie
            if partie and doc.get("partie", "").lower() in partie.lower():
                score *= 1.3
            # Boost validated clauses
            if doc.get("source") == "user_validated":
                score *= 1.1
            scored.append((score, doc))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [doc for _, doc in scored[:top_k]]

# ── Text extraction ───────────────────────────────────────
def extract_text_from_docx(file_bytes):
    try:
        doc = Document(io.BytesIO(file_bytes))
        text = []
        for para in doc.paragraphs:
            if para.text.strip():
                text.append(para.text)
        return "\n".join(text)
    except Exception:
        try:
            with zipfile.ZipFile(io.BytesIO(file_bytes)) as z:
                if 'word/document.xml' in z.namelist():
                    doc_xml = z.read('word/document.xml').decode('utf-8', errors='ignore')
                    text = re.sub(r'<[^>]+>', ' ', doc_xml)
                    return re.sub(r'\s+', ' ', text).strip()
        except Exception as e2:
            raise ValueError(f"Impossible de lire le fichier Word: {str(e2)}")

def read_file(file):
    file_bytes = file.read()
    filename = file.filename.lower()
    if filename.endswith(".docx") or filename.endswith(".doc"):
        return extract_text_from_docx(file_bytes), file_bytes, filename
    else:
        return file_bytes.decode("utf-8", errors="ignore"), file_bytes, filename

# ── AI functions ──────────────────────────────────────────
def identify_parties(contract_text, lang, api_key):
    client = anthropic.Anthropic(api_key=api_key)
    system = f"""Tu es un juriste expert. Identifie les parties dans ce contrat.
Réponds UNIQUEMENT en {'anglais' if lang == 'en' else 'français'} avec ce JSON exact, sans markdown:
{{"parties":[{{"id":"partie_1","name":"Nom exact de la partie 1","description":"Role de cette partie"}},{{"id":"partie_2","name":"Nom exact de la partie 2","description":"Role de cette partie"}}]}}
- Utilise les vrais noms tels qu'ils apparaissent dans le contrat
- Maximum 3 parties, description max 10 mots"""

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
    return json.loads(match.group(0))

def analyze_contract(contract_text, lang, contract_type, api_key, partie="la partie bénéficiaire"):
    api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise ValueError("Clé API manquante")
    client = anthropic.Anthropic(api_key=api_key)

    # Search RAG for relevant context
    rag_context = ""
    try:
        voyage_key = os.environ.get("VOYAGE_API_KEY", "")
        relevant_docs = search_rag(contract_text[:2000], api_key, voyage_key, top_k=5, partie=partie)
        if relevant_docs:
            rag_context = "\n\nDOCUMENTS DE RÉFÉRENCE JURIDIQUE (utilise ces documents pour renforcer tes modifications):\n"
            for doc in relevant_docs:
                title = doc.get("title", "Document")
                content_preview = doc.get("content", "")[:800]
                rag_context += f"\n=== {title} ===\n{content_preview}\n"
            rag_context += "\n(FIN DES DOCUMENTS DE RÉFÉRENCE — cite-les explicitement dans tes modifications)\n"
    except:
        pass

    system = f"""Tu es un juriste expert. Analyse ce contrat et propose des modifications pour protéger {partie}.
LANGUE OBLIGATOIRE: Détecte automatiquement la langue du contrat et réponds UNIQUEMENT dans cette même langue.
Type de contrat: {contract_type}
Partie à protéger: {partie} — toutes les modifications doivent favoriser les intérêts de {partie}.
{rag_context}

Retourne UNIQUEMENT du JSON valide, sans markdown, sans backticks:
{{"modifications":[{{"id":1,"clause_name":"nom court","risk":"high|medium|low","reason":"Explication du risque avec référence au document de référence si applicable.","original":"texte exact copié du contrat","proposed":"clause complète et professionnelle bien rédigée, inspirée des documents de référence si disponibles"}}]}}

Règles STRICTES:
- Identifie TOUTES les clauses problématiques, sans limite de nombre (minimum 5, pas de maximum)
- original: copie mot pour mot du contrat, max 50 mots
- proposed: clause complète et professionnelle, max 80 mots
- reason: 1-2 phrases claires, cite le document de référence pertinent si disponible (ex: "Selon le Code des Obligations, art. X...")
- clause_name: max 5 mots
- OBLIGATOIRE: si des documents de référence sont fournis, utilise-les activement dans tes propositions et mentionne-les explicitement"""

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

def fuzzy_match(original, para_text, threshold=0.45):
    """Check if original text roughly matches para_text"""
    original = original.lower().strip()
    para_text = para_text.lower().strip()
    # Exact match
    if original in para_text:
        return True
    # Partial match: check if >45% of words from original appear in para
    orig_words = set(re.findall(r'[a-zA-ZÀ-ÿ]+', original))
    para_words = set(re.findall(r'[a-zA-ZÀ-ÿ]+', para_text))
    if not orig_words or len(orig_words) < 3:
        return False
    overlap = len(orig_words & para_words) / len(orig_words)
    return overlap >= threshold

def apply_track_changes(file_bytes, modifications, decisions):
    doc = Document(io.BytesIO(file_bytes))
    author = "ContractSense"
    date = datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%SZ")
    rev_id = 1

    accepted = [m for m in modifications if decisions.get(str(m["id"])) == "accepted"]
    applied = set()

    for para in doc.paragraphs:
        para_text = para.text.strip()
        if not para_text:
            continue
        for mod in accepted:
            mod_id = mod.get("id")
            if mod_id in applied:
                continue
            original = mod.get("original", "").strip()
            proposed = mod.get("proposed", "").strip()
            if not original or not proposed:
                continue
            # Use fuzzy matching to find the right paragraph
            if fuzzy_match(original, para_text):
                # Clear all runs
                for run in para.runs:
                    run.text = ""
                p = para._p

                # Del element - use actual para text for accuracy
                del_elem = OxmlElement('w:del')
                del_elem.set(qn('w:id'), str(rev_id))
                del_elem.set(qn('w:author'), author)
                del_elem.set(qn('w:date'), date)
                del_run = OxmlElement('w:r')
                del_rpr = OxmlElement('w:rPr')
                del_run.append(del_rpr)
                del_text = OxmlElement('w:delText')
                del_text.set('{http://www.w3.org/XML/1998/namespace}space', 'preserve')
                del_text.text = para_text  # use full para text
                del_run.append(del_text)
                del_elem.append(del_run)
                p.append(del_elem)
                rev_id += 1

                # Ins element
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

                applied.add(mod_id)
                break

    # Log how many modifications were applied
    print(f"Track changes applied: {len(applied)}/{len(accepted)}")
    output = io.BytesIO()
    doc.save(output)
    output.seek(0)
    return output

# ── Routes ────────────────────────────────────────────────
@app.route("/health", methods=["GET"])
def health():
    rag = load_rag()
    return jsonify({"status": "ok", "rag_docs": len(rag["documents"])})

@app.route("/identify-parties", methods=["POST"])
def identify_parties_route():
    try:
        file = request.files.get("file")
        api_key = os.environ.get("ANTHROPIC_API_KEY") or request.form.get("api_key", "")
        lang = request.form.get("lang", "fr")
        if not file:
            return jsonify({"error": "Fichier manquant"}), 400
        contract_text, _, _ = read_file(file)
        if not contract_text or len(contract_text.strip()) < 50:
            return jsonify({"error": "Fichier vide ou illisible"}), 400
        result = identify_parties(contract_text, lang, api_key)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/analyze", methods=["POST"])
def analyze():
    try:
        file = request.files.get("file")
        lang = request.form.get("lang", "fr")
        contract_type = request.form.get("type", "generic")
        api_key = os.environ.get("ANTHROPIC_API_KEY") or request.form.get("api_key", "")
        partie = request.form.get("partie", "la partie bénéficiaire") or "la partie bénéficiaire"
        if not file:
            return jsonify({"error": "Fichier manquant"}), 400
        contract_text, _, _ = read_file(file)
        if not contract_text or len(contract_text.strip()) < 50:
            return jsonify({"error": "Fichier vide ou illisible"}), 400
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
            doc = Document()
            doc.add_heading('ContractSense - Modifications acceptées', 0)
            accepted = [m for m in modifications if decisions.get(str(m["id"])) == "accepted"]
            for i, m in enumerate(accepted):
                doc.add_heading(f"{i+1}. {m.get('clause_name', '')}", level=2)
                p_del = doc.add_paragraph()
                run_del = p_del.add_run(m.get("original", ""))
                rpr = run_del._r.get_or_add_rPr()
                strike = OxmlElement('w:strike')
                rpr.append(strike)
                color = OxmlElement('w:color')
                color.set(qn('w:val'), 'FF0000')
                rpr.append(color)
                p_ins = doc.add_paragraph()
                run_ins = p_ins.add_run(m.get("proposed", ""))
                rpr2 = run_ins._r.get_or_add_rPr()
                color2 = OxmlElement('w:color')
                color2.set(qn('w:val'), '008000')
                rpr2.append(color2)
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

# ── Queue for pending clauses ─────────────────────────────
QUEUE_PATH = os.environ.get("QUEUE_PATH", "/data/queue.json")

def load_queue():
    try:
        with open(QUEUE_PATH, 'r', encoding='utf-8') as f:
            return json.load(f)
    except:
        return {"pending": []}

def save_queue(data):
    os.makedirs(os.path.dirname(QUEUE_PATH), exist_ok=True)
    with open(QUEUE_PATH, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

@app.route("/rag/contribute", methods=["POST"])
def rag_contribute():
    """Auto-queue full contract with AI scoring for admin validation"""
    try:
        file = request.files.get("file")
        modifications = json.loads(request.form.get("modifications", "[]"))
        decisions = json.loads(request.form.get("decisions", "{}"))
        partie = request.form.get("partie", "")
        contract_type = request.form.get("contract_type", "generic")
        api_key = os.environ.get("ANTHROPIC_API_KEY") or request.form.get("api_key", "")

        if not file:
            return jsonify({"error": "Fichier manquant"}), 400

        contract_text, _, filename = read_file(file)
        accepted = [m for m in modifications if decisions.get(str(m["id"])) == "accepted"]
        rejected = [m for m in modifications if decisions.get(str(m["id"])) == "rejected"]

        # AI scoring of contract quality for RAG
        client = anthropic.Anthropic(api_key=api_key)
        scoring_prompt = f"""Évalue ce contrat pour son intérêt dans une base de connaissances juridiques.
Réponds UNIQUEMENT en JSON valide:
{{
  "score": 0-100,
  "category": "nda|saas|purchase|employment|partnership|service|generic",
  "party_label": "favorable {partie if partie else 'neutre'}",
  "quality_reason": "1 phrase expliquant le score",
  "key_clauses": ["clause1", "clause2", "clause3"]
}}
Score élevé = contrat complet, bien structuré, avec des clauses intéressantes à réutiliser.
Score faible = contrat trop basique ou incomplet."""

        message = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            system=scoring_prompt,
            messages=[{"role": "user", "content": "Contrat:\n\n" + contract_text[:5000]}]
        )
        raw = message.content[0].text
        match = re.search(r'\{[\s\S]*\}', raw)
        scoring = json.loads(match.group(0)) if match else {"score": 50, "category": contract_type, "party_label": f"favorable {partie}", "quality_reason": "Scoring indisponible", "key_clauses": []}

        import uuid
        queue = load_queue()
        queue["pending"].append({
            "id": str(uuid.uuid4()),
            "contract_text": contract_text[:50000],
            "filename": filename,
            "partie": partie,
            "party_label": scoring.get("party_label", f"favorable {partie}"),
            "contract_type": contract_type,
            "score": scoring.get("score", 50),
            "category": scoring.get("category", contract_type),
            "quality_reason": scoring.get("quality_reason", ""),
            "key_clauses": scoring.get("key_clauses", []),
            "accepted_count": len(accepted),
            "rejected_count": len(rejected),
            "accepted_modifications": accepted,
            "submitted_at": datetime.datetime.now().isoformat()
        })
        save_queue(queue)
        return jsonify({"success": True, "score": scoring.get("score", 50)})

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/queue/list", methods=["GET"])
def queue_list():
    """List pending clauses for admin review"""
    try:
        queue = load_queue()
        return jsonify({"pending": queue["pending"], "total": len(queue["pending"])})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/queue/validate", methods=["POST"])
def queue_validate():
    """Admin validates contract — indexes full text into RAG"""
    try:
        body = request.get_json()
        contract_id = body.get("id")
        admin_category = body.get("category", "")
        admin_party_label = body.get("party_label", "")
        voyage_key = os.environ.get("VOYAGE_API_KEY", "")

        queue = load_queue()
        contract = next((c for c in queue["pending"] if c["id"] == contract_id), None)
        if not contract:
            return jsonify({"error": "Contrat introuvable"}), 404

        contract_text = contract.get("contract_text", "")
        category = admin_category or contract.get("category", "generic")
        party_label = admin_party_label or contract.get("party_label", "")
        title_base = f"[{category.upper()}] {party_label}"

        # Split contract into chunks and index
        import uuid
        words = contract_text.split()
        chunk_size = 400
        chunks = []
        for i in range(0, len(words), chunk_size):
            chunks.append(" ".join(words[i:i+chunk_size]))

        data = load_rag()
        for i, chunk in enumerate(chunks):
            embedding = get_embedding(chunk, voyage_key)
            title = f"{title_base} (partie {i+1})" if len(chunks) > 1 else title_base
            data["documents"].append({
                "id": str(uuid.uuid4()),
                "title": title,
                "category": category,
                "party_label": party_label,
                "partie": contract.get("partie", ""),
                "contract_type": category,
                "content": chunk,
                "embedding": embedding,
                "source": "admin_validated",
                "key_clauses": contract.get("key_clauses", []),
                "score": contract.get("score", 50),
                "validated_at": datetime.datetime.now().isoformat()
            })

        # Also index accepted modifications as separate entries
        for mod in contract.get("accepted_modifications", []):
            mod_text = "CLAUSE VALIDEE [" + party_label + "]: " + mod.get('clause_name','') + "\n" + mod.get('proposed','')
            embedding = get_embedding(mod_text, voyage_key)
            data["documents"].append({
                "id": str(uuid.uuid4()),
                "title": f"[CLAUSE] {mod.get('clause_name','')} — {party_label}",
                "category": "validated_clause",
                "party_label": party_label,
                "partie": contract.get("partie", ""),
                "contract_type": category,
                "content": mod_text,
                "embedding": embedding,
                "source": "admin_validated_clause",
                "validated_at": datetime.datetime.now().isoformat()
            })

        save_rag(data)
        queue["pending"] = [c for c in queue["pending"] if c["id"] != contract_id]
        save_queue(queue)

        return jsonify({"success": True, "chunks_indexed": len(chunks)})

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/queue/reject", methods=["POST"])
def queue_reject():
    """Admin rejects a clause — removes from queue"""
    try:
        body = request.get_json()
        clause_id = body.get("id")
        queue = load_queue()
        queue["pending"] = [c for c in queue["pending"] if c["id"] != clause_id]
        save_queue(queue)
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/rag/upload", methods=["POST"])
def rag_upload():
    try:
        file = request.files.get("file")
        title = request.form.get("source_name") or request.form.get("title", "Document")
        category = request.form.get("doc_type") or request.form.get("category", "general")
        api_key = os.environ.get("ANTHROPIC_API_KEY") or request.form.get("api_key", "")
        if not file:
            return jsonify({"error": "Fichier manquant"}), 400

        file_bytes = file.read()
        filename = file.filename.lower()
        if filename.endswith(".docx") or filename.endswith(".doc"):
            content = extract_text_from_docx(file_bytes)
        else:
            content = file_bytes.decode("utf-8", errors="ignore")

        if not content or len(content.strip()) < 50:
            return jsonify({"error": "Document vide ou illisible"}), 400

        # Split into chunks of ~500 words
        words = content.split()
        chunk_size = 500
        chunks = []
        for i in range(0, len(words), chunk_size):
            chunk = " ".join(words[i:i+chunk_size])
            chunks.append(chunk)

        data = load_rag()
        import uuid
        for i, chunk in enumerate(chunks):
            voyage_key = os.environ.get("VOYAGE_API_KEY") or request.form.get("voyage_key", "")
            embedding = get_embedding(chunk, voyage_key)
            data["documents"].append({
                "id": str(uuid.uuid4()),
                "title": f"{title} (partie {i+1})" if len(chunks) > 1 else title,
                "category": category,
                "content": chunk,
                "embedding": embedding
            })

        save_rag(data)
        return jsonify({"success": True, "chunks": len(chunks), "source": title, "total_docs": len(data["documents"])})

    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/rag/list", methods=["GET"])
def rag_list():
    try:
        data = load_rag()
        import re as _re
        sources = {}
        for d in data["documents"]:
            base = _re.sub(r" \(partie \d+\)$", "", d.get("title", "Document"))
            if base not in sources:
                sources[base] = {"source": base, "type": d.get("category", "law"), "chunks": 0}
            sources[base]["chunks"] += 1
        docs = list(sources.values())
        return jsonify({"documents": docs, "total": len(data["documents"])})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/rag/delete/<doc_id>", methods=["DELETE"])
def rag_delete_by_id(doc_id):
    try:
        data = load_rag()
        data["documents"] = [d for d in data["documents"] if d["id"] != doc_id]
        save_rag(data)
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/rag/delete", methods=["POST"])
def rag_delete():
    try:
        import re as _re
        body = request.get_json()
        source = body.get("source", "")
        data = load_rag()
        data["documents"] = [d for d in data["documents"] if _re.sub(r" \(partie \d+\)$", "", d.get("title", "")) != source]
        save_rag(data)
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
