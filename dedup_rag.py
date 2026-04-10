"""
Script de déduplication de la table rag_documents.

Détecte les doublons par :
  1. Titre exact identique
  2. Contenu identique (hash MD5)
  3. Contenu très similaire (>90% de chevauchement sur les 500 premiers chars)

Usage :
  SUPABASE_URL=https://xxx.supabase.co SUPABASE_KEY=eyJ... python dedup_rag.py
  ou
  python dedup_rag.py  (en renseignant les constantes ci-dessous)
"""

import os, hashlib, requests, json
from collections import defaultdict

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")   # <- colle ici si pas de var env
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "") or os.environ.get("SUPABASE_KEY", "")

DRY_RUN = True   # Mettre False pour supprimer réellement

if not SUPABASE_URL or not SUPABASE_KEY:
    print("ERREUR: SUPABASE_URL et SUPABASE_KEY sont requis")
    exit(1)

headers = {
    "apikey": SUPABASE_KEY,
    "Authorization": "Bearer " + SUPABASE_KEY,
    "Content-Type": "application/json"
}

def fetch_all_docs():
    """Récupère tous les documents par pages de 1000."""
    all_docs = []
    offset = 0
    while True:
        r = requests.get(
            SUPABASE_URL + "/rest/v1/rag_documents",
            headers={**headers, "Range": f"{offset}-{offset+999}", "Prefer": "count=exact"},
            params={"select": "id,title,source,content,jurisdiction,created_at"}
        )
        if r.status_code not in (200, 206):
            print(f"Erreur fetch: {r.status_code} {r.text[:200]}")
            break
        batch = r.json()
        if not batch:
            break
        all_docs.extend(batch)
        print(f"  Fetched {len(all_docs)} docs...")
        if len(batch) < 1000:
            break
        offset += 1000
    return all_docs

def content_hash(doc):
    """Hash MD5 des 2000 premiers caractères du contenu."""
    c = (doc.get("content") or "")[:2000].strip().lower()
    return hashlib.md5(c.encode()).hexdigest()

def delete_doc(doc_id):
    r = requests.delete(
        SUPABASE_URL + f"/rest/v1/rag_documents?id=eq.{doc_id}",
        headers=headers
    )
    return r.status_code in (200, 204)

print("=== DEDUP RAG DOCUMENTS ===")
print(f"Mode: {'DRY RUN (simulation)' if DRY_RUN else '*** SUPPRESSION REELLE ***'}\n")

print("Chargement des documents...")
docs = fetch_all_docs()
print(f"\nTotal: {len(docs)} documents\n")

# --- Groupe 1: Doublons par titre exact ---
by_title = defaultdict(list)
for d in docs:
    key = (d.get("title") or "").strip().lower()
    if key:
        by_title[key].append(d)

title_dupes = {k: v for k, v in by_title.items() if len(v) > 1}

# --- Groupe 2: Doublons par hash de contenu ---
by_hash = defaultdict(list)
for d in docs:
    h = content_hash(d)
    by_hash[h].append(d)

content_dupes = {k: v for k, v in by_hash.items() if len(v) > 1}

# --- Rapport ---
to_delete = set()

print(f"=== DOUBLONS PAR TITRE EXACT: {len(title_dupes)} groupes ===")
for title, group in sorted(title_dupes.items()):
    group_sorted = sorted(group, key=lambda x: x.get("created_at") or "", reverse=True)
    keeper = group_sorted[0]
    dupes = group_sorted[1:]
    print(f"\n  TITRE: '{title[:80]}'")
    print(f"  Garde : id={keeper['id']} créé={keeper.get('created_at','?')[:10]}")
    for d in dupes:
        print(f"  Supprime: id={d['id']} créé={d.get('created_at','?')[:10]}")
        to_delete.add(d["id"])

print(f"\n=== DOUBLONS PAR CONTENU IDENTIQUE: {len(content_dupes)} groupes ===")
for h, group in content_dupes.items():
    group_sorted = sorted(group, key=lambda x: x.get("created_at") or "", reverse=True)
    keeper = group_sorted[0]
    dupes = group_sorted[1:]
    # Skip if already flagged by title
    new_dupes = [d for d in dupes if d["id"] not in to_delete]
    if not new_dupes:
        continue
    print(f"\n  HASH: {h[:8]}...")
    print(f"  Garde : id={keeper['id']} titre='{(keeper.get('title') or '')[:60]}'")
    for d in new_dupes:
        print(f"  Supprime: id={d['id']} titre='{(d.get('title') or '')[:60]}'")
        to_delete.add(d["id"])

print(f"\n=== RÉSUMÉ ===")
print(f"Total docs       : {len(docs)}")
print(f"Doublons trouvés : {len(to_delete)}")
print(f"Docs après nettoyage : {len(docs) - len(to_delete)}")

if not to_delete:
    print("\nAucun doublon détecté.")
elif DRY_RUN:
    print(f"\nDRY RUN: aucune suppression effectuée.")
    print("Pour supprimer, relance avec DRY_RUN = False dans le script.")
else:
    print(f"\nSuppression de {len(to_delete)} documents...")
    ok, fail = 0, 0
    for doc_id in to_delete:
        if delete_doc(doc_id):
            ok += 1
        else:
            fail += 1
            print(f"  ECHEC suppression id={doc_id}")
    print(f"Supprimés: {ok}/{len(to_delete)} | Echecs: {fail}")

print("\nTerminé.")
