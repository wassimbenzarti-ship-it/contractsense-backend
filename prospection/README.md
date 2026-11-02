# Prospection Omniscient

Ce dossier est géré automatiquement par la Routine Claude.

## Fonctionnement
1. La Routine cloud génère les emails et pousse `emails_to_send.json`
2. Le GitHub Action `send-prospection.yml` se déclenche automatiquement
3. Les emails sont envoyés via OVH SMTP depuis `contact@westfieldavocats.com`

## Configuration requise
Ajouter le secret GitHub `SMTP_PASSWORD` dans :
Settings → Secrets and variables → Actions → New repository secret
