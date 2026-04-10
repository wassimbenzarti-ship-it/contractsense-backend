"""
Script pour tagger manuellement la juridiction des docs RAG existants.

Usage:
  # Voir tous les docs sans jurisdiction (ou avec 'auto'/'universel')
  python tag_jurisdiction.py --list

  # Tagger TOUS les docs null/auto/universel en droit_marocain
  python tag_jurisdiction.py --tag-all droit_marocain

  # Tagger un doc spécifique par ID
  python tag_jurisdiction.py --tag-id abc123 droit_francais

  # Voir les docs par juridiction
  python tag_jurisdiction.py --stats

Juridictions valides:
  droit_marocain  droit_francais  droit_algerien  droit_tunisien  universel
"""

import os, sys, requests, json

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "") or os.environ.get("SUPABASE_KEY", "")
VALID_JURISDICTIONS = ["droit_marocain", "droit_francais", "droit_algerien", "droit_tunisien", "universel"]

if not SUPABASE_URL or not SUPABASE_KEY:
    print("ERREUR: SUPABASE_URL et SUPABASE_KEY requis")
    print("  SUPABASE_URL=... SUPABASE_KEY=... python tag_jurisdiction.py --stats")
    sys.exit(1)

headers = {
    "apikey": SUPABASE_KEY,
    "Authorization": "Bearer " + SUPABASE_KEY,
    "Content-Type": "application/json"
}

def fetch_all(select="id,title,source,jurisdiction,created_at"):
    docs, offset = [], 0
    while True:
        r = requests.get(
            SUPABASE_URL + "/rest/v1/rag_documents",
            headers={**headers, "Range": f"{offset}-{offset+999}"},
            params={"select": select}
        )
        batch = r.json() if r.ok else []
        if not batch: break
        docs.extend(batch)
        if len(batch) < 1000: break
        offset += 1000
    return docs

def update_jurisdiction(doc_id, jurisdiction):
    r = requests.patch(
        SUPABASE_URL + f"/rest/v1/rag_documents?id=eq.{doc_id}",
        headers=headers,
        json={"jurisdiction": jurisdiction}
    )
    return r.status_code in (200, 204)

def cmd_stats():
    print("Chargement...")
    docs = fetch_all()
    from collections import Counter
    c = Counter((d.get("jurisdiction") or "NULL").lower() for d in docs)
    print(f"\n{'JURIDICTION':<25} {'NB DOCS':>8}")
    print("-" * 35)
    for jur, count in sorted(c.items(), key=lambda x: -x[1]):
        print(f"  {jur:<23} {count:>8}")
    print(f"\n  TOTAL: {len(docs)} documents")

def cmd_list():
    print("Chargement...")
    docs = fetch_all()
    untagged = [d for d in docs if not d.get("jurisdiction") or d["jurisdiction"] in ("", "auto", "universel", None)]
    print(f"\n{len(untagged)} docs sans juridiction précise:\n")
    print(f"  {'ID':<40} {'TITLE':<60} {'JURIDICTION'}")
    print("-" * 115)
    for d in untagged[:100]:
        jur = d.get("jurisdiction") or "NULL"
        title = (d.get("title") or d.get("source") or "")[:58]
        print(f"  {d['id']:<40} {title:<60} {jur}")
    if len(untagged) > 100:
        print(f"  ... et {len(untagged)-100} autres")

def cmd_tag_all(jurisdiction):
    if jurisdiction not in VALID_JURISDICTIONS:
        print(f"Juridiction invalide: {jurisdiction}")
        print(f"Valides: {', '.join(VALID_JURISDICTIONS)}")
        sys.exit(1)
    print("Chargement...")
    docs = fetch_all()
    to_tag = [d for d in docs if not d.get("jurisdiction") or d["jurisdiction"] in ("", "auto", "universel", None)]
    print(f"\n{len(to_tag)} docs à tagger en '{jurisdiction}'")
    confirm = input("Confirmer ? (oui/non) : ").strip().lower()
    if confirm != "oui":
        print("Annulé.")
        return
    ok, fail = 0, 0
    for d in to_tag:
        title = (d.get("title") or d.get("source") or "")[:50]
        if update_jurisdiction(d["id"], jurisdiction):
            ok += 1
            print(f"  ✓ {title}")
        else:
            fail += 1
            print(f"  ✗ ECHEC: {title}")
    print(f"\nTaggés: {ok} | Echecs: {fail}")

def cmd_tag_id(doc_id, jurisdiction):
    if jurisdiction not in VALID_JURISDICTIONS:
        print(f"Juridiction invalide. Valides: {', '.join(VALID_JURISDICTIONS)}")
        sys.exit(1)
    if update_jurisdiction(doc_id, jurisdiction):
        print(f"✓ Doc {doc_id} -> {jurisdiction}")
    else:
        print(f"✗ Echec pour {doc_id}")

# --- CLI ---
args = sys.argv[1:]
if not args or args[0] == "--stats":
    cmd_stats()
elif args[0] == "--list":
    cmd_list()
elif args[0] == "--tag-all" and len(args) >= 2:
    cmd_tag_all(args[1])
elif args[0] == "--tag-id" and len(args) >= 3:
    cmd_tag_id(args[1], args[2])
else:
    print(__doc__)
