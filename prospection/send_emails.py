#!/usr/bin/env python3
"""
Envoi des emails de prospection Omniscient via OVH SMTP.
Déclenché par GitHub Actions quand emails_to_send.json est poussé.
"""
import smtplib
import json
import os
import sys
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

SMTP_HOST = "ssl0.ovh.net"
SMTP_PORT = 465
FROM_EMAIL = "contact@westfieldavocats.com"
FROM_NAME  = "Wassim Benzarti — Westfield Avocats"

password = os.environ.get("SMTP_PASSWORD")
if not password:
    print("ERREUR : variable SMTP_PASSWORD non définie.")
    sys.exit(1)

data_file = os.path.join(os.path.dirname(__file__), "emails_to_send.json")
with open(data_file, encoding="utf-8") as f:
    emails = json.load(f)

if not emails:
    print("Aucun email à envoyer.")
    sys.exit(0)

print(f"Connexion à {SMTP_HOST}:{SMTP_PORT}...")
ok = fail = 0

with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=30) as smtp:
    smtp.login(FROM_EMAIL, password)
    print("Connecté.")

    for e in emails:
        try:
            msg = MIMEMultipart("alternative")
            msg["From"] = f"{FROM_NAME} <{FROM_EMAIL}>"
            msg["To"] = e["to"]
            msg["Subject"] = e["subject"]
            msg.attach(MIMEText(e["html"], "html", "utf-8"))
            smtp.sendmail(FROM_EMAIL, e["to"], msg.as_bytes())
            print(f"✓ {e['dest']} → {e['to']}")
            ok += 1
        except Exception as ex:
            print(f"✗ {e['dest']} → {e['to']} : {ex}")
            fail += 1

print(f"\nTerminé : {ok} envoyés, {fail} échecs.")
if fail > 0:
    sys.exit(1)
