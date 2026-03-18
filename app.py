import os
import uuid
import datetime
from flask import Flask, request, jsonify
from flask_cors import CORS
from supabase import create_client
import voyageai

app = Flask(__name__)
CORS(app)

# =========================
# 🔐 ENV
# =========================
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
VOYAGE_API_KEY = os.environ.get("VOYAGE_API_KEY")

# =========================
# 🔌 INIT CLIENTS
# =========================
def get_supabase():
    return create_client(SUPABASE_URL, SUPABASE_KEY)

def get_embedding(text):
    client = voyageai.Client(api_key=VOYAGE_API_KEY)
    return client.embed([text], model="voyage-2").embeddings[0]

# =========================
# 💾 RAG SAVE
# =========================
def save_rag_doc(doc):
    try:
        sb = get_supabase()
        sb.table("rag_documents").upsert(doc).execute()
    except Exception as e:
        print(f"save_rag_doc error: {e}")

# =========================
# 📥 QUEUE SAVE
# =========================
def save_queue_item(item):
    try:
        sb = get_supabase()
        sb.table("rag_queue").upsert(item).execute()
    except Exception as e:
        print(f"save_queue_item error: {e}")

def delete_queue_item(item_id):
    try:
        sb = get_supabase()
        sb.table("rag_queue").delete().eq("id", item_id).execute()
    except Exception as e:
        print(f"delete_queue_item error: {e}")

# =========================
# 📤 UPLOAD RAG
# =========================
@app.route("/rag/upload", methods=["POST"])
def upload_rag():
    try:
        content = request.form.get("content")
        title = request.form.get("title")
        category = request.form.get("category", "general")

        if not content:
            return jsonify({"error": "No content"}), 400

        words = content.split()
        chunk_size = 400
        chunks = [" ".join(words[i:i+chunk_size]) for i in range(0, len(words), chunk_size)]

        for i, chunk in enumerate(chunks):
            embedding = get_embedding(chunk)

            save_rag_doc({
                "id": str(uuid.uuid4()),
                "title": f"{title} (partie {i+1})" if len(chunks) > 1 else title,
                "category": category,
                "content": chunk,
                "embedding": embedding,
                "created_at": datetime.datetime.now().isoformat()
            })

        return jsonify({"status": "success", "chunks": len(chunks)})

    except Exception as e:
        return jsonify({"error": str(e)}), 500

# =========================
# 📥 ADD TO QUEUE
# =========================
@app.route("/queue/add", methods=["POST"])
def add_to_queue():
    try:
        data = request.json

        item = {
            "id": str(uuid.uuid4()),
            "content": data.get("content"),
            "title": data.get("title"),
            "category": data.get("category"),
            "status": "pending",
            "created_at": datetime.datetime.now().isoformat()
        }

        save_queue_item(item)

        return jsonify({"status": "queued"})

    except Exception as e:
        return jsonify({"error": str(e)}), 500

# =========================
# ✅ VALIDATE QUEUE
# =========================
@app.route("/queue/validate", methods=["POST"])
def validate_queue():
    try:
        data = request.json
        contract = data.get("contract")

        contract_text = contract.get("content")
        title_base = contract.get("title", "Contract")
        category = contract.get("category", "contract")
        contract_id = contract.get("id")

        words = contract_text.split()
        chunk_size = 400
        chunks = [" ".join(words[i:i+chunk_size]) for i in range(0, len(words), chunk_size)]

        for i, chunk in enumerate(chunks):
            embedding = get_embedding(chunk)

            save_rag_doc({
                "id": str(uuid.uuid4()),
                "title": f"{title_base} (partie {i+1})",
                "category": category,
                "content": chunk,
                "embedding": embedding,
                "source": "validated",
                "validated_at": datetime.datetime.now().isoformat()
            })

        delete_queue_item(contract_id)

        return jsonify({"status": "validated"})

    except Exception as e:
        return jsonify({"error": str(e)}), 500

# =========================
# 🔍 SEARCH RAG
# =========================
@app.route("/rag/search", methods=["POST"])
def search_rag():
    try:
        query = request.json.get("query")

        if not query:
            return jsonify({"error": "No query"}), 400

        query_embedding = get_embedding(query)

        sb = get_supabase()
        res = sb.rpc("match_documents", {
            "query_embedding": query_embedding,
            "match_count": 5
        }).execute()

        return jsonify(res.data)

    except Exception as e:
        return jsonify({"error": str(e)}), 500

# =========================
# ❤️ HEALTH CHECK
# =========================
@app.route("/")
def home():
    return "API RUNNING"

# =========================
# 🚀 RUN
# =========================
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
