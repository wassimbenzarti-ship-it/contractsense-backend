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
import requests as req_lib
from docx import Document
from docx.oxml.ns import qn
from docx.oxml import OxmlElement

app = Flask(__name__)
CORS(app)

# ── Supabase client ──────────────────────────────────────
SUPA_URL = os.environ.get("SUPABASE_URL", "https://nezxohrkikgjegnhgpyn.supabase.co")
SUPA_KEY = os.environ.get("SUPABASE_KEY", "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Im5lenhvaHJraWtnamVnbmhncHluIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NzM4MTQxNzYsImV4cCI6MjA4OTM5MDE3Nn0.zhBCacGGmIX-rVE9E9MUcbY2RpMomfq33lyq6DNU2kI")

def supa_headers():
    return {
        "apikey": SUPA_KEY,
        "Authorization": "Bearer " + SUPA_KEY,
        "Content-Type": "application/json",
        "Prefer": "return=minimal"
    }

def supa_get(table, params=None):
    url = SUPA_URL + "/rest/v1/" + table
    r = req_lib.get(url, headers=supa_headers(), params=params, timeout=30)
    r.raise_for_status()
    return r.json()

def supa_insert(table, data):
    url = SUPA_URL + "/rest/v1/" + table
    r = req_lib.post(url, headers=supa_headers(), json=data, timeout=30)
    r.raise_for_status()
    return r

def supa_delete(table, filters):
    url = SUPA_URL + "/rest/v1/" + table
    r = req_lib.delete(url, headers=supa_headers(), params=filters, timeout=30)
    r.raise_for_status()
    return r

# ── RAG: Supabase REST storage ────────────────────────────
def load_rag():
    try:
        docs = supa_get("rag_documents", {"select": "*", "limit": "1000"})
        return {"documents": docs or []}
    except Exception as e:
        print("load_rag error: " + str(e))
        return {"documents": []}

def save_rag_doc(doc):
    try:
        doc_copy = dict(doc)
        if "embedding" in doc_copy and isinstance(doc_copy["embedding"], list):
            doc_copy["embedding"] = json.dumps(doc_copy["embedding"])
        supa_insert("rag_documents", doc_copy)
        print("save_rag_doc OK: " + str(doc_copy.get("title","?"))[:50])
    except Exception as e:
        print("save_rag_doc ERROR: " + str(e))
        raise

def delete_rag_by_source(source):
    try:
        import re as _re
        docs = supa_get("rag_documents", {"select": "id,title", "limit": "1000"})
        count = 0
        for d in (docs or []):
            base = _re.sub(r" \(partie \d+\)$", "", d.get("title", ""))
            if base == source:
                supa_delete("rag_documents", {"id": "eq." + d["id"]})
                count += 1
        return count
    except Exception as e:
        print("delete_rag error: " + str(e))
        return 0

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
        emb = doc.get("embedding")
        if emb is None:
            continue
        # Parse embedding from JSON string if needed
        if isinstance(emb, str):
            try:
                emb = json.loads(emb)
            except:
                continue
        score = cosine_similarity(query_vec, emb)
        # Boost for matching party
        if partie and doc.get("party_label", "").lower() and partie.lower() in doc.get("party_label", "").lower():
            score *= 1.3
        # Boost validated clauses
        if "validated_clause" in doc.get("source", ""):
            score *= 1.2
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

def build_numbered_paragraphs(file_bytes, filename):
    """Build a numbered paragraph index from DOCX for precise matching"""
    try:
        if filename.endswith('.docx') or filename.endswith('.doc'):
            doc = Document(io.BytesIO(file_bytes))
            paragraphs = []
            for i, para in enumerate(doc.paragraphs):
                text = para.text.strip()
                if text:
                    paragraphs.append({"idx": i, "text": text})
            return paragraphs
    except:
        pass
    return []

def analyze_contract(contract_text, lang, contract_type, api_key, partie="la partie bénéficiaire", file_bytes=None, filename=""):
    api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise ValueError("Clé API manquante")
    client = anthropic.Anthropic(api_key=api_key)

    # Build numbered paragraphs for precise matching
    paragraphs = build_numbered_paragraphs(file_bytes, filename) if file_bytes else []
    
    # Build numbered contract text for AI
    if paragraphs:
        numbered_text = "\n".join([f"[P{p['idx']}] {p['text']}" for p in paragraphs[:150]])
    else:
        numbered_text = contract_text[:50000]

    # Search RAG for relevant context
    rag_context = ""
    try:
        voyage_key = os.environ.get("VOYAGE_API_KEY", "")
        relevant_docs = search_rag(contract_text[:2000], api_key, voyage_key, top_k=6, partie=partie)
        if relevant_docs:
            validated_clauses = [d for d in relevant_docs if "validated_clause" in d.get("source", "")]
            reference_docs = [d for d in relevant_docs if "validated_clause" not in d.get("source", "")]

            if validated_clauses:
                rag_context += "\n\nCLAUSES VALIDÉES PAR DES JURISTES (utilise ces reformulations comme modèles):\n"
                for doc in validated_clauses:
                    content_raw = doc.get("content", "")
                    rag_context += "\n---\n" + content_raw[:600] + "\n"
                rag_context += "\n→ Ces clauses ont été validées par des juristes. Inspire-toi directement de leur formulation.\n"

            if reference_docs:
                rag_context += "\n\nDOCUMENTS JURIDIQUES DE RÉFÉRENCE:\n"
                for doc in reference_docs:
                    rag_context += "\n=== " + doc.get("title", "Document") + " ===\n" + doc.get("content", "")[:500] + "\n"
    except Exception as e:
        print("RAG search error: " + str(e))

    system = (
        "Tu es un juriste expert spécialisé en analyse contractuelle. "
        "Tu analyses ce contrat et proposes des modifications pour protéger " + partie + ".\n\n"
        "LANGUE: Réponds UNIQUEMENT dans la langue du contrat.\n"
        "TYPE: " + contract_type + "\n"
        "PARTIE À PROTÉGER: " + partie + "\n"
        + rag_context +
        "\n\nMÉTHODE D'ANALYSE:\n"
        "1. Identifie chaque clause déséquilibrée ou risquée pour " + partie + "\n"
        "2. Si une CLAUSE VALIDÉE similaire existe ci-dessus, utilise sa formulation comme base\n"
        "3. Si un DOCUMENT DE RÉFÉRENCE est pertinent, cite-le explicitement\n"
        "4. Sinon, propose une reformulation professionnelle standard\n\n"
        "IMPORTANT: Le contrat est numéroté [P0], [P1], etc. "
        "Utilise le numéro exact du paragraphe.\n\n"
        "Retourne UNIQUEMENT du JSON valide, sans markdown:\n"
        '{"modifications":[{"id":1,"para_idx":32,"clause_name":"nom court",'
        '"risk":"high|medium|low",'
        '"reason":"Explication + source si clause validée utilisée",'
        '"original":"texte EXACT du paragraphe copié mot pour mot",'
        '"proposed":"clause reformulée, inspirée des clauses validées si disponibles",'
        '"rag_source":"titre du document RAG utilisé ou null"}]}\n\n'
        "Règles STRICTES:\n"
        "- Identifie TOUTES les clauses problématiques (minimum 5, pas de maximum)\n"
        "- para_idx: numéro entier du paragraphe [Pxx]\n"
        "- original: copie EXACTE du texte, sans modification\n"
        "- proposed: max 80 mots, professionnel\n"
        "- reason: cite explicitement la source RAG si utilisée (ex: 'Basé sur clause validée: Paiement favorable prestataire')\n"
        "- rag_source: nom du document RAG utilisé, ou null si aucun"
    )

    message = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=4000,
        system=system,
        messages=[{"role": "user", "content": "Contrat:\n\n" + numbered_text + "\n\nRetourne le JSON."}]
    )
    raw = message.content[0].text
    match = re.search(r'\{[\s\S]*\}', raw)
    if not match:
        raise ValueError("Réponse invalide de l'IA")
    result = json.loads(match.group(0))

    # Add confidence score based on RAG usage
    mods = result.get("modifications", [])
    rag_backed = sum(1 for m in mods if m.get("rag_source"))
    result["_rag_coverage"] = str(rag_backed) + "/" + str(len(mods)) + " modifications basées sur le RAG"
    result["_paragraphs"] = paragraphs
    return result

def fuzzy_match(original, para_text, threshold=0.60):
    """Check if original text roughly matches para_text"""
    original_lower = original.lower().strip()
    para_lower = para_text.lower().strip()
    # Exact match
    if original_lower in para_lower:
        return True
    # Extract meaningful words (ignore short words)
    orig_words = [w for w in re.findall(r"[a-zA-ZÀ-ÿ]{3,}", original_lower)]
    para_words_set = set(re.findall(r"[a-zA-ZÀ-ÿ]{3,}", para_lower))
    orig_words_set = set(orig_words)
    if len(orig_words_set) < 4:
        return False
    overlap = len(orig_words_set & para_words_set) / len(orig_words_set)
    return overlap >= threshold

def apply_track_changes(file_bytes, modifications, decisions):
    doc = Document(io.BytesIO(file_bytes))
    author = "ContractSense"
    date = datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%SZ")
    rev_id = 1

    accepted = [m for m in modifications if decisions.get(str(m["id"])) == "accepted"]
    applied = set()
    paragraphs = list(doc.paragraphs)

    for mod in accepted:
        mod_id = mod.get("id")
        proposed = mod.get("proposed", "").strip()
        if not proposed:
            continue

        para = None

        # Method 1: Use para_idx if available (precise)
        para_idx = mod.get("para_idx")
        if para_idx is not None and para_idx < len(paragraphs):
            candidate = paragraphs[para_idx]
            if candidate.text.strip():
                para = candidate

        # Method 2: Fuzzy match fallback
        if para is None:
            original = mod.get("original", "").strip()
            for p in paragraphs:
                if p.text.strip() and fuzzy_match(original, p.text.strip()):
                    para = p
                    break

        if para is None:
            print(f"Could not find paragraph for mod {mod_id}: {mod.get('clause_name')}")
            continue

        para_text = para.text.strip()

        # Clear all runs
        for run in para.runs:
            run.text = ""
        p = para._p

        # Del element
        del_elem = OxmlElement('w:del')
        del_elem.set(qn('w:id'), str(rev_id))
        del_elem.set(qn('w:author'), author)
        del_elem.set(qn('w:date'), date)
        del_run = OxmlElement('w:r')
        del_rpr = OxmlElement('w:rPr')
        del_run.append(del_rpr)
        del_text = OxmlElement('w:delText')
        del_text.set('{http://www.w3.org/XML/1998/namespace}space', 'preserve')
        del_text.text = para_text
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
        ins_text_el = OxmlElement('w:t')
        ins_text_el.set('{http://www.w3.org/XML/1998/namespace}space', 'preserve')
        ins_text_el.text = proposed
        ins_run.append(ins_text_el)
        ins_elem.append(ins_run)
        p.append(ins_elem)
        rev_id += 1

        applied.add(mod_id)

    print(f"Track changes: {len(applied)}/{len(accepted)} applied")
    output = io.BytesIO()
    doc.save(output)
    output.seek(0)
    return output

# ── Routes ────────────────────────────────────────────────
@app.route("/debug-env", methods=["GET"])
def debug_env():
    try:
        test = supa_get("rag_documents", {"select": "id", "limit": "1"})
        supa_status = "OK - " + str(len(test)) + " docs"
    except Exception as e:
        supa_status = "ERROR: " + str(e)
    return jsonify({
        "supabase_url": SUPA_URL[:40],
        "supabase_key_set": bool(SUPA_KEY),
        "supabase_test": supa_status
    })

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
        contract_text, file_bytes, filename = read_file(file)
        if not contract_text or len(contract_text.strip()) < 50:
            return jsonify({"error": "Fichier vide ou illisible"}), 400
        result = analyze_contract(contract_text, lang, contract_type, api_key, partie, file_bytes, filename)
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

# ── Queue: Supabase REST storage ─────────────────────────
def load_queue():
    try:
        items = supa_get("queue_pending", {"select": "*", "order": "submitted_at", "limit": "200"})
        return {"pending": items or []}
    except Exception as e:
        print("load_queue error: " + str(e))
        return {"pending": []}

def save_queue_item(item):
    try:
        item_copy = dict(item)
        for field in ["key_clauses", "accepted_modifications"]:
            if field in item_copy and not isinstance(item_copy[field], str):
                item_copy[field] = json.dumps(item_copy.get(field, []))
        supa_insert("queue_pending", item_copy)
    except Exception as e:
        print("save_queue_item error: " + str(e))

def delete_queue_item(item_id):
    try:
        supa_delete("queue_pending", {"id": "eq." + item_id})
    except Exception as e:
        print("delete_queue_item error: " + str(e))

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

        # Store rejection signals for learning (silent, no RAG indexing — just metadata)
        if rejected:
            print("Rejected clauses (" + str(len(rejected)) + "): " + ", ".join([m.get("clause_name","?") for m in rejected]))

        # AI scoring of contract quality for RAG
        client = anthropic.Anthropic(api_key=api_key)
        scoring_prompt = """Evalue ce contrat pour une base de connaissances juridiques.
Reponds UNIQUEMENT en JSON valide, sans markdown:
{
  "score": 0-100,
  "category": "nda|saas|purchase|employment|partnership|service|collaboration|generic",
  "party_label": "favorable """ + (partie if partie else "neutre") + """",
  "quality_reason": "1 phrase expliquant le score",
  "key_clauses": ["clause1", "clause2", "clause3"]
}
Regles:
- category: deduis du CONTENU du contrat, pas du type selectionne par l utilisateur
  * service = contrat de prestation de services, collaboration, mission
  * nda = confidentialite
  * employment = travail, salarie
  * partnership = association, joint-venture
  * purchase = achat, vente
  * saas = logiciel, abonnement
- party_label: utilise un label GENERIQUE selon le role de la partie
  * service/prestation/collaboration → "favorable client" ou "favorable prestataire"
  * travail → "favorable employeur" ou "favorable employe"
  * nda → "favorable divulgateur" ou "favorable destinataire"
  * achat/vente → "favorable acheteur" ou "favorable vendeur"
  * partenariat → "favorable partenaire A" ou "favorable partenaire B"
  La partie favorisee est: """ + (partie if partie else "neutre") + """
- score: 0-100 selon la qualite et completude du contrat
Score eleve = contrat complet avec clauses interessantes a reutiliser."""

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
        import uuid as _uuid
        save_queue_item({
            "id": str(_uuid.uuid4()),
            "contract_text": contract_text[:50000],
            "filename": filename,
            "partie": partie,
            "party_label": scoring.get("party_label", "favorable " + partie),
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
        pending = queue.get("pending", [])
        contract = next((c for c in pending if c["id"] == contract_id), None)
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
        accepted_mods = contract.get("accepted_modifications", [])
        if isinstance(accepted_mods, str):
            accepted_mods = json.loads(accepted_mods)
        for mod in accepted_mods:
            mod_text = "CLAUSE VALIDEE [" + party_label + "]: " + mod.get('clause_name','') + "\n" + mod.get('proposed','')
            embedding = get_embedding(mod_text, voyage_key)
            save_rag_doc({
                "id": str(uuid.uuid4()),
                "title": "[CLAUSE] " + mod.get('clause_name','') + " — " + party_label,
                "category": "validated_clause",
                "party_label": party_label,
                "partie": contract.get("partie", ""),
                "contract_type": category,
                "content": mod_text,
                "embedding": json.dumps(embedding),
                "source": "admin_validated_clause",
                "validated_at": datetime.datetime.now().isoformat()
            })

        delete_queue_item(contract_id)

        return jsonify({"success": True, "chunks_indexed": len(chunks)})

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/queue/reject", methods=["POST"])
def queue_reject():
    """Admin rejects contract — removes from queue"""
    try:
        body = request.get_json()
        contract_id = body.get("id")
        delete_queue_item(contract_id)
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

        import uuid
        voyage_key = os.environ.get("VOYAGE_API_KEY") or request.form.get("voyage_key", "")
        for i, chunk in enumerate(chunks):
            embedding = get_embedding(chunk, voyage_key)
            chunk_title = (title + " (partie " + str(i+1) + ")") if len(chunks) > 1 else title
            save_rag_doc({
                "id": str(uuid.uuid4()),
                "title": chunk_title,
                "category": category,
                "content": chunk,
                "embedding": json.dumps(embedding),
                "source": "manual_upload",
                "validated_at": datetime.datetime.now().isoformat()
            })

        total = load_rag()
        return jsonify({"success": True, "chunks": len(chunks), "source": title, "total_docs": len(total["documents"])})

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
                sources[base] = {
                    "source": base,
                    "type": d.get("category", "law"),
                    "party_label": d.get("party_label", ""),
                    "chunks": 0
                }
            sources[base]["chunks"] += 1
        docs = list(sources.values())
        return jsonify({"documents": docs, "total": len(data["documents"])})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/rag/delete/<doc_id>", methods=["DELETE"])
def rag_delete_by_id(doc_id):
    try:
        sb = get_supabase()
        sb.table("rag_documents").delete().eq("id", doc_id).execute()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/rag/delete", methods=["POST"])
def rag_delete():
    try:
        body = request.get_json()
        source = body.get("source", "")
        count = delete_rag_by_source(source)
        return jsonify({"success": True, "deleted": count})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
