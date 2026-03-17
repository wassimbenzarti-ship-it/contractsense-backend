from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
import anthropic
import json
import os
import io
import re
import zipfile
import datetime
from docx import Document
from docx.oxml.ns import qn
from docx.oxml import OxmlElement

# ChromaDB for RAG
import chromadb
from chromadb.utils import embedding_functions

app = Flask(__name__)
CORS(app)

# ── ChromaDB setup ────────────────────────────────────────
CHROMA_PATH = os.environ.get("CHROMA_PATH", "/data/chroma")
os.makedirs(CHROMA_PATH, exist_ok=True)

chroma_client = chromadb.PersistentClient(path=CHROMA_PATH)

def get_collection():
    return chroma_client.get_or_create_collection(
        name="legal_docs",
        metadata={"hnsw:space": "cosine"}
    )

# ── Helpers ───────────────────────────────────────────────
def get_api_key(req):
    return os.environ.get("ANTHROPIC_API_KEY") or req.form.get("api_key", "") or req.json.get("api_key", "") if req.is_json else os.environ.get("ANTHROPIC_API_KEY") or req.form.get("api_key", "")

def extract_text_from_docx(file_bytes):
    try:
        doc = Document(io.BytesIO(file_bytes))
        return "\n".join(p.text for p in doc.paragraphs if p.text.strip())
    except Exception:
        try:
            with zipfile.ZipFile(io.BytesIO(file_bytes)) as z:
                if 'word/document.xml' in z.namelist():
                    xml = z.read('word/document.xml').decode('utf-8', errors='ignore')
                    return re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' ', xml)).strip()
        except Exception as e:
            raise ValueError(f"Impossible de lire le fichier Word: {str(e)}")

def chunk_text(text, chunk_size=800, overlap=100):
    """Split text into overlapping chunks"""
    words = text.split()
    chunks = []
    i = 0
    while i < len(words):
        chunk = ' '.join(words[i:i+chunk_size])
        chunks.append(chunk)
        i += chunk_size - overlap
    return chunks

def embed_texts(texts, api_key):
    """Generate embeddings using Anthropic voyage embeddings via direct API"""
    # Use sentence_transformers as fallback (free, no API key needed)
    try:
        from sentence_transformers import SentenceTransformer
        model = SentenceTransformer('paraphrase-multilingual-MiniLM-L12-v2')
        embeddings = model.encode(texts, convert_to_list=True)
        return [e for e in embeddings]
    except Exception as e:
        raise ValueError(f"Erreur embeddings: {str(e)}")

def search_rag(query, api_key, n_results=5):
    """Search RAG for relevant passages"""
    try:
        collection = get_collection()
        if collection.count() == 0:
            return []
        
        query_embeddings = embed_texts([query], api_key)
        results = collection.query(
            query_embeddings=query_embeddings,
            n_results=min(n_results, collection.count()),
            include=["documents", "metadatas", "distances"]
        )
        
        passages = []
        for i, doc in enumerate(results['documents'][0]):
            meta = results['metadatas'][0][i]
            distance = results['distances'][0][i]
            if distance < 0.8:  # Only relevant results
                passages.append({
                    "text": doc,
                    "source": meta.get("source", "Document inconnu"),
                    "type": meta.get("type", "reference"),
                    "relevance": round(1 - distance, 2)
                })
        return passages
    except Exception:
        return []

# ── RAG endpoints ─────────────────────────────────────────
@app.route("/rag/upload", methods=["POST"])
def rag_upload():
    try:
        file = request.files.get("file")
        doc_type = request.form.get("doc_type", "reference")
        source_name = request.form.get("source_name", "")
        api_key = request.form.get("api_key", "")

        if not file:
            return jsonify({"error": "Fichier manquant"}), 400

        file_bytes = file.read()
        filename = file.filename
        if not source_name:
            source_name = filename

        # Extract text
        if filename.lower().endswith(('.docx', '.doc')):
            text = extract_text_from_docx(file_bytes)
        elif filename.lower().endswith('.pdf'):
            return jsonify({"error": "PDF non supporté pour la base RAG — utilise DOCX ou TXT"}), 400
        else:
            text = file_bytes.decode('utf-8', errors='ignore')

        if not text or len(text.strip()) < 50:
            return jsonify({"error": "Document vide ou illisible"}), 400

        # Chunk text
        chunks = chunk_text(text)
        if not chunks:
            return jsonify({"error": "Impossible de découper le document"}), 400

        # Generate embeddings
        embeddings = embed_texts(chunks, api_key)

        # Store in ChromaDB
        collection = get_collection()
        ids = [f"{source_name}_{i}_{datetime.datetime.now().timestamp()}" for i in range(len(chunks))]
        metadatas = [{"source": source_name, "type": doc_type, "chunk": i} for i in range(len(chunks))]

        collection.add(
            ids=ids,
            embeddings=embeddings,
            documents=chunks,
            metadatas=metadatas
        )

        return jsonify({
            "success": True,
            "chunks": len(chunks),
            "source": source_name,
            "total_docs": collection.count()
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/rag/list", methods=["GET"])
def rag_list():
    try:
        collection = get_collection()
        if collection.count() == 0:
            return jsonify({"documents": [], "total": 0})

        results = collection.get(include=["metadatas"])
        sources = {}
        for meta in results['metadatas']:
            src = meta.get('source', 'Inconnu')
            if src not in sources:
                sources[src] = {"source": src, "type": meta.get('type', 'reference'), "chunks": 0}
            sources[src]["chunks"] += 1

        return jsonify({
            "documents": list(sources.values()),
            "total": collection.count()
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/rag/delete", methods=["POST"])
def rag_delete():
    try:
        source_name = request.json.get("source") if request.is_json else request.form.get("source")
        if not source_name:
            return jsonify({"error": "Source manquante"}), 400

        collection = get_collection()
        results = collection.get(where={"source": source_name})
        if results['ids']:
            collection.delete(ids=results['ids'])

        return jsonify({"success": True, "deleted": len(results['ids'])})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ── Main endpoints ────────────────────────────────────────
@app.route("/health", methods=["GET"])
def health():
    collection = get_collection()
    return jsonify({"status": "ok", "rag_docs": collection.count()})

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
{{"parties":[{{"id":"partie_1","name":"Nom exact de la partie 1","description":"Rôle court de cette partie"}},{{"id":"partie_2","name":"Nom exact de la partie 2","description":"Rôle court de cette partie"}}]}}
- Utilise les vrais noms tels qu'ils apparaissent dans le contrat
- Maximum 3 parties
- description: max 8 mots"""

        message = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=400,
            system=system,
            messages=[{"role": "user", "content": f"Contrat:\n\n{contract_text[:15000]}\n\nIdentifie les parties."}]
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
        api_key = os.environ.get("ANTHROPIC_API_KEY") or request.form.get("api_key", "")
        partie = request.form.get("partie", "la partie bénéficiaire") or "la partie bénéficiaire"

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

        # RAG: search for relevant legal references
        rag_context = ""
        rag_passages = search_rag(contract_text[:2000], api_key)
        if rag_passages:
            rag_context = "\n\nRÉFÉRENCES JURIDIQUES PERTINENTES (base de connaissances):\n"
            for p in rag_passages:
                rag_context += f"\n[{p['source']}] {p['text'][:400]}\n"
            rag_context += "\nUtilise ces références pour enrichir et justifier tes modifications.\n"

        client = anthropic.Anthropic(api_key=api_key)
        system = f"""Tu es un juriste expert en droit marocain. Analyse ce contrat et propose des modifications pour protéger {partie}.
LANGUE OBLIGATOIRE: Réponds UNIQUEMENT dans la même langue que le contrat, sans mélange.
Type de contrat: {contract_type}
Partie à protéger: {partie}{rag_context}

Retourne UNIQUEMENT du JSON valide, sans markdown:
{{"modifications":[{{"id":1,"clause_name":"nom court","risk":"high|medium|low","reason":"Explication du risque avec référence juridique si disponible.","original":"texte exact copié du contrat, max 50 mots","proposed":"clause complète et professionnelle, max 60 mots"}}]}}

Règles:
- Exactement 5 modifications
- Si des références juridiques sont disponibles, cite-les dans "reason" (ex: "Selon l'article X du COC...")
- original: copie mot pour mot du contrat
- proposed: clause professionnelle favorisant {partie}
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

        result = json.loads(match.group(0))
        result["rag_used"] = len(rag_passages) > 0
        result["rag_sources"] = [p["source"] for p in rag_passages]
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
        author = "ContractSense"
        date = datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%SZ")

        if filename.endswith(".docx") or filename.endswith(".doc"):
            doc = Document(io.BytesIO(file_bytes))
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
                        for run in para.runs:
                            run.text = ""
                        p = para._p
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
        else:
            doc = Document()
            doc.add_heading('ContractSense - Modifications', 0)
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

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
