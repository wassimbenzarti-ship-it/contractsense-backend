#!/usr/bin/env python3
"""Génère les brouillons d'emails nominatifs et envoie un email de validation à contact@westfieldavocats.com."""
import json
import os
import re
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime
import anthropic

SMTP_HOST = "ssl0.ovh.net"
SMTP_PORT = 465
FROM_EMAIL = "contact@westfieldavocats.com"
FROM_NAME = "Omniscient Prospection"

client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
smtp_password = os.environ["SMTP_PASSWORD"]

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


def send_validation_email(drafts):
    """Envoie un email récapitulatif à contact@ pour relecture avant envoi."""
    # Exclure les brouillons sans contenu (body null)
    ready = [d for d in drafts if d.get("body")]
    if not ready:
        print("Aucun brouillon avec contenu à envoyer.")
        return

    html_parts = ["""
    <html><body style="font-family: Arial, sans-serif; max-width: 800px; margin: 0 auto; padding: 20px;">
    <h1 style="color: #1a1a2e;">Brouillons à valider — Prospection Omniscient</h1>
    <p style="color: #555;">Voici les {n} emails prêts à être envoyés à vos contacts nominatifs.<br>
    <strong>Pour approuver</strong> : ouvrez votre session Claude et écrivez <em>"OK envoie les nominatifs"</em>.<br>
    <strong>Pour modifier</strong> : précisez le nom du contact et la correction souhaitée.</p>
    <hr/>
    """.format(n=len(ready))]

    for i, d in enumerate(ready, 1):
        html_parts.append(f"""
        <div style="border: 1px solid #ddd; border-radius: 8px; padding: 20px; margin: 20px 0; background: #f9f9f9;">
            <h2 style="margin-top: 0; color: #1a1a2e;">{i}. {d['contact']} — {d['company']}</h2>
            <p><strong>Email :</strong> {d['email']}<br>
            <strong>Secteur :</strong> {d['sector']}</p>
            <div style="background: white; border: 1px solid #ccc; border-radius: 4px; padding: 15px; margin: 10px 0;">
                <p><strong>Objet :</strong> {d['subject']}</p>
                <hr/>
                {d['body']}
            </div>
        </div>
        """)

    html_parts.append("</body></html>")
    html_content = "".join(html_parts)

    msg = MIMEMultipart("alternative")
    msg["From"] = f"{FROM_NAME} <{FROM_EMAIL}>"
    msg["To"] = FROM_EMAIL
    msg["Subject"] = f"[Validation requise] {len(ready)} emails nominatifs à approuver"
    msg.attach(MIMEText(html_content, "html", "utf-8"))

    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=15) as smtp:
        smtp.login(FROM_EMAIL, smtp_password)
        smtp.sendmail(FROM_EMAIL, FROM_EMAIL, msg.as_bytes())
    print(f"✓ Email de validation envoyé à {FROM_EMAIL} ({len(ready)} brouillons)")


# --- Main ---
with open("prospection/targets.json", "r", encoding="utf-8") as f:
    targets = json.load(f)

nominative_pending = [
    t for t in targets
    if not t.get("sent", False) and is_nominative(t.get("contact", ""))
]
print(f"Contacts nominatifs en attente : {len(nominative_pending)}")

if not nominative_pending:
    print("Aucun contact nominatif à traiter.")
    exit(0)

drafts_path = "prospection/drafts.json"
try:
    with open(drafts_path, "r", encoding="utf-8") as f:
        existing = {d["email"]: d for d in json.load(f)}
except (FileNotFoundError, json.JSONDecodeError):
    existing = {}

new_count = 0
for target in nominative_pending:
    # Générer si absent OU si body est null (entrée placeholder)
    if target["email"] in existing and existing[target["email"]].get("body"):
        print(f"  Déjà généré : {target['contact']}")
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

print(f"\n{new_count} nouveaux brouillons générés.")

# Envoyer l'émail de validation avec tous les brouillons non approuvés
pending_approval = [d for d in existing.values() if not d.get("approved", False)]
if pending_approval:
    print(f"\nEnvoi email de validation pour {len(pending_approval)} brouillons...")
    send_validation_email(pending_approval)
else:
    print("Tous les brouillons sont déjà approuvés.")
