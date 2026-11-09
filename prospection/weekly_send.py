#!/usr/bin/env python3
import os
import json
import smtplib
import imaplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
import anthropic

SMTP_HOST = "ssl0.ovh.net"
SMTP_PORT = 465
IMAP_HOST = "ssl0.ovh.net"
IMAP_PORT = 993
FROM_EMAIL = "contact@westfieldavocats.com"
FROM_NAME = "Wassim Benzarti — Westfield Avocats"
MAX_PER_DAY = 5

GENERIC_KEYWORDS = ["Direction", "Maître"]


def is_nominative(contact):
    """True si le contact est un vrai nom (pas Direction / Maître X)."""
    if not contact:
        return False
    for kw in GENERIC_KEYWORDS:
        if kw in contact:
            return False
    return len(contact.strip().split()) >= 2


def load_approved_drafts():
    """Charge les brouillons approuvés pour les contacts nominatifs."""
    try:
        with open("prospection/drafts.json", "r", encoding="utf-8") as f:
            drafts = json.load(f)
        return {d["email"]: d for d in drafts if d.get("approved", False)}
    except Exception:
        return {}


def generate_email(client, target):
    first_name = target.get("contact", "").split()[0] if target.get("contact", "") not in ["Direction", "Direction Juridique", ""] else ""
    greeting = f"Bonjour {first_name}," if first_name else "Bonjour,"

    response = client.messages.create(
        model="claude-haiku-4-5",
        max_tokens=1500,
        messages=[{
            "role": "user",
            "content": f"""Tu es l'assistant de prospection de Westfield Avocats.
Génère un email de prospection en français pour présenter Omniscient by Westfield.

Cible:
- Entreprise: {target['company']}
- Secteur: {target['sector']}
- Contact: {target.get('contact', 'la direction juridique')}
- Salutation d'ouverture à utiliser: "{greeting}"

Omniscient by Westfield: outil IA d'analyse de contrats (https://ai.westfieldavocats.com), à partir de 950 MAD/mois.
Détecte les clauses risquées, vérifie conformité droit marocain et français, suggère des révisions.

RETOURNE UNIQUEMENT un objet JSON valide :
{{"subject": "Objet court max 60 caractères spécifique au secteur",
"html": "Corps HTML COMPLET avec :
1. <p>{greeting}</p>
2. <p>2-3 paragraphes : accroche sectorielle + présentation Omniscient + bénéfice concret</p>
3. <p style=\"text-align:center;margin:24px 0;\"><a href=\"https://ai.westfieldavocats.com\" style=\"background:#1a1a2e;color:#ffffff;padding:12px 28px;border-radius:6px;text-decoration:none;font-weight:bold;font-size:15px;\">Essayer Omniscient gratuitement &rarr;</a></p>
4. <p>Ou répondez à cet email pour convenir d'un appel découverte de 20 minutes.</p>
5. <p>Cordialement,<br>Wassim Benzarti<br>Westfield Avocats<br>contact@westfieldavocats.com</p>"
}}"""
        }]
    )
    text = response.content[0].text.strip()
    start = text.find('{')
    end = text.rfind('}') + 1
    return json.loads(text[start:end])


def build_message(to, subject, html):
    msg = MIMEMultipart("alternative")
    msg["From"] = f"{FROM_NAME} <{FROM_EMAIL}>"
    msg["To"] = to
    msg["Subject"] = subject
    msg.attach(MIMEText(html, "html", "utf-8"))
    return msg


def copy_to_sent(imap, raw_msg):
    _, folders = imap.list()
    sent_folder = "Sent"
    for f in folders:
        name = f.decode().split('"')[-1].strip()
        if any(x in name.lower() for x in ["sent", "envoy", "envoye"]):
            sent_folder = name
            break
    imap.append(sent_folder, "\\Seen", None, raw_msg)


def main():
    anthropic_key = os.environ["ANTHROPIC_API_KEY"]
    smtp_password = os.environ["SMTP_PASSWORD"]

    with open("prospection/targets.json", "r", encoding="utf-8") as f:
        targets = json.load(f)

    approved_drafts = load_approved_drafts()

    # File d'envoi :
    # - contacts nominatifs : seulement si brouillon approuvé
    # - contacts génériques : génération à la volée
    send_queue = []
    for t in targets:
        if t.get("sent", False):
            continue
        if is_nominative(t.get("contact", "")):
            if t["email"] in approved_drafts:
                send_queue.append((t, approved_drafts[t["email"]]))
        else:
            send_queue.append((t, None))

    send_queue = send_queue[:MAX_PER_DAY]

    if not send_queue:
        print("Aucune cible à contacter aujourd'hui.")
        return

    print(f"{len(send_queue)} cibles à contacter aujourd'hui.")
    client = anthropic.Anthropic(api_key=anthropic_key)

    ok, fail = 0, 0
    sent_raws = []

    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=15) as smtp:
        smtp.login(FROM_EMAIL, smtp_password)
        for target, draft in send_queue:
            try:
                print(f"Envoi pour {target['company']} ({target.get('contact', '')})...")
                if draft:
                    subject = draft["subject"]
                    html = draft["body"]
                    print(f"  (brouillon approuvé)")
                else:
                    email_data = generate_email(client, target)
                    subject = email_data["subject"]
                    html = email_data["html"]
                msg = build_message(target["email"], subject, html)
                raw = msg.as_bytes()
                smtp.sendmail(FROM_EMAIL, target["email"], raw)
                sent_raws.append(raw)
                target["sent"] = True
                print(f"✓ Envoyé → {target['email']}")
                ok += 1
            except Exception as e:
                print(f"✗ Échec → {target['email']}: {e}")
                fail += 1

    if sent_raws:
        try:
            print("\nCopie dans le dossier Envoyés...")
            imap = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
            imap.login(FROM_EMAIL, smtp_password)
            for raw in sent_raws:
                copy_to_sent(imap, raw)
            imap.logout()
            print(f"✓ {len(sent_raws)} emails copiés dans Envoyés")
        except Exception as e:
            print(f"(dossier Envoyés non mis à jour : {e})")

    with open("prospection/targets.json", "w", encoding="utf-8") as f:
        json.dump(targets, f, ensure_ascii=False, indent=2)

    print(f"\nTerminé : {ok} envoyés, {fail} échecs.")
    if fail > 0:
        exit(1)


if __name__ == "__main__":
    main()
