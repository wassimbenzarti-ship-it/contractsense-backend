#!/usr/bin/env python3
"""Génère les brouillons d'emails pour les contacts nominatifs (noms réels) sans envoyer."""
import json
import os
from datetime import datetime
import anthropic

client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

GENERIC_KEYWORDS = ["Direction", "Maître"]


def is_nominative(contact):
    if not contact:
        return False
    for kw in GENERIC_KEYWORDS:
        if kw in contact:
            return False
    return len(contact.strip().split()) >= 2


def generate_email(target):
    first_name = target["contact"].split()[0]
    greeting = f"Bonjour {first_name},"

    response = client.messages.create(
        model="claude-haiku-4-5",
        max_tokens=1200,
        messages=[{
            "role": "user",
            "content": f"""Tu es l'assistant de prospection de Westfield Avocats.
Génère un email de prospection en français pour présenter Omniscient by Westfield.

Cible:
- Entreprise: {target['company']}
- Secteur: {target['sector']}
- Contact: {target['contact']}
- Salutation d'ouverture à utiliser: "{greeting}"

Omniscient by Westfield: outil IA d'analyse de contrats (ai.westfieldavocats.com), à partir de 950 MAD/mois.
Détecte les clauses risquées, vérifie conformité droit marocain et français, suggère des révisions.

Retourne UNIQUEMENT un objet JSON valide (pas de markdown, pas de texte autour) :
{{"subject": "Objet court max 60 caractères spécifique au secteur", "html": "<p>{greeting}</p><p>Corps HTML 2-3 paragraphes : accroche spécifique au secteur + présentation Omniscient + CTA démo 20 min</p><p>Cordialement,<br>Wassim Benzarti<br>Westfield Avocats<br>contact@westfieldavocats.com | ai.westfieldavocats.com</p>"}}"""
        }]
    )
    text = response.content[0].text.strip()
    start = text.find('{')
    end = text.rfind('}') + 1
    return json.loads(text[start:end])


with open("prospection/targets.json", "r", encoding="utf-8") as f:
    targets = json.load(f)

nominative_pending = [
    t for t in targets
    if not t.get("sent", False) and is_nominative(t.get("contact", ""))
]
print(f"Contacts nominatifs en attente : {len(nominative_pending)}")

drafts_path = "prospection/drafts.json"
try:
    with open(drafts_path, "r", encoding="utf-8") as f:
        existing = {d["email"]: d for d in json.load(f)}
except (FileNotFoundError, json.JSONDecodeError):
    existing = {}

new_count = 0
for target in nominative_pending:
    if target["email"] in existing:
        print(f"  Déjà généré : {target['contact']} ({target['email']})")
        continue

    print(f"  Génération pour {target['contact']} ({target['company']})...")
    try:
        email = generate_email(target)
        existing[target["email"]] = {
            "email": target["email"],
            "contact": target["contact"],
            "company": target["company"],
            "sector": target["sector"],
            "subject": email["subject"],
            "body": email["html"],
            "approved": False,
            "generated_at": datetime.now().isoformat()
        }
        new_count += 1
        print(f"    ✓ Sujet : {email['subject']}")
    except Exception as e:
        print(f"    ✗ Erreur : {e}")

with open(drafts_path, "w", encoding="utf-8") as f:
    json.dump(list(existing.values()), f, ensure_ascii=False, indent=2)

print(f"\n{new_count} nouveaux brouillons. Total : {len(existing)} dans {drafts_path}")
