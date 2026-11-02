#!/usr/bin/env python3
"""
Prospection hebdomadaire Omniscient by Westfield
Génère des emails personnalisés via Claude API et les envoie via OVH SMTP
"""
import os
import json
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
import anthropic

SMTP_HOST = "ssl0.ovh.net"
SMTP_PORT = 465
FROM_EMAIL = "contact@westfieldavocats.com"
FROM_NAME = "Wassim Benzarti — Westfield Avocats"
MAX_PER_WEEK = 5


def generate_email(client, target):
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
- Contact: {target.get('contact', 'la direction juridique')}

Omniscient by Westfield: outil IA d'analyse de contrats (ai.westfieldavocats.com), à partir de 950 MAD/mois.
Détecte les clauses risquées, vérifie conformité droit marocain et français, suggère des révisions.

Retourne UNIQUEMENT un objet JSON valide (pas de markdown, pas de texte autour) :
{{"subject": "Objet court max 60 caractères spécifique au secteur", "html": "<p>Corps HTML 3 paragraphes max : accroche personnalisée sur le secteur + présentation Omniscient + CTA démo 20 min</p><p>Cordialement,<br>Wassim Benzarti<br>Westfield Avocats<br>contact@westfieldavocats.com | ai.westfieldavocats.com</p>"}}"""
        }]
    )
    text = response.content[0].text.strip()
    start = text.find('{')
    end = text.rfind('}') + 1
    return json.loads(text[start:end])


def send_email(smtp, to, subject, html):
    msg = MIMEMultipart("alternative")
    msg["From"] = f"{FROM_NAME} <{FROM_EMAIL}>"
    msg["To"] = to
    msg["Subject"] = subject
    msg.attach(MIMEText(html, "html", "utf-8"))
    smtp.sendmail(FROM_EMAIL, to, msg.as_bytes())


def main():
    anthropic_key = os.environ["ANTHROPIC_API_KEY"]
    smtp_password = os.environ["SMTP_PASSWORD"]

    with open("prospection/targets.json", "r", encoding="utf-8") as f:
        targets = json.load(f)

    unsent = [t for t in targets if not t.get("sent", False)][:MAX_PER_WEEK]

    if not unsent:
        print("Aucune cible à contacter cette semaine.")
        return

    print(f"{len(unsent)} cibles à contacter cette semaine.")
    client = anthropic.Anthropic(api_key=anthropic_key)

    ok, fail = 0, 0
    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT) as smtp:
        smtp.login(FROM_EMAIL, smtp_password)
        for target in unsent:
            try:
                print(f"Génération email pour {target['company']}...")
                email_data = generate_email(client, target)
                send_email(smtp, target["email"], email_data["subject"], email_data["html"])
                target["sent"] = True
                print(f"✓ Envoyé → {target['email']}")
                ok += 1
            except Exception as e:
                print(f"✗ Échec → {target['email']}: {e}")
                fail += 1

    with open("prospection/targets.json", "w", encoding="utf-8") as f:
        json.dump(targets, f, ensure_ascii=False, indent=2)

    print(f"\nTerminé : {ok} envoyés, {fail} échecs.")
    if fail > 0:
        exit(1)


if __name__ == "__main__":
    main()
