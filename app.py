from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
import anthropic
import json
import os
import io
import re
import zipfile
import datetime
import hashlib
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
import base64
import uuid
import numpy as np
import voyageai
import requests
from docx import Document
try:
    import olefile as olefile_lib
    HAS_OLEFILE = True
except ImportError:
    HAS_OLEFILE = False
from docx.oxml.ns import qn
from docx.oxml import OxmlElement

app = Flask(__name__)
CORS(app, origins=[
    "https://ai.westfieldavocats.com",
    "https://westfieldavocats.com",
    "https://www.westfieldavocats.com",
    "https://wassimbenzarti-ship-it.github.io",
    "https://contractsense.fr",
    "https://www.contractsense.fr",
    "http://localhost",
    "http://localhost:3000",
    "http://localhost:5173",
    "null"
], supports_credentials=True)

def get_legal_framework(contract_type, jurisdiction="auto"):
    """Return mandatory legal constraints per contract type and jurisdiction."""
    ct = (contract_type or "").lower()
    jur = (jurisdiction or "auto").lower()

    is_employment = ct in ("employment", "cdi", "cdd") or any(k in ct for k in ["travail", "emploi", "salari"])
    is_service = ct in ("service", "services", "saas", "nda") or any(k in ct for k in ["prestation", "conseil", "maintenance", "logiciel", "informatique", "mission"])
    is_purchase = ct in ("purchase",) or any(k in ct for k in ["achat", "vente", "distribution"])

    if is_employment:
        if jur in ("droit_marocain", "auto", "universel"):
            return (
                "DROIT DU TRAVAIL MAROCAIN - REGLES IMPERATIVES:\n"
                "- CDD: max 1 an, renouvelable UNE seule fois (Art. 16 CT)\n"
                "- Renouvellement abusif = requalification automatique en CDI\n"
                "- Preavis: 8j (<1an), 1 mois (1-5ans), 2 mois (>5ans) ouvriers; 1/2/3 mois cadres\n"
                "- Indemnite licenciement: 96h/an (3 premieres annees), 144h/an apres\n"
                "- Licenciement: cause reelle et serieuse obligatoire\n"
                "- Heures sup: +25% jour, +50% nuit/vendredi, +100% dimanche\n"
                "- Conge: 1.5 jour/mois (min 18 jours/an)\n"
                "- Toute clause moins favorable que la loi est NULLE"
            )
        elif jur == "droit_francais":
            return (
                "DROIT DU TRAVAIL FRANCAIS - REGLES IMPERATIVES:\n"
                "- CDD: cas limitatifs, max 18 mois renouvelable une fois\n"
                "- Periode essai CDI: 2 mois ouvriers, 3 mois maitrise, 4 mois cadres (renouvelable)\n"
                "- Preavis: selon convention collective (min 1 mois usage)\n"
                "- Heures sup: +25% (8 premieres h/sem), +50% au-dela\n"
                "- Conges: 2.5 jours ouvrables/mois (30j/an)\n"
                "- Licenciement: cause reelle et serieuse, procedure contradictoire obligatoire"
            )
        elif jur == "droit_anglais":
            return (
                "ENGLISH EMPLOYMENT LAW - MANDATORY RULES:\n"
                "- Unfair dismissal: 2 years continuous employment required\n"
                "- Statutory notice: 1 week per year of service (max 12 weeks)\n"
                "- National Living Wage applies\n"
                "- Statutory annual leave: 28 days (incl. bank holidays)\n"
                "- Working Time Regulations: max 48h/week average (opt-out possible)\n"
                "- Written employment statement required from day 1"
            )
        else:
            return (
                "DROIT DU TRAVAIL APPLICABLE - A VERIFIER:\n"
                "- Identifier le droit du travail applicable (clause de droit applicable)\n"
                "- Verifier duree maximale legale de la periode d'essai\n"
                "- Verifier preavis minimum et indemnites de licenciement selon loi locale\n"
                "- Verifier conformite aux minima legaux de remuneration et de conges\n"
                "- Toute clause moins favorable que la loi locale est nulle"
            )
    elif is_service:
        if jur in ("droit_marocain", "auto", "universel"):
            return (
                "DROIT MAROCAIN - PRESTATION DE SERVICES:\n"
                "- Delai de paiement: max 60 jours (Art. 78 loi 15-95)\n"
                "- Penalites de retard legales: taux directeur BAM + 3 points\n"
                "- Clauses limitatives de responsabilite admises si non abusives (Art. 263 DOC)\n"
                "- Clause NDA: confidentialite Art. 231 DOC\n"
                "- Clause non-concurrence: limitee dans le temps et l'espace"
            )
        elif jur == "droit_francais":
            return (
                "DROIT FRANCAIS - PRESTATION DE SERVICES:\n"
                "- Delai de paiement: max 30j (art. L441-10 C.com.) ou 60j date de facture\n"
                "- Clauses abusives entre professionnels prohibees (art. L442-1 C.com.)\n"
                "- Responsabilite: droit commun art. 1231 C.civ.\n"
                "- RGPD obligatoire si traitement de donnees personnelles\n"
                "- Cession droits PI: doit etre explicite (CPI)"
            )
        elif jur == "droit_anglais":
            return (
                "ENGLISH CONTRACT LAW - SERVICES:\n"
                "- Implied terms under Supply of Goods and Services Act 1982\n"
                "- Unfair Contract Terms Act 1977: exclusion clauses subject to reasonableness\n"
                "- Late Payment of Commercial Debts Act: statutory interest applies\n"
                "- GDPR: data processing agreements required if personal data involved\n"
                "- IP assignment: must be in writing (Copyright, Designs and Patents Act 1988)"
            )
        else:
            return (
                "DROIT APPLICABLE - PRESTATION DE SERVICES:\n"
                "- Identifier le droit applicable et verifier les delais de paiement legaux\n"
                "- Verifier les clauses de responsabilite et d'exclusion selon le droit local\n"
                "- Verifier conformite aux regles de protection des donnees applicables\n"
                "- Verifier cession des droits de propriete intellectuelle"
            )
    elif is_purchase:
        if jur in ("droit_marocain", "auto", "universel"):
            return (
                "DROIT MAROCAIN - VENTE:\n"
                "- Garantie des vices caches: 1 an (Art. 573 DOC)\n"
                "- Transfert de propriete: a la livraison sauf clause contraire\n"
                "- Reserve de propriete possible jusqu'au paiement complet"
            )
        elif jur == "droit_francais":
            return (
                "DROIT FRANCAIS - VENTE:\n"
                "- Garantie des vices caches: 2 ans (art. 1648 C.civ.)\n"
                "- Garantie de conformite: 2 ans (consommateur)\n"
                "- Transfert propriete: accord des parties (sauf reserve)\n"
                "- Reserve de propriete possible jusqu'au paiement complet"
            )
        else:
            return (
                "DROIT APPLICABLE - VENTE:\n"
                "- Verifier regime de garantie legale selon le droit applicable\n"
                "- Verifier moment du transfert de propriete et des risques\n"
                "- Reserve de propriete a prevoir si paiement differe"
            )
    else:
        if jur == "droit_marocain":
            return "Respecte le droit marocain applicable et les principes generaux du DOC."
        elif jur == "droit_francais":
            return "Respecte le droit francais applicable (Code civil, Code de commerce)."
        elif jur == "droit_anglais":
            return "Apply English law and common law principles applicable to this contract."
        else:
            return "Identifie le droit applicable dans ce contrat et applique ses regles imperatives."

def detect_jurisdiction(text, title=""):
    """Detect legal jurisdiction from document/contract text using keyword matching."""
    combined = ((title or "") + " " + (text or "")[:3000]).lower()
    # Moroccan
    if any(k in combined for k in ["dahir", "doc (dahir", "code du travail marocain", "loi 09-08", "bank al-maghrib", "banque al-maghrib", "cnss", "cimr", "maroc", "marocain", "marocaine", "rabat", "casablanca", "agadir", "fes", "cour supreme du maroc", "tribunal de commerce de casa"]):
        return "droit_marocain"
    # Tunisian
    if any(k in combined for k in ["tunisie", "tunisien", "tunisienne", "code du travail tunisien", "banque centrale de tunisie", "tunis", "sfax"]):
        return "droit_tunisien"
    # Algerian
    if any(k in combined for k in ["algerie", "algerien", "algerienne", "code du travail algerien", "banque d'algerie", "alger", "oran"]):
        return "droit_algerien"
    # Belgian
    if any(k in combined for k in ["belgique", "belge", "droit belge", "code civil belge", "bruxelles", "liege", "gand"]):
        return "droit_belge"
    # French
    if any(k in combined for k in ["code civil francais", "code du travail francais", "cnil", "rgpd", "tribunal de grande instance", "cour de cassation", "code de commerce francais", "droit francais", "loi francaise", "paris", "france", "francais", "francaise"]):
        return "droit_francais"
    # English/UK/Common law
    if any(k in combined for k in ["english law", "uk law", "companies act", "employment rights act", "common law", "court of appeal", "high court", "gdpr", "united kingdom", "england", "wales", "scotland", "london", "british"]):
        return "droit_anglais"
    return "universel"


# ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ Party label normalization ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ
CONTRACT_CATEGORIES = {
    "service": "Prestation de services",
    "saas": "SaaS / Logiciel",
    "nda": "Confidentialite (NDA)",
    "employment": "Contrat de travail",
    "purchase": "Achat / Vente",
    "partnership": "Partenariat",
    "collaboration": "Convention de collaboration",
    "generic": "Generique",
}

PARTY_KEYWORDS = [
    (["prestataire", "service provider", "fournisseur", "mandate"], "favorable prestataire"),
    (["client", "customer", "mandant", "donneur"], "favorable client"),
    (["employeur", "employer"], "favorable employeur"),
    (["employe", "employee", "salarie"], "favorable employe"),
    (["divulgateur", "disclosing"], "favorable divulgateur"),
    (["destinataire", "receiving"], "favorable destinataire"),
    (["vendeur", "seller"], "favorable vendeur"),
    (["acheteur", "buyer"], "favorable acheteur"),
]

def normalize_party_label(partie, contract_type=None):
    if not partie:
        return "neutre"
    p = partie.lower().strip()
    for keywords, label in PARTY_KEYWORDS:
        if any(k in p for k in keywords):
            return label
    # Derive from contract type
    defaults = {
        "service": "favorable prestataire",
        "saas": "favorable prestataire",
        "collaboration": "favorable prestataire",
        "employment": "favorable employe",
        "nda": "favorable divulgateur",
        "purchase": "favorable vendeur",
    }
    if contract_type in defaults:
        return defaults[contract_type]
    # Clean up ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ remove company names, keep first word
    first_word = p.split()[0] if p.split() else p
    return "favorable " + first_word

# ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ Supabase client ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ
SUPA_URL = os.environ.get("SUPABASE_URL", "")
SUPA_KEY = os.environ.get("SUPABASE_KEY", "")
SUPA_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")

# ── Email (SMTP) ──────────────────────────────────────────────────────────────
SMTP_HOST     = os.environ.get("SMTP_HOST", "")
SMTP_PORT     = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER     = os.environ.get("SMTP_USER", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
SMTP_FROM     = os.environ.get("SMTP_FROM", SMTP_USER)

def send_email(to: str, subject: str, html: str):
    if not SMTP_HOST or not SMTP_USER or not SMTP_PASSWORD:
        print(f"[EMAIL] SMTP non configuré — email non envoyé à {to}", flush=True)
        return
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = SMTP_FROM
        msg["To"]      = to
        msg.attach(MIMEText(html, "html", "utf-8"))
        if SMTP_PORT == 465:
            with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT) as s:
                s.login(SMTP_USER, SMTP_PASSWORD)
                s.sendmail(SMTP_FROM, [to], msg.as_string())
        else:
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
                s.starttls()
                s.login(SMTP_USER, SMTP_PASSWORD)
                s.sendmail(SMTP_FROM, [to], msg.as_string())
        print(f"[EMAIL] Envoyé à {to} — {subject}", flush=True)
    except Exception as e:
        print(f"[EMAIL] Erreur envoi à {to}: {e}", flush=True)

# ── In-memory file cache ──────────────────────────────────────────────────────
# Stores original uploaded files (bytes) keyed by UUID so /export can retrieve
# them even when the client no longer has the file. Limited to 200 entries ~100 MB.
_FILE_CACHE: dict = {}
_FILE_CACHE_ORDER: list = []
_FILE_CACHE_MAX = 200

def _cache_store(key: str, data: bytes):
    _FILE_CACHE[key] = data
    _FILE_CACHE_ORDER.append(key)
    if len(_FILE_CACHE_ORDER) > _FILE_CACHE_MAX:
        old = _FILE_CACHE_ORDER.pop(0)
        _FILE_CACHE.pop(old, None)

def _cache_get(key):
    return _FILE_CACHE.get(key)

# CMI Payment config
CMI_CLIENT_ID   = os.environ.get("CMI_CLIENT_ID", "")
CMI_STORE_KEY   = os.environ.get("CMI_STORE_KEY", "")
CMI_PAYMENT_URL = os.environ.get("CMI_PAYMENT_URL", "https://testpayment.cmi.co.ma/fim/est3Dgate")
APP_URL         = os.environ.get("APP_URL", "https://westfieldavocats.com").strip().rstrip("/")

def supa_headers():
    return {
        "apikey": SUPA_KEY,
        "Authorization": "Bearer " + SUPA_KEY,
        "Content-Type": "application/json",
        "Prefer": "return=minimal"
    }

def supa_get(table, params=None):
    url = SUPA_URL + "/rest/v1/" + table
    r = requests.get(url, headers=supa_headers(), params=params, timeout=30)
    r.raise_for_status()
    return r.json()

def supa_update(table, record_id, updates):
    url = SUPA_URL + f"/rest/v1/{table}?id=eq.{record_id}"
    r = requests.patch(url, headers=supa_headers(), json=updates, timeout=10)
    if not r.content or r.status_code == 204:
        return {"_status": r.status_code}
    try:
        return r.json()
    except Exception:
        return {"_status": r.status_code}

def supa_insert(table, data):
    key = SUPA_SERVICE_KEY or SUPA_KEY
    url = SUPA_URL + "/rest/v1/" + table
    headers = {
        "apikey": key,
        "Authorization": "Bearer " + key,
        "Content-Type": "application/json",
        "Prefer": "return=minimal"
    }
    r = requests.post(url, headers=headers, json=data, timeout=30)
    if not r.ok:
        detail = r.text[:500]
        print("supa_insert ERROR " + str(r.status_code) + ": " + detail)
        raise Exception(f"Erreur base de données ({r.status_code}): {detail}")
    return r

def supa_delete(table, filters):
    url = SUPA_URL + "/rest/v1/" + table
    r = requests.delete(url, headers=supa_headers(), params=filters, timeout=30)
    r.raise_for_status()
    return r

def supa_patch(table, updates, filter_str):
    """PATCH with a raw Supabase filter string, e.g. 'email=eq.foo@bar.com'"""
    url = SUPA_URL + f"/rest/v1/{table}?{filter_str}"
    r = requests.patch(url, headers=supa_headers(), json=updates, timeout=10)
    return r

def supa_upsert(table, data, on_conflict="email"):
    """UPSERT: insert or update on conflict. Uses service key. data must be a dict."""
    key = SUPA_SERVICE_KEY or SUPA_KEY
    url = SUPA_URL + f"/rest/v1/{table}?on_conflict={on_conflict}"
    headers = {
        "apikey": key,
        "Authorization": "Bearer " + key,
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates,return=minimal"
    }
    r = requests.post(url, headers=headers, json=data, timeout=10)
    if not r.ok:
        print(f"supa_upsert ERROR {r.status_code}: {r.text[:500]}")
    return r

def _storage_headers():
    key = SUPA_SERVICE_KEY or SUPA_KEY
    return {
        "apikey": key,
        "Authorization": "Bearer " + key,
    }

def supa_storage_ensure_bucket(bucket_name):
    """Create the storage bucket if it doesn't exist (idempotent)."""
    url = SUPA_URL + "/storage/v1/bucket"
    r = requests.post(url, headers={**_storage_headers(), "Content-Type": "application/json"},
                      json={"id": bucket_name, "name": bucket_name, "public": False}, timeout=10)
    return r

def supa_storage_upload(bucket, path, file_bytes, content_type="application/octet-stream"):
    """Upload a file to Supabase Storage, auto-creating the bucket if missing."""
    url = SUPA_URL + f"/storage/v1/object/{bucket}/{path}"
    headers = {**_storage_headers(), "Content-Type": content_type}
    r = requests.post(url, headers=headers, data=file_bytes, timeout=60)
    # Supabase returns 400 with "Bucket not found" when bucket doesn't exist
    bucket_missing = r.status_code in (400, 404) and "ucket" in r.text
    if bucket_missing:
        supa_storage_ensure_bucket(bucket)
        r = requests.post(url, headers=headers, data=file_bytes, timeout=60)
    return r

def supa_storage_download(bucket, path):
    """Download a file from Supabase Storage. Returns bytes or None."""
    url = SUPA_URL + f"/storage/v1/object/{bucket}/{path}"
    r = requests.get(url, headers=_storage_headers(), timeout=60)
    if r.ok:
        return r.content
    print(f"supa_storage_download failed {r.status_code}: {r.text[:200]}")
    return None

def parse_dt(s):
    """Parse ISO datetime string, strip timezone info for naive comparison."""
    if not s:
        return None
    try:
        dt = datetime.datetime.fromisoformat(s)
        return dt.replace(tzinfo=None)  # make naive
    except Exception:
        return None

# ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ RAG: Supabase REST storage ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ
def load_rag(contract_type=None, limit=200):
    """Load RAG docs ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ load a sample from each category for /rag/list endpoint only"""
    try:
        # Load sample from each category for display
        docs = supa_get("rag_documents", {
            "select": "id,title,content,source,category,party_label",
            "limit": str(limit)
        })
        return {"documents": docs or []}
    except Exception as e:
        print("load_rag error: " + str(e))
        return {"documents": []}

def clean_text(text):
    """Remove null bytes and invalid unicode for Supabase"""
    if not isinstance(text, str):
        return text
    return text.replace("\x00", "").replace("\u0000", "")

def save_rag_doc(doc):
    try:
        doc_copy = dict(doc)
        # Clean all string fields
        for k, v in doc_copy.items():
            if isinstance(v, str):
                doc_copy[k] = clean_text(v)
        
        # Save embedding both as JSON (legacy) and as vector (pgvector)
        emb = doc_copy.get("embedding")
        if emb and isinstance(emb, list) and len(emb) == 1024:
            doc_copy["embedding_vector"] = emb  # pgvector column
            doc_copy["embedding"] = json.dumps(emb)  # legacy JSON column
            print("save_rag_doc: embedding 1024 dims OK")
        elif emb and isinstance(emb, list):
            doc_copy.pop("embedding_vector", None)  # skip pgvector for 512 dims
            doc_copy["embedding"] = json.dumps(emb)

        supa_insert("rag_documents", doc_copy)
        print("save_rag_doc OK: " + str(doc_copy.get("title","?"))[:50])
    except Exception as e:
        print("save_rag_doc ERROR: " + str(e))
        raise

def delete_rag_by_source(source):
    try:
        import re as _re
        docs = supa_get("rag_documents", {"select": "id,title", "limit": "1000"})
        count = 0
        for d in (docs or []):
            base = _re.sub(r" \(partie \d+\)$", "", d.get("title", ""))
            if base == source:
                supa_delete("rag_documents", {"id": "eq." + d["id"]})
                count += 1
        return count
    except Exception as e:
        print("delete_rag error: " + str(e))
        return 0

def cosine_similarity(a, b):
    a, b = np.array(a, dtype=float), np.array(b, dtype=float)
    # Skip if different dimensions
    if a.shape != b.shape:
        return 0.0
    return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-10))

def get_embedding(text, voyage_key=None, input_type="document"):
    # Try Voyage AI for semantic embeddings
    if voyage_key:
        try:
            vo = voyageai.Client(api_key=voyage_key)
            result = vo.embed([text[:1000]], model="voyage-law-2", input_type=input_type)
            return result.embeddings[0]
        except Exception as e:
            print("Voyage AI error: " + str(e))
            pass
    # Fallback to TF-IDF hashing
    import hashlib
    words = re.findall(r'\w+', text.lower())
    vec = [0.0] * 512
    for word in words:
        h = int(hashlib.md5(word.encode()).hexdigest(), 16) % 512
        vec[h] += 1.0
    for i in range(len(words)-1):
        bigram = words[i] + '_' + words[i+1]
        h = int(hashlib.sha256(bigram.encode()).hexdigest(), 16) % 512
        vec[h] += 0.5
    norm = sum(v*v for v in vec) ** 0.5
    if norm > 0:
        vec = [v/norm for v in vec]
    return vec

def search_rag_pgvector(query_embedding, top_k=10, doc_type=None, user_id=None):
    """Search RAG using pgvector directly in Supabase ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ fast semantic search"""
    try:
        url = SUPA_URL + "/rest/v1/rpc/search_rag"
        # Convert embedding list to pgvector string format
        if isinstance(query_embedding, list):
            vec_str = "[" + ",".join(str(x) for x in query_embedding) + "]"
        else:
            vec_str = str(query_embedding)
        payload = {
            "query_embedding": vec_str,
            "match_count": top_k,
            "filter_type": doc_type
        }
        # If user_id provided, search only their models
        if user_id:
            payload["filter_user_id"] = user_id
        key = SUPA_SERVICE_KEY or SUPA_KEY
        headers = {"apikey": key, "Authorization": "Bearer " + key, "Content-Type": "application/json"}
        r = requests.post(url, headers=headers, json=payload, timeout=15)
        if r.ok:
            results = r.json()
            print(f"pgvector search: {len(results)} results")
            return results or []
        else:
            print("pgvector search error " + str(r.status_code) + ": " + r.text[:300])
            return []
    except Exception as e:
        print("pgvector search exception: " + str(e))
        return []

def search_rag(query, api_key, voyage_key=None, top_k=5, partie=None):
    data = load_rag()
    if not data["documents"]:
        return []
    query_vec = get_embedding(query, voyage_key)
    scored = []
    for doc in data["documents"]:
        emb = doc.get("embedding")
        if emb is None:
            continue
        # Parse embedding from JSON string if needed
        if isinstance(emb, str):
            try:
                emb = json.loads(emb)
            except:
                continue
        score = cosine_similarity(query_vec, emb)
        # Boost for matching party
        doc_label = doc.get("party_label") or ""
        if partie and doc_label and partie.lower() in doc_label.lower():
            score *= 1.3
        # Boost validated clauses
        if "validated_clause" in doc.get("source", ""):
            score *= 1.2
        scored.append((score, doc))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [doc for _, doc in scored[:top_k]]

# ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ Text extraction ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ
def extract_text_from_doc_ole(file_bytes):
    """Extract text from old .doc format using olefile"""
    if not HAS_OLEFILE:
        return None
    try:
        ole = olefile_lib.OleFileIO(io.BytesIO(file_bytes))
        if not ole.exists('WordDocument'):
            return None
        stream = ole.openstream('WordDocument').read()
        text = stream.decode('latin-1', errors='ignore')
        clean = re.sub(r'[^\x20-\x7E\x80-\xFF\n\r\t]', ' ', text)
        clean = re.sub(r' {3,}', ' ', clean)
        clean = re.sub(r'\n{3,}', '\n\n', clean)
        # Skip binary header ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ find first readable content
        for marker in ['CONTRAT', 'Contrat', 'CONTRACT', 'ACCORD', 'CONVENTION']:
            idx = clean.find(marker)
            if idx != -1 and idx < len(clean) // 2:
                return clean[idx:]
        # Fallback: skip first third
        return clean[len(clean)//4:]
    except Exception as e:
        print("OLE extract error: " + str(e))
        return None

def extract_text_from_docx(file_bytes):
    try:
        doc = Document(io.BytesIO(file_bytes))
        text = []
        for para in doc.paragraphs:
            if para.text.strip():
                text.append(para.text)
        return "\n".join(text)
    except Exception:
        # Try OLE for old .doc format
        ole_text = extract_text_from_doc_ole(file_bytes)
        if ole_text:
            return ole_text
        try:
            with zipfile.ZipFile(io.BytesIO(file_bytes)) as z:
                if 'word/document.xml' in z.namelist():
                    doc_xml = z.read('word/document.xml').decode('utf-8', errors='ignore')
                    text = re.sub(r'<[^>]+>', ' ', doc_xml)
                    return re.sub(r'\s+', ' ', text).strip()
        except Exception as e2:
            raise ValueError("Impossible de lire le fichier Word: " + str(e2))

def read_file(file):
    file_bytes = file.read()
    filename = file.filename.lower()
    if filename.endswith(".docx") or filename.endswith(".doc"):
        text = extract_text_from_docx(file_bytes)
    else:
        text = file_bytes.decode("utf-8", errors="ignore")
    # Remove null bytes
    text = text.replace("\x00", "").replace("\u0000", "") if text else text
    return text, file_bytes, filename

# ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ AI functions ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ
def identify_parties(contract_text, lang, api_key):
    client = anthropic.Anthropic(api_key=api_key)
    system = f"""Tu es un juriste expert. Identifie les parties dans ce contrat.
RÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ©ponds UNIQUEMENT en {'anglais' if lang == 'en' else 'franÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ§ais'} avec ce JSON exact, sans markdown:
{{"parties":[{{"id":"partie_1","name":"Nom exact de la partie 1","description":"Role de cette partie"}},{{"id":"partie_2","name":"Nom exact de la partie 2","description":"Role de cette partie"}}]}}
- Utilise les vrais noms tels qu'ils apparaissent dans le contrat
- Maximum 3 parties, description max 10 mots"""

    message = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=500,
        system=system,
        messages=[{"role": "user", "content": f"Contrat:\n\n{contract_text[:20000]}\n\nIdentifie les parties."}]
    )
    raw = message.content[0].text
    match = re.search(r'\{[\s\S]*\}', raw)
    if not match:
        raise ValueError("RÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ©ponse invalide")
    return json.loads(match.group(0))

def build_numbered_paragraphs(file_bytes, filename):
    """Build a numbered paragraph index from DOCX for precise matching"""
    try:
        if filename.endswith('.docx') or filename.endswith('.doc'):
            doc = Document(io.BytesIO(file_bytes))
            paragraphs = []
            for i, para in enumerate(doc.paragraphs):
                text = para.text.strip()
                if text:
                    paragraphs.append({"idx": i, "text": text})
            return paragraphs
    except:
        pass
    return []

def analyze_contract(contract_text, lang, contract_type, api_key, partie="la partie bÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ©nÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ©ficiaire", file_bytes=None, filename=""):
    api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise ValueError("ClÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ© API manquante")
    client = anthropic.Anthropic(api_key=api_key)

    # Detect legal jurisdiction of the contract
    _jurisdiction = detect_jurisdiction(contract_text)
    print(f"Detected jurisdiction: {_jurisdiction}")

    # Build numbered paragraphs for precise matching
    paragraphs = build_numbered_paragraphs(file_bytes, filename) if file_bytes else []
    
    # Build numbered contract text for AI
    if paragraphs:
        numbered_text = "\n".join(("[P" + str(p["idx"]) + "] " + p["text"]) for p in paragraphs[:150])
    else:
        numbered_text = contract_text[:20000]

    # ── Structured RAG: separate model docs (protection) from legal docs (conformite) ──
    model_context = ""
    legal_context = ""
    _rag_contract_count = 0
    _rag_legal_count = 0
    _rag_is_voyage = False
    LEGAL_CATS = {"loi", "law", "doctrine", "jurisprudence", "legal", "legislation"}
    try:
        voyage_key = os.environ.get("VOYAGE_API_KEY", "")
        search_query = contract_type + " " + partie + " " + contract_text[:500]
        query_vec = get_embedding(search_query, voyage_key, input_type="query")
        is_voyage = bool(voyage_key) and len(query_vec) == 1024

        all_docs = []

        # Primary: pgvector semantic search (requires Voyage 1024-dim embeddings)
        if is_voyage:
            all_docs = search_rag_pgvector(query_vec, top_k=20)
            print(f"pgvector: {len(all_docs)} docs")

        # Fallback: direct Supabase fetch + in-memory cosine similarity
        if not all_docs:
            print("RAG fallback: fetching docs with embeddings from Supabase")
            try:
                key = SUPA_SERVICE_KEY or SUPA_KEY
                raw_url = SUPA_URL + "/rest/v1/rag_documents"
                raw_headers = {"apikey": key, "Authorization": "Bearer " + key}
                raw_params = {"select": "id,title,content,source,category,party_label,jurisdiction,embedding", "limit": "150"}
                raw_r = requests.get(raw_url, headers=raw_headers, params=raw_params, timeout=20)
                if raw_r.ok:
                    raw_docs = raw_r.json() or []
                    scored = []
                    for doc in raw_docs:
                        emb = doc.get("embedding")
                        if isinstance(emb, str):
                            try: emb = json.loads(emb)
                            except: emb = None
                        if emb and isinstance(emb, list):
                            score = cosine_similarity(query_vec, emb)
                            scored.append((score, doc))
                    scored.sort(key=lambda x: x[0], reverse=True)
                    all_docs = [d for _, d in scored[:20]]
                    print(f"Fallback RAG: {len(all_docs)} docs ranked from {len(raw_docs)} total")
                else:
                    print(f"Fallback RAG fetch error {raw_r.status_code}: {raw_r.text[:200]}")
            except Exception as fe:
                print("Fallback RAG error: " + str(fe))

        # Jurisdiction boost: docs matching contract jurisdiction rank higher
        def _jur_score(doc):
            doc_jur = (doc.get("jurisdiction") or "universel").lower()
            if doc_jur == _jurisdiction or doc_jur in ("universel", "auto"):
                return 1.0
            return 0.5  # penalize mismatched jurisdiction but don't exclude
        all_docs.sort(key=lambda d: _jur_score(d), reverse=True)
        print(f"Jurisdiction filter: {_jurisdiction} | matching={sum(1 for d in all_docs if (d.get('jurisdiction') or 'universel') in (_jurisdiction, 'universel', 'auto'))}/{len(all_docs)}")

        contract_docs = [d for d in all_docs if d.get("category","").lower() not in LEGAL_CATS]
        legal_docs    = [d for d in all_docs if d.get("category","").lower() in LEGAL_CATS]

        # Dedicated per-category searches to ensure coverage of all doc types
        seen_ids = {d.get("id") for d in all_docs}
        _cat_key = SUPA_SERVICE_KEY or SUPA_KEY
        _cat_url = SUPA_URL + "/rest/v1/rag_documents"
        _cat_hdrs = {"apikey": _cat_key, "Authorization": "Bearer " + _cat_key}
        for cat in ["contract", "law", "doctrine", "jurisprudence"]:
            try:
                _cat_params = {"select": "id,title,content,source,category,party_label,jurisdiction,embedding",
                               "category": "eq." + cat, "limit": "50"}
                _cat_r = requests.get(_cat_url, headers=_cat_hdrs, params=_cat_params, timeout=15)
                if _cat_r.ok:
                    _cat_raw = _cat_r.json() or []
                    _cat_scored = []
                    for doc in _cat_raw:
                        emb = doc.get("embedding")
                        if isinstance(emb, str):
                            try: emb = json.loads(emb)
                            except: emb = None
                        if emb and isinstance(emb, list):
                            score = cosine_similarity(query_vec, emb)
                            _cat_scored.append((score, doc))
                    _cat_scored.sort(key=lambda x: x[0], reverse=True)
                    _added = 0
                    for _score, doc in _cat_scored[:8]:
                        if doc.get("id") not in seen_ids:
                            seen_ids.add(doc.get("id"))
                            if cat == "contract":
                                contract_docs.append(doc)
                            else:
                                legal_docs.append(doc)
                            _added += 1
                    _top = f"{_cat_scored[0][0]:.3f}" if _cat_scored else "n/a"
                    print(f"Category [{cat}]: {len(_cat_raw)} docs, top={_top}, added {_added}")
                else:
                    print(f"Category fetch [{cat}] error {_cat_r.status_code}: {_cat_r.text[:100]}")
            except Exception as _ce:
                print(f"Category search error [{cat}]: {_ce}")

        protected_kw = ["lexisnexis","dalloz","lamy","mernissi","traite-de-droit","pdf-free","lexis"]

        # Context 1: model docs -> protection client
        if contract_docs:
            validated = [d for d in contract_docs if "validated_clause" in d.get("source","")]
            reference = [d for d in contract_docs if "validated_clause" not in d.get("source","")]
            model_context = "\n\n=== MODELES DE CONTRATS ET CLAUSES PROTECTRICES ===\n"
            for doc in (validated + reference)[:8]:
                title = doc.get("title","") or doc.get("source","modele")
                is_prot = any(p in (title + doc.get("source","")).lower() for p in protected_kw)
                model_context += "\n=== " + str(title) + " ===\n" + str(doc.get("content",""))[:600] + "\n"
                model_context += "\u2192 rag_source: " + ("null (protege)" if is_prot else str(title)) + "\n"
                if doc.get("party_label"): model_context += "[PARTIE PROTEGEE PAR CE MODELE: " + str(doc.get("party_label","")) + "]\n"

        # Context 2: legal docs -> conformite
        if legal_docs:
            legal_context = "\n\n=== REFERENCES JURIDIQUES (LOIS / DOCTRINE / JURISPRUDENCE) ===\n"
            for doc in legal_docs[:8]:
                cat = doc.get("category","reference").upper()
                title = doc.get("title","") or doc.get("source","reference")
                legal_context += "\n[" + cat + "] " + str(title) + "\n" + str(doc.get("content",""))[:600] + "\n"
                legal_context += "\u2192 rag_source: " + str(title) + "\n"

        _rag_contract_count = len(contract_docs)
        _rag_legal_count = len(legal_docs)
        _rag_is_voyage = is_voyage
        print(f"RAG final: {len(contract_docs)} contract docs, {len(legal_docs)} legal docs | model={len(model_context)}c legal={len(legal_context)}c")
    except Exception as e:
        print("RAG search error: " + str(e))
        import traceback; traceback.print_exc()
    rag_context = model_context  # legacy compat for prompt below

    # Detect contract language
    english_words = len([w for w in contract_text[:2000].lower().split() if w in ['the','and','of','to','in','for','is','this','agreement','shall','party','parties','contract','hereby','whereas','including','provided','subject','pursuant','accordance','obligation','represent','warrant','indemnify','liability','termination','governing','arbitration','confidential']])
    french_words = len([w for w in contract_text[:2000].lower().split() if w in ['le','la','les','de','du','des','en','et','est','que','qui','une','par','pour','sur','dans','avec','aux','au','contrat','sociÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ©tÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ©','article','prÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ©sent','parties','prestataire','client','mandant','mandataire','clause','accord','convention','rÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ©siliation','responsabilitÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ©','confidentialitÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ©']])
    arabic_words = len([w for w in contract_text[:2000].split() if any(0x0600 <= ord(c) <= 0x06FF for c in w)])
    
    if arabic_words > 10:
        detected_lang = "AR (Arabic)"
    elif english_words > french_words:
        detected_lang = "EN (English)"
    else:
        detected_lang = "FR (French)"
    
    print(f"Detected language: {detected_lang} (en={english_words}, fr={french_words}, ar={arabic_words})")

    # Define what "favorable" means for each role
    role_objectives = {
        "employeur": "maximiser la flexibilitÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ© opÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ©rationnelle, minimiser les obligations et coÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ»ts, renforcer le pouvoir de direction et de contrÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ´le, faciliter la rÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ©siliation, protÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ©ger les intÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ©rÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂªts commerciaux",
        "employe": "garantir la stabilitÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ© de l'emploi, maximiser les protections et indemnitÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ©s, limiter les obligations post-contrat, encadrer les heures et conditions de travail",
        "prestataire": "garantir le paiement, limiter la responsabilitÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ©, protÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ©ger la propriÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ©tÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ© intellectuelle, encadrer les modifications de scope",
        "client": "garantir la qualitÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ© et les dÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ©lais, maximiser les pÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ©nalitÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ©s, faciliter la rÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ©siliation, protÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ©ger les donnÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ©es",
        "acheteur": "garantir la conformitÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ©, maximiser les garanties, faciliter les recours",
        "vendeur": "garantir le paiement, limiter les garanties et responsabilitÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ©s",
    }
    # Extract role from partie label
    role_key = "employeur"
    for key in role_objectives:
        if key in partie.lower():
            role_key = key
            break
    role_obj = role_objectives.get(role_key, "protÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ©ger ses intÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ©rÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂªts")

    system = (
        "Tu es un avocat d'affaires senior avec 20 ans d'expÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ©rience en droit des contrats. Ta responsabilitÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ© professionnelle est engagÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ©e.\n"
        "MISSION CRITIQUE: Analyser EXHAUSTIVEMENT ce contrat. Tu n'as pas le droit ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ  l'erreur ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ chaque clause dÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ©savantageuse non identifiÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ©e est une faute professionnelle.\n"
        "OBLIGATION D'EXHAUSTIVITÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ: Tu DOIS analyser CHAQUE clause du contrat, une par une. Ne saute AUCUN paragraphe.\n"
        "FAVORISER: " + partie + "\n\n"
        "LANGUE DU CONTRAT: " + detected_lang + "\n"
        "JURIDICTION DETECTEE: " + _jurisdiction + "\n"
        "RÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂGLE ABSOLUE: Tu DOIS rÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ©pondre dans LA MÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂME LANGUE QUE LE CONTRAT.\n"
        "- Contrat en ANGLAIS ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ tous les champs (reason, proposed, clause_name) en ANGLAIS UNIQUEMENT\n"
        "- Contrat en FRANÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂAIS ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ tous les champs en FRANÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂAIS UNIQUEMENT\n"
        "- Contrat en ARABE ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ tous les champs en ARABE UNIQUEMENT\n"
        "FAUTE PROFESSIONNELLE: rÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ©pondre en franÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ§ais pour un contrat anglais est une erreur grave.\n"
        "INTERDICTION ABSOLUE de mÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ©langer les langues ou rÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ©pondre dans une autre langue.\n"
        "TYPE DE CONTRAT: " + contract_type + "\n"
        "PARTIE ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ PROTÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂGER: " + partie + "\n"
        "OBJECTIFS CONCRETS pour " + partie + ": " + role_obj + "\n\n"
        "RÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂGLES D'ANALYSE PROFESSIONNELLE:\n"
        "1. EXHAUSTIVITÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ TOTALE: Identifie TOUTES les clauses dÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ©savantageuses pour " + partie + " ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ mÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂªme les clauses en apparence neutres\n"
        "2. CLAUSES ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ RISQUE: Cherche spÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ©cifiquement: limitation de responsabilitÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ©, rÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ©siliation unilatÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ©rale, pÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ©nalitÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ©s asymÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ©triques, clauses d'exclusivitÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ© abusives, dÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ©lais de paiement dÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ©favorables, cessions de droits excessives, clauses de non-concurrence, force majeure restrictive, juridiction dÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ©favorable\n"
        "3. NOUVELLES CLAUSES PROTECTRICES - OBLIGATOIRE ABSOLU: Verifie si ces clauses sont ABSENTES du contrat. Si absente, tu DOIS la creer (type=nouvelle_clause, original=null). METHODE OBLIGATOIRE pour rediger chaque nouvelle clause: (1) Cherche DABORD dans les === MODELES DE CONTRATS === du contexte une clause similaire favorable a la partie protegee. Si trouvee: adapte ce texte exactement, mets son rag_source. (2) Seulement si AUCUN modele pertinent dans le contexte: redige depuis tes connaissances juridiques, rag_source=null. Clauses a verifier obligatoirement: (A) Clause de non-concurrence [duree/perimetre/compensation], (B) Clause penale [indemnite fixe en cas de violation], (C) Clause de non-sollicitation [interdiction de debauchage], (D) Clause de performance/objectifs [KPI et consequences], (E) Clause de remboursement formation [si depart avant X mois]. ERREUR GRAVE si le JSON final ne contient AUCUN element type=nouvelle_clause.\n"
        "4. NIVEAU RÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂDACTIONNEL: Style avocat d'affaires senior ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ prÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ©cis, technique, sans ambiguÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¯tÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ©\n"
        "5. SOURCES RAG: Chaque document du contexte a une ligne ‘→ rag_source: [titre]’. Pour chaque modification, copie EXACTEMENT le titre de la source la plus pertinente du contexte dans rag_source. Si la ligne indique ‘null (protege)’ mets null. Tu DOIS citer au moins une source par tranche de 3 modifications.\n"
        "6. LÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂGALITÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ: Toutes les modifications doivent respecter le droit applicable ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ jamais de clauses illÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ©gales\n\n"
        "PROCESSUS D'ANALYSE:\n"
        "ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂtape 1: Lis tout le contrat\n"
        "ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂtape 2: Pour chaque paragraphe, demande-toi: Cette clause est-elle favorable, neutre ou dÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ©favorable ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ  " + partie + " ?\n"
        "ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂtape 3: Pour chaque clause dÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ©favorable ou neutre amÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ©liorable ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ propose une modification\n"
        "ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂtape 4: VÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ©rifie les protections manquantes ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ propose des clauses additionnelles\n"
        "ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂtape 5: VÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ©rifie chaque modification contre le RAG pour citer les sources\n\n"
        + get_legal_framework(contract_type, _jurisdiction) +
        "\n\n"
        + model_context +
        (("\n\nATTENTION MODELES RAG:\n" "- Si [PARTIE PROTEGEE PAR CE MODELE] correspond a " + partie + ": inspire-toi directement de cette clause.\n" "- Si ce modele protege l'autre partie: le contrat a analyser risque de contenir une telle clause - identifie-la et propose de la modifier pour avantager " + partie + ".\n" "- INTERDICTION: ne jamais proposer une clause qui avantage l'autre partie.\n") if model_context else "") +
        legal_context +
        "\n\nATTENTION sur les clauses valid\u00e9es du RAG:\n"
        "- VERIFICATION OBLIGATOIRE avant chaque proposed: est-ce que cette clause protege " + partie + " ? Si non, reformule-la pour l'avantager.\n"
        "- ERREUR GRAVE: proposer une clause de limitation de responsabilite, exclusion de garantie ou peine pour " + partie + " - ces clauses protegent l'autre partie.\n"
        "- CONTROLE FINAL: lis chaque proposed et confirme que " + partie + " obtient un AVANTAGE net par rapport au contrat original.\n\n"
        "IMPORTANT: Le contrat est numÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ©rotÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ© [P0], [P1], etc.\n\n"
        "Retourne UNIQUEMENT du JSON valide, sans markdown:\n"
        '{"modifications":[{"id":1,"para_idx":32,"clause_name":"nom court","risk":"high|medium|low",'
        '"reason":"explication","type":"modification","original":"texte EXACT du paragraphe",'
        '"proposed":"clause reformulee favorisant ' + partie + '","insertion_after":null,'
        '"rag_source":"titre EXACT du contexte ou null"}],'
        '"nouvelles_clauses":[{"id":11,"para_idx":null,"clause_name":"non-concurrence",'
        '"risk":"high","reason":"Protection absente - inspire du modele RAG en priorite",'
        '"type":"nouvelle_clause","original":null,'
        '"proposed":"Clause complete favorisant ' + partie + ' avec duree, perimetre et compensation",'
        '"insertion_after":50,"rag_source":"titre EXACT modele RAG ou null"}],'
        '"compliance":[{"id":1,"type":"loi|doctrine|jurisprudence","source":"Titre exact","issue":"Art. XX CT - description","severity":"high|medium|low","recommendation":"Ce que prevoir","para_idx":5}]}\n\n'
        "CONFORMITE OBLIGATOIRE (MINIMUM 3 elements) - JURIDICTION: " + _jurisdiction + "\\n"
        "Pour CONTRAT DE TRAVAIL (CDI/CDD): verifier periode d'essai, preavis, heures sup, conges, protection contre licenciement abusif selon le droit " + _jurisdiction + ".\\n"
        "Pour CONTRAT DE PRESTATION DE SERVICES: (A) plafond/exclusions de responsabilite; (B) resiliation et preavis; (C) protection des donnees personnelles (RGPD/loi 09-08/applicable); (D) cession droits de propriete intellectuelle sur livrables; (E) confidentialite.\\n"
        "Pour CONTRAT COMMERCIAL: garanties, transfert de risques, conditions de paiement, reserve de propriete selon le droit applicable.\\n"
        "REGLES COMPLIANCE: Dans source mets le titre exact du document RAG OU la reference legale depuis tes connaissances (article + loi + juridiction). Dans issue cite l'article exact. compliance=[] est INTERDIT - genere au minimum 3 elements depuis tes connaissances si le RAG ne couvre pas le type de contrat.\\n"
        "R\u00e8gles:\n"
        "- MINIMUM 6 modifications (type=modification) dans le tableau modifications\n"
        "- MINIMUM 3 nouvelles_clauses (type=nouvelle_clause, original=null) dans nouvelles_clauses: non-concurrence, clause penale, non-sollicitation ou autres protections absentes\n"
        "- para_idx: numÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ©ro entier du paragraphe\n"
        "- original: copie EXACTE sans modification\n"
        "- proposed: clause juridique complÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¨te et professionnelle, rÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ©digÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ©e en style contractuel soutenu\n"
        "- proposed: utilise le vocabulaire juridique appropriÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ© (nonobstant, en ce compris, ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ  titre de, ci-aprÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¨s, sous rÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ©serve de...)\n"
        "- proposed: structure avec sujet + verbe + objet + conditions + exceptions si nÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ©cessaire\n"
        "- proposed: max 120 mots, mais suffisamment dÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ©taillÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ© pour ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂªtre opÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ©rationnel sans ambiguÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¯tÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ©\n"
        "- proposed: jamais de blancs ou placeholders comme ___ ou [ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ  complÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ©ter]\n"
        "- proposed: rÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ©dige comme un avocat d'affaires senior rÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ©digeant pour un client exigeant\n"
        "- VÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ©rifie chaque proposed: est-ce que ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ§a avantage bien " + partie + " ? Si non, reformule."
    )

    # Limit text to avoid timeout
    truncated_text = numbered_text[:15000]
    message = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=4000,
        system=system,
        messages=[{"role": "user", "content": "Contrat:\n\n" + truncated_text + "\n\nRetourne le JSON."}]
    )
    raw = message.content[0].text
    print("RAW FULL:", raw[:3000])

    # Strip markdown code blocks
    raw = re.sub(r'```(?:json)?\s*', '', raw)
    raw = raw.replace('```', '')

    # Extract modifications array directly - more robust than full JSON parsing
    # Find all modification objects
    mod_pattern = re.compile(
        r'\{\s*"id"\s*:\s*(\d+)[\s\S]*?"proposed"\s*:\s*"((?:[^"\\]|\\.)*)"',
        re.DOTALL
    )

    # First try standard JSON parsing
    match = re.search(r'\{[\s\S]*"modifications"[\s\S]*\}', raw)
    if match:
        json_str = match.group(0)
        # Fix double opening braces
        json_str = re.sub(r'\{\s*\{', '{', json_str)
        # Remove control characters
        json_str = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', ' ', json_str)
        # Remove trailing commas
        json_str = re.sub(r',\s*}', '}', json_str)
        json_str = re.sub(r',\s*]', ']', json_str)
        # Fix missing commas between keys (common Claude mistake)
        json_str = re.sub(r'("|}|\d|true|false|null)\s*\n\s*"', r'\1,\n"', json_str)
        try:
            result = json.loads(json_str)
        except:
            result = None
    else:
        result = None

    # Fallback: extract individual modification objects using brace tracking
    if not result or not result.get("modifications"):
        mods = []
        # Track braces to find complete objects
        depth = 0
        start = -1
        for i, c in enumerate(raw):
            if c == "{":
                if depth == 0:
                    start = i
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0 and start >= 0:
                    obj_str = raw[start:i+1]
                    if '"id"' in obj_str and '"proposed"' in obj_str:
                        try:
                            clean = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', ' ', obj_str)
                            clean = re.sub(r',\s*}', '}', clean)
                            clean = re.sub(r',\s*]', ']', clean)
                            obj = json.loads(clean)
                            if obj.get("proposed"):
                                mods.append(obj)
                        except:
                            pass
                    start = -1

        if not mods:
            # Last resort: regex extraction
            ids = re.findall(r'"id"\s*:\s*(\d+)', raw)
            names = re.findall(r'"clause_name"\s*:\s*"([^"]+)"', raw)
            risks = re.findall(r'"risk"\s*:\s*"([^"]+)"', raw)
            originals = re.findall(r'"original"\s*:\s*"((?:[^"\\]|\\.)*)"', raw)
            proposeds = re.findall(r'"proposed"\s*:\s*"((?:[^"\\]|\\.)*)"', raw)
            reasons = re.findall(r'"reason"\s*:\s*"((?:[^"\\]|\\.)*)"', raw)
            rag_sources = re.findall(r'"rag_source"\s*:\s*(?:"((?:[^"\\\\]|\\\\.)*?)"|null)', raw)
            types = re.findall(r'"type"\s*:\s*"([^"]+)"', raw)
            insertions = re.findall(r'"insertion_after"\s*:\s*(\d+|null)', raw)
            for i in range(min(len(ids), len(proposeds))):
                mods.append({
                    "id": int(ids[i]) if i < len(ids) else i+1,
                    "clause_name": names[i] if i < len(names) else "Clause",
                    "risk": risks[i] if i < len(risks) else "medium",
                    "type": types[i] if i < len(types) else "modification",
                    "reason": reasons[i] if i < len(reasons) else "",
                    "original": originals[i] if i < len(originals) else "",
                    "proposed": proposeds[i] if i < len(proposeds) else "",
                    "insertion_after": int(insertions[i]) if i < len(insertions) and insertions[i] != 'null' else None,
                    "rag_source": rag_sources[i] if i < len(rag_sources) and rag_sources[i] else None
                })

        if mods:
            result = {"modifications": mods}
        else:
            raise ValueError("Impossible d'extraire les modifications")

    # Add confidence score based on RAG usage
    mods = result.get("modifications", [])
    # Merge nouvelles_clauses into modifications array
    nouvelles = result.get("nouvelles_clauses", [])
    if nouvelles and isinstance(nouvelles, list):
        for nc in nouvelles:
            if isinstance(nc, dict):
                nc["type"] = "nouvelle_clause"
                nc["original"] = nc.get("original") or None
                nc["id"] = len(mods) + 1
                mods.append(nc)
    # Fallback: if still no nouvelle_clause, add hardcoded defaults
    has_new_clause = any(m.get("type") == "nouvelle_clause" for m in mods)
    if not has_new_clause:
        _last_para = len(paragraphs) - 1 if paragraphs else None
        _defaults = [
            {"type": "nouvelle_clause", "clause_name": "Non-concurrence", "risk": "high",
             "reason": "Clause absente - protection essentielle de l'employeur",
             "original": None, "para_idx": None, "insertion_after": _last_para,
             "proposed": "Le salarie s'interdit, pendant une duree de 12 mois apres cessation du contrat, d'exercer une activite concurrente directement ou indirectement pour tout concurrent de l'Employeur dans le secteur geographique concerne. En contrepartie, l'Employeur verse une indemnite de non-concurrence egale a 30% de la remuneration mensuelle brute par mois de restriction.",
             "rag_source": None},
            {"type": "nouvelle_clause", "clause_name": "Clause penale", "risk": "medium",
             "reason": "Dissuasion contre rupture fautive et protection contre licenciement abusif",
             "original": None, "para_idx": None, "insertion_after": _last_para,
             "proposed": "En cas de licenciement abusif au sens de l'Art. 63 du Code du Travail, l'Employeur verse au Salarie une indemnite forfaitaire equivalente a 3 mois de salaire brut, independamment des indemnites legales.",
             "rag_source": None},
            {"type": "nouvelle_clause", "clause_name": "Non-sollicitation", "risk": "medium",
             "reason": "Protection des equipes et clients de l'employeur",
             "original": None, "para_idx": None, "insertion_after": _last_para,
             "proposed": "Pendant le contrat et 24 mois apres cessation, l'Employeur s'interdit de solliciter ou recruter tout collaborateur ayant travaille avec le Salarie. Toute violation entraine une indemnite forfaitaire de 6 mois de salaire brut.",
             "rag_source": None},
        ]
        for _nc in _defaults:
            _nc["id"] = len(mods) + 1
            mods.append(_nc)
    rag_backed = sum(1 for m in mods if m.get("rag_source"))
    result["_rag_coverage"] = str(rag_backed) + "/" + str(len(mods)) + " sur RAG"
    result["_jurisdiction"] = _jurisdiction
    result["_paragraphs"] = paragraphs
    # Extract compliance if present
    compliance = result.get("compliance", [])
    if not isinstance(compliance, list):
        compliance = []
    # Fallback: inject compliance items if empty, based on contract type + jurisdiction
    if not compliance:
        _ct = (contract_type or "").lower()
        _jur = result.get("_jurisdiction", _jurisdiction)
        if any(kw in _ct for kw in ["service", "presta", "conseil", "maintenance", "logiciel", "informatique", "mission"]):
            if _jur in ("droit_marocain", "universel", "auto"):
                compliance = [
                    {"id": 1, "type": "loi", "source": "Dahir des Obligations et Contrats (DOC) - Art.263",
                     "issue": "Plafond de responsabilite contractuelle absent ou insuffisant",
                     "severity": "high", "recommendation": "Definir un plafond de responsabilite (ex: montant du contrat) et lister les exclusions.", "para_idx": None},
                    {"id": 2, "type": "loi", "source": "Loi 09-08 - Protection des donnees personnelles (CNDP)",
                     "issue": "Absence de clause sur le traitement des donnees personnelles",
                     "severity": "high", "recommendation": "Ajouter une clause designant le responsable de traitement et les obligations de securite.", "para_idx": None},
                    {"id": 3, "type": "loi", "source": "DOC Art.754 - Resiliation",
                     "issue": "Conditions de resiliation et preavis insuffisamment precises",
                     "severity": "medium", "recommendation": "Preciser le preavis minimum, les cas de resiliation pour faute et les consequences financieres.", "para_idx": None},
                    {"id": 4, "type": "loi", "source": "Droit de la propriete intellectuelle applicable",
                     "issue": "Cession des droits PI sur les livrables non precisee",
                     "severity": "high", "recommendation": "Specifier la cession ou la licence des droits PI sur tous les livrables.", "para_idx": None},
                ]
            elif _jur == "droit_francais":
                compliance = [
                    {"id": 1, "type": "loi", "source": "Code civil - Art. 1231",
                     "issue": "Regime de responsabilite du prestataire insuffisamment encadre",
                     "severity": "high", "recommendation": "Preciser les plafonds de responsabilite conformes au droit francais.", "para_idx": None},
                    {"id": 2, "type": "loi", "source": "RGPD - Reglement UE 2016/679",
                     "issue": "Absence de clause de traitement des donnees a caractere personnel",
                     "severity": "high", "recommendation": "Ajouter DPA ou clause RGPD avec roles responsable/sous-traitant.", "para_idx": None},
                    {"id": 3, "type": "loi", "source": "Code de commerce - Art. L441-10",
                     "issue": "Delai de paiement a verifier (max 30 ou 60 jours selon accord)",
                     "severity": "medium", "recommendation": "Preciser le delai de paiement et les penalites de retard conformes.", "para_idx": None},
                    {"id": 4, "type": "loi", "source": "CPI - Code de la propriete intellectuelle",
                     "issue": "Cession des droits PI sur les livrables non precisee",
                     "severity": "high", "recommendation": "Specifier la cession ou la licence des droits PI (art. L131-3 CPI).", "para_idx": None},
                ]
            elif _jur == "droit_anglais":
                compliance = [
                    {"id": 1, "type": "loi", "source": "Unfair Contract Terms Act 1977",
                     "issue": "Limitation of liability clause requires reasonableness test",
                     "severity": "high", "recommendation": "Ensure liability cap passes the reasonableness test under UCTA 1977.", "para_idx": None},
                    {"id": 2, "type": "loi", "source": "UK GDPR / Data Protection Act 2018",
                     "issue": "No data processing agreement or privacy clause",
                     "severity": "high", "recommendation": "Add DPA with controller/processor roles if personal data is processed.", "para_idx": None},
                    {"id": 3, "type": "loi", "source": "Copyright, Designs and Patents Act 1988",
                     "issue": "IP ownership of deliverables not specified",
                     "severity": "high", "recommendation": "Expressly assign IP rights in deliverables to the client in writing.", "para_idx": None},
                ]
            else:
                compliance = [
                    {"id": 1, "type": "loi", "source": "Droit applicable - Responsabilite contractuelle",
                     "issue": "Plafond de responsabilite absent ou insuffisant",
                     "severity": "high", "recommendation": "Definir un plafond de responsabilite et lister les exclusions selon le droit applicable.", "para_idx": None},
                    {"id": 2, "type": "loi", "source": "Reglementation donnees personnelles applicable",
                     "issue": "Absence de clause sur le traitement des donnees personnelles",
                     "severity": "high", "recommendation": "Ajouter une clause de protection des donnees conforme au droit local.", "para_idx": None},
                    {"id": 3, "type": "loi", "source": "Droit PI applicable",
                     "issue": "Cession des droits PI sur les livrables non precisee",
                     "severity": "high", "recommendation": "Specifier explicitement la cession ou la licence des droits PI.", "para_idx": None},
                ]
        elif any(kw in _ct for kw in ["cdi", "cdd", "travail", "emploi", "salari"]):
            if _jur in ("droit_marocain", "universel", "auto"):
                compliance = [
                    {"id": 1, "type": "loi", "source": "Code du Travail - Periode d'essai (Art.13-14 CT maroc / equivalent)",
                     "issue": "Periode d'essai a verifier selon le droit applicable", "severity": "medium",
                     "recommendation": "Verifier la duree maximale legale et le nombre de renouvellements autorises.", "para_idx": None},
                    {"id": 2, "type": "loi", "source": "Code du Travail - Preavis (Art.43-44 CT maroc / equivalent)",
                     "issue": "Delai de preavis legal a verifier", "severity": "high",
                     "recommendation": "Preavis minimum selon anciennete et categorie du salarie.", "para_idx": None},
                    {"id": 3, "type": "loi", "source": "Code du Travail - Licenciement (Art.63-65 CT maroc / equivalent)",
                     "issue": "Protection contre le licenciement abusif", "severity": "high",
                     "recommendation": "Tout licenciement doit etre justifie et respecter la procedure legale sous peine d'etre abusif.", "para_idx": None},
                ]
            elif _jur == "droit_francais":
                compliance = [
                    {"id": 1, "type": "loi", "source": "Code du travail francais - Periode d'essai",
                     "issue": "Duree de periode d'essai a verifier (max 4 mois cadres)", "severity": "medium",
                     "recommendation": "CDI: 2 mois ouvriers, 3 mois maitrise, 4 mois cadres, renouvelable une fois.", "para_idx": None},
                    {"id": 2, "type": "loi", "source": "Code du travail francais - Licenciement",
                     "issue": "Clause de licenciement a verifier", "severity": "high",
                     "recommendation": "Le licenciement doit etre justifie par une cause reelle et serieuse avec procedure contradictoire.", "para_idx": None},
                    {"id": 3, "type": "loi", "source": "Code du travail francais - Convention collective",
                     "issue": "Convention collective applicable non mentionnee", "severity": "medium",
                     "recommendation": "Identifier et mentionner la convention collective applicable et ses dispositions plus favorables.", "para_idx": None},
                ]
            else:
                compliance = [
                    {"id": 1, "type": "loi", "source": "Code du Travail applicable - Periode d'essai",
                     "issue": "Duree de periode d'essai a verifier selon le droit applicable", "severity": "medium",
                     "recommendation": "Verifier la duree maximale legale de la periode d'essai.", "para_idx": None},
                    {"id": 2, "type": "loi", "source": "Code du Travail applicable - Preavis",
                     "issue": "Delai de preavis legal a verifier", "severity": "high",
                     "recommendation": "Verifier le preavis minimum selon anciennete et categorie.", "para_idx": None},
                    {"id": 3, "type": "loi", "source": "Code du Travail applicable - Licenciement",
                     "issue": "Protection contre le licenciement abusif", "severity": "high",
                     "recommendation": "Verifier la procedure de licenciement et les motifs autorises selon le droit local.", "para_idx": None},
                ]
    result["compliance"] = compliance
    result["_has_legal_context"] = bool(legal_context)
    result["_rag_debug"] = {
        "contract_docs": _rag_contract_count,
        "legal_docs": _rag_legal_count,
        "model_ctx_len": len(model_context),
        "legal_ctx_len": len(legal_context),
        "is_voyage": _rag_is_voyage,
    }
    return result

def fuzzy_match(original, para_text, threshold=0.60):
    """Check if original text roughly matches para_text"""
    original_lower = original.lower().strip()
    para_lower = para_text.lower().strip()
    # Exact match
    if original_lower in para_lower:
        return True
    # Extract meaningful words (ignore short words)
    orig_words = [w for w in re.findall(r"[a-zA-ZÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ-ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¿]{3,}", original_lower)]
    para_words_set = set(re.findall(r"[a-zA-ZÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ-ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¿]{3,}", para_lower))
    orig_words_set = set(orig_words)
    if len(orig_words_set) < 4:
        return False
    overlap = len(orig_words_set & para_words_set) / len(orig_words_set)
    return overlap >= threshold

def create_docx_with_changes(contract_text, modifications, decisions):
    """Fallback DOCX: rapport professionnel avec texte original barre et proposition en vert."""
    from docx import Document as DocxDocument
    from docx.shared import RGBColor, Pt, Cm
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml.ns import qn as _qn
    from docx.oxml import OxmlElement as _OE

    doc = DocxDocument()
    for section in doc.sections:
        section.top_margin    = Cm(2)
        section.bottom_margin = Cm(2)
        section.left_margin   = Cm(2.5)
        section.right_margin  = Cm(2.5)

    title = doc.add_heading("Rapport de modifications — ContractSense", 0)
    title.alignment = WD_ALIGN_PARAGRAPH.CENTER
    dp = doc.add_paragraph()
    dp.alignment = WD_ALIGN_PARAGRAPH.CENTER
    dr = dp.add_run("Genere le " + datetime.datetime.now().strftime("%d/%m/%Y a %H:%M"))
    dr.font.size = Pt(9)
    dr.font.color.rgb = RGBColor(0x70, 0x70, 0x70)

    accepted = [m for m in modifications if decisions.get(str(m["id"])) == "accepted"]
    if not accepted:
        doc.add_paragraph("Aucune modification acceptee.")
        out = io.BytesIO(); doc.save(out); out.seek(0); return out

    doc.add_paragraph()
    sp = doc.add_paragraph()
    sp.add_run(str(len(accepted)) + " clause(s) modifiee(s)").bold = True

    RISK_LABEL = {"high": "Risque eleve", "medium": "Risque modere", "low": "Risque faible"}
    RISK_COLOR = {"high": RGBColor(0xEF,0x44,0x44), "medium": RGBColor(0xF5,0x9E,0x0B), "low": RGBColor(0x10,0xB9,0x81)}

    for i, mod in enumerate(accepted):
        doc.add_paragraph()
        doc.add_heading(str(i+1) + ". " + mod.get("clause_name", "Clause"), level=2)

        risk = mod.get("risk", "")
        if risk:
            rp = doc.add_paragraph()
            rr = rp.add_run("[ " + RISK_LABEL.get(risk, risk) + " ]")
            rr.font.color.rgb = RISK_COLOR.get(risk, RGBColor(0x70,0x70,0x70))
            rr.font.size = Pt(9); rr.bold = True

        reason = mod.get("reason", "")
        if reason:
            rp2 = doc.add_paragraph()
            rr2 = rp2.add_run(reason)
            rr2.font.size = Pt(9)
            rr2.font.color.rgb = RGBColor(0x50,0x50,0x50)
            rr2.italic = True

        pl = doc.add_paragraph()
        rl = pl.add_run("TEXTE ORIGINAL :")
        rl.bold = True; rl.font.size = Pt(9)
        rl.font.color.rgb = RGBColor(0xCC,0x00,0x00)

        po = doc.add_paragraph()
        po.paragraph_format.left_indent = Cm(0.5)
        ro = po.add_run(mod.get("original", ""))
        ro.font.color.rgb = RGBColor(0xCC,0x00,0x00)
        ro.font.strike = True

        pa = doc.add_paragraph("Proposition de modification :")
        pa.runs[0].bold = True
        pa.runs[0].font.size = Pt(9)
        pa.runs[0].font.color.rgb = RGBColor(0x00,0x80,0x00)

        pp = doc.add_paragraph()
        pp.paragraph_format.left_indent = Cm(0.5)
        rp3 = pp.add_run(mod.get("proposed", ""))
        rp3.font.color.rgb = RGBColor(0x00,0x70,0x00)
        rp3.bold = True

        sep = doc.add_paragraph()
        pPr = sep._p.get_or_add_pPr()
        pBdr = _OE("w:pBdr")
        bottom = _OE("w:bottom")
        bottom.set(_qn("w:val"), "single")
        bottom.set(_qn("w:sz"), "4")
        bottom.set(_qn("w:space"), "1")
        bottom.set(_qn("w:color"), "CCCCCC")
        pBdr.append(bottom); pPr.append(pBdr)

    out = io.BytesIO(); doc.save(out); out.seek(0); return out


def apply_track_changes(file_bytes, modifications, decisions):
    doc = Document(io.BytesIO(file_bytes))
    author = "ContractSense"
    date = datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%SZ")
    rev_id = 1

    accepted = [m for m in modifications if decisions.get(str(m["id"])) == "accepted"]
    applied = set()
    paragraphs = list(doc.paragraphs)

    for mod in accepted:
        mod_id = mod.get("id")
        proposed = mod.get("proposed", "").strip()
        if not proposed:
            continue

        para = None

        # Method 1: Use para_idx if available (precise)
        para_idx = mod.get("para_idx")
        if para_idx is not None and para_idx < len(paragraphs):
            candidate = paragraphs[para_idx]
            if candidate.text.strip():
                para = candidate

        # Method 2: Fuzzy match fallback
        if para is None:
            original = mod.get("original", "").strip()
            for p in paragraphs:
                if p.text.strip() and fuzzy_match(original, p.text.strip()):
                    para = p
                    break

        # Handle new clauses (type=nouvelle_clause) ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ insert as new paragraph
        if mod.get('type') == 'nouvelle_clause':
            insertion_after = mod.get('insertion_after')
            insert_para = None
            MIN_INSERT_IDX = 5

            # Find insertion point ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ use insertion_after directly
            if insertion_after is not None:
                safe_idx = max(int(insertion_after), MIN_INSERT_IDX)
                if safe_idx < len(paragraphs):
                    insert_para = paragraphs[safe_idx]

            # Fallback: insert before last paragraph
            if insert_para is None:
                for p in reversed(paragraphs):
                    if p.text.strip() and len(p.text.strip()) > 10:
                        insert_para = p
                        break

            if insert_para is not None:
                # Copy formatting from insert_para run
                ref_rpr = None
                if insert_para.runs:
                    ref_rpr = insert_para.runs[0]._r.find(qn('w:rPr'))

                # Build new paragraph with Track Changes ins
                new_p = OxmlElement('w:p')

                # Copy paragraph properties if available
                if insert_para._p.find(qn('w:pPr')) is not None:
                    import copy
                    new_ppr = copy.deepcopy(insert_para._p.find(qn('w:pPr')))
                    new_p.append(new_ppr)

                ins_elem = OxmlElement('w:ins')
                ins_elem.set(qn('w:id'), str(rev_id))
                ins_elem.set(qn('w:author'), author)
                ins_elem.set(qn('w:date'), date)
                rev_id += 1

                new_r = OxmlElement('w:r')
                # Copy run formatting
                if ref_rpr is not None:
                    import copy
                    new_r.append(copy.deepcopy(ref_rpr))
                new_t = OxmlElement('w:t')
                new_t.text = proposed
                new_t.set('{http://www.w3.org/XML/1998/namespace}space', 'preserve')
                new_r.append(new_t)
                ins_elem.append(new_r)
                new_p.append(ins_elem)

                # Insert AFTER target paragraph
                # addnext inserts before in lxml ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ get next sibling and insert before it
                next_sib = insert_para._p.getnext()
                if next_sib is not None:
                    insert_para._p.getparent().insert(
                        list(insert_para._p.getparent()).index(next_sib),
                        new_p
                    )
                else:
                    insert_para._p.getparent().append(new_p)
                applied.add(mod_id)
                print(f"Inserted new clause '{mod.get('clause_name')}' after para {insertion_after}")
            else:
                print(f"Could not find insertion point for new clause: {mod.get('clause_name')}")
            continue

        if para is None:
            print(f"Could not find paragraph for mod {mod_id}: {mod.get('clause_name')}")
            continue

        para_text = para.text.strip()

        # Clear all runs
        for run in para.runs:
            run.text = ""
        p = para._p

        # Del element
        del_elem = OxmlElement('w:del')
        del_elem.set(qn('w:id'), str(rev_id))
        del_elem.set(qn('w:author'), author)
        del_elem.set(qn('w:date'), date)
        del_run = OxmlElement('w:r')
        del_rpr = OxmlElement('w:rPr')
        del_run.append(del_rpr)
        del_text = OxmlElement('w:delText')
        del_text.set('{http://www.w3.org/XML/1998/namespace}space', 'preserve')
        del_text.text = para_text
        del_run.append(del_text)
        del_elem.append(del_run)
        p.append(del_elem)
        rev_id += 1

        # Ins element
        ins_elem = OxmlElement('w:ins')
        ins_elem.set(qn('w:id'), str(rev_id))
        ins_elem.set(qn('w:author'), author)
        ins_elem.set(qn('w:date'), date)
        ins_run = OxmlElement('w:r')
        ins_text_el = OxmlElement('w:t')
        ins_text_el.set('{http://www.w3.org/XML/1998/namespace}space', 'preserve')
        ins_text_el.text = proposed
        ins_run.append(ins_text_el)
        ins_elem.append(ins_run)
        p.append(ins_elem)
        rev_id += 1

        applied.add(mod_id)

    print(f"Track changes: {len(applied)}/{len(accepted)} applied")
    output = io.BytesIO()
    doc.save(output)
    output.seek(0)
    return output

# ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ Routes ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ
@app.route("/debug-env", methods=["GET"])
def debug_env():
    try:
        test = supa_get("rag_documents", {"select": "id", "limit": "1"})
        supa_status = "OK - " + str(len(test)) + " docs"
    except Exception as e:
        supa_status = "ERROR: " + str(e)
    return jsonify({
        "supabase_url": SUPA_URL[:40],
        "supabase_key_set": bool(SUPA_KEY),
        "supabase_test": supa_status,
        "anthropic_key_set": bool(os.environ.get("ANTHROPIC_API_KEY")),
        "voyage_key_set": bool(os.environ.get("VOYAGE_API_KEY"))
    })


@app.route("/queue/add", methods=["POST", "OPTIONS"])
def queue_add():
    """Ajoute une analyse à la queue admin — stocké en Supabase"""
    if request.method == "OPTIONS":
        return "", 204
    try:
        data = request.get_json() or {}
        doc = {
            "filename": data.get("filename", "Contrat"),
            "contract_type": data.get("contract_type", ""),
            "partie": data.get("partie", ""),
            "accepted_modifications": data.get("accepted_modifications", "[]"),
            "decisions": data.get("decisions", "{}"),
            "submitted_by": data.get("submitted_by", "user"),
            "score": data.get("score", 75),
            "status": "pending"
        }
        supa_insert("analyses_queue", doc)
        return jsonify({"status": "ok"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/rag/suggest", methods=["POST", "OPTIONS"])
def rag_suggest():
    if request.method == "OPTIONS":
        return "", 204
    try:
        filename = request.form.get("source") or request.form.get("filename") or ""
        file_obj = request.files.get("file")
        if not filename and file_obj:
            filename = file_obj.filename or "inconnu"
        if not filename:
            filename = "inconnu"
        category = request.form.get("category", "contract")
        suggested_by = request.form.get("suggested_by", "anonyme")
        file = request.files.get("file")
        content_text = ""
        if file:
            try:
                content_text = file.read().decode("utf-8", errors="ignore")[:50000]
            except:
                content_text = ""
        supa_insert("pending_suggestions", {
            "filename": filename,
            "content": content_text,
            "category": category,
            "suggested_by": suggested_by,
            "status": "pending"
        })
        return jsonify({"status": "ok"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/suggestions/list", methods=["GET"])
def suggestions_list():
    try:
        url = SUPA_URL + "/rest/v1/pending_suggestions?order=submitted_at.desc&limit=100&select=id,filename,category,suggested_by,status,submitted_at"
        headers = {"apikey": SUPA_KEY, "Authorization": f"Bearer {SUPA_KEY}"}
        r = requests.get(url, headers=headers, timeout=10)
        return jsonify({"suggestions": r.json() if r.ok else []})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/suggestions/preview/<suggestion_id>", methods=["GET", "OPTIONS"])
def suggestion_preview(suggestion_id):
    if request.method == "OPTIONS":
        return "", 204
    try:
        url = SUPA_URL + "/rest/v1/pending_suggestions?id=eq." + suggestion_id + "&select=filename,content,category,suggested_by"
        r = requests.get(url, headers=supa_headers(), timeout=15)
        data = r.json()
        if not data:
            return jsonify({"error": "Suggestion non trouvee"}), 404
        s = data[0]
        content = s.get("content", "") or ""
        filename = s.get("filename", "document") or "document"
        # Return as downloadable text
        from flask import Response
        resp = Response(content, mimetype="text/plain; charset=utf-8")
        resp.headers["Content-Disposition"] = "inline; filename=" + filename
        return resp
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/suggestions/approve/<suggestion_id>", methods=["POST", "OPTIONS"])
def suggestion_approve(suggestion_id):
    if request.method == "OPTIONS":
        return "", 204
    try:
        url = SUPA_URL + f"/rest/v1/pending_suggestions?id=eq.{suggestion_id}&select=*"
        headers = {"apikey": SUPA_KEY, "Authorization": f"Bearer {SUPA_KEY}"}
        r = requests.get(url, headers=headers, timeout=10)
        docs = r.json()
        if not docs:
            return jsonify({"error": "Non trouve"}), 404
        doc = docs[0]
        voyage_key = os.environ.get("VOYAGE_API_KEY", "")
        emb = get_embedding((doc.get("content") or "")[:1000], voyage_key)
        rag_doc = {
            "source": doc["filename"],
            "title": doc["filename"],
            "content": doc.get("content", ""),
            "category": doc.get("category", "contract"),
        }
        if emb and len(emb) == 1024:
            rag_doc["embedding_vector"] = "[" + ",".join(str(x) for x in emb) + "]"
        supa_insert("rag_documents", rag_doc)
        supa_update("pending_suggestions", suggestion_id, {"status": "approved"})
        return jsonify({"status": "ok", "message": "Approuve et ajoute au RAG"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
@app.route("/suggestions/reject/<suggestion_id>", methods=["POST", "OPTIONS"])
def suggestion_reject(suggestion_id):
    if request.method == "OPTIONS": return "", 204
    try:
        supa_update("pending_suggestions", suggestion_id, {"status": "rejected"})
        return jsonify({"status": "ok", "message": "Suggestion rejetee"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ===== DIRECTOR SUGGESTIONS (juriste -> directeur -> admin) =====

@app.route("/rag/suggest-to-director", methods=["POST", "OPTIONS"])
def suggest_to_director():
    if request.method == "OPTIONS": return "", 204
    try:
        file_obj = request.files.get("file")
        filename = request.form.get("source", "") or (file_obj.filename if file_obj else "inconnu")
        if not filename or filename == "inconnu":
            filename = file_obj.filename if file_obj else "inconnu"
        category = request.form.get("category", "contract")
        suggested_by = request.form.get("suggested_by", "")
        target_email = request.form.get("target_email", "")
        content_text = ""
        if file_obj:
            try:
                raw = file_obj.read()
                try:
                    import zipfile as zf
                    from docx import Document
                    import io as sio
                    doc_obj = Document(sio.BytesIO(raw))
                    content_text = "\n".join([p.text for p in doc_obj.paragraphs])
                except:
                    content_text = raw.decode("utf-8", errors="replace")
            except: pass
        if not target_email:
            return jsonify({"error": "target_email manquant — le juriste n'est rattaché à aucun directeur"}), 400
        content_text = content_text.replace('\x00', '')  # PostgreSQL rejette les octets nuls
        supa_insert("pending_suggestions_director", {
            "filename": filename,
            "content": content_text[:50000],  # limite sécurité 50k chars
            "category": category,
            "suggested_by": suggested_by,
            "target_director_email": target_email,
            "status": "pending"
        })
        return jsonify({"status": "ok", "message": "Suggestion envoyee au directeur"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/suggestions/list-for-director", methods=["GET", "OPTIONS"])
def suggestions_list_for_director():
    if request.method == "OPTIONS": return "", 204
    try:
        director_email = request.args.get("director_email", "")
        if not director_email:
            return jsonify({"suggestions": []})
        suggestions = supa_get("pending_suggestions_director", {
            "target_director_email": "eq." + director_email,
            "status": "eq.pending",
            "order": "created_at.desc"
        })
        result = []
        for s in (suggestions or []):
            result.append({
                "id": s.get("id"),
                "filename": s.get("filename", "inconnu"),
                "category": s.get("category", ""),
                "suggested_by": s.get("suggested_by", ""),
                "content": s.get("content", ""),
                "status": s.get("status", "pending"),
                "submitted_at": s.get("created_at", "")
            })
        return jsonify({"suggestions": result})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/suggestions/forward-to-admin/<suggestion_id>", methods=["POST", "OPTIONS"])
def forward_suggestion_to_admin(suggestion_id):
    if request.method == "OPTIONS": return "", 204
    try:
        # Get the director suggestion
        rows = supa_get("pending_suggestions_director", {"id": "eq." + suggestion_id})
        if not rows:
            return jsonify({"error": "Suggestion introuvable"}), 404
        s = rows[0]
        # Insert into main admin suggestions queue
        supa_insert("pending_suggestions", {
            "filename": s.get("filename", "inconnu"),
            "content": s.get("content", ""),
            "category": s.get("category", "contract"),
            "suggested_by": s.get("suggested_by", ""),
            "status": "pending"
        })
        # Mark director suggestion as forwarded
        supa_update("pending_suggestions_director", suggestion_id, {"status": "forwarded"})
        return jsonify({"status": "ok", "message": "Suggestion transmise a admin"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/suggestions/reject-director/<suggestion_id>", methods=["POST", "OPTIONS"])
def reject_director_suggestion(suggestion_id):
    if request.method == "OPTIONS": return "", 204
    try:
        supa_update("pending_suggestions_director", suggestion_id, {"status": "rejected"})
        return jsonify({"status": "ok"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/analyses/request-revision/<analysis_id>", methods=["POST", "OPTIONS"])
def request_revision_by_director(analysis_id):
    if request.method == "OPTIONS": return "", 204
    try:
        data = request.get_json() or {}
        modifications = data.get("modifications", [])
        director_notes = (data.get("director_notes") or "").strip()
        modifications = [m for m in modifications if not (isinstance(m, dict) and m.get("_isDirectorNote"))]
        if director_notes:
            modifications = [{"_isDirectorNote": True, "note": director_notes}] + modifications
        patch = {
            "status": "revision_requested",
            "modifications": modifications,
            "director_email": data.get("director_email", ""),
            "director_notes": director_notes
        }
        # Use service role key to bypass RLS for cross-user operations
        _skey = SUPA_SERVICE_KEY or SUPA_KEY
        patch_url = SUPA_URL + f"/rest/v1/analyses?id=eq.{analysis_id}"
        patch_headers = {
            "apikey": _skey,
            "Authorization": "Bearer " + _skey,
            "Content-Type": "application/json",
            "Prefer": "return=representation"
        }
        r = requests.patch(patch_url, headers=patch_headers, json=patch, timeout=10)
        if not r.ok:
            err = r.json() if r.content else {}
            return jsonify({"error": err.get("message", f"Erreur Supabase {r.status_code}")}), 500
        rows = r.json() if r.content else []
        if not rows:
            return jsonify({"error": "Analyse introuvable ou droits insuffisants"}), 403
        return jsonify({"status": "ok", "updated": len(rows)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/analyses/save-director/<analysis_id>", methods=["POST", "OPTIONS"])
def save_director_changes(analysis_id):
    if request.method == "OPTIONS": return "", 204
    try:
        data = request.get_json() or {}
        patch = {
            "modifications": data.get("modifications", []),
            "director_notes": (data.get("director_notes") or "").strip()
        }
        _skey = SUPA_SERVICE_KEY or SUPA_KEY
        patch_url = SUPA_URL + f"/rest/v1/analyses?id=eq.{analysis_id}"
        patch_headers = {
            "apikey": _skey,
            "Authorization": "Bearer " + _skey,
            "Content-Type": "application/json",
            "Prefer": "return=minimal"
        }
        r = requests.patch(patch_url, headers=patch_headers, json=patch, timeout=10)
        if not r.ok:
            err = r.json() if r.content else {}
            return jsonify({"error": err.get("message", f"Erreur Supabase {r.status_code}")}), 500
        return jsonify({"status": "ok"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/analyses/validate-by-director/<analysis_id>", methods=["POST", "OPTIONS"])
def validate_analysis_by_director(analysis_id):
    if request.method == "OPTIONS": return "", 204
    try:
        data = request.get_json() or {}
        patch = {
            "status": "validated",
            "director_email": data.get("director_email", ""),
            "modifications": data.get("modifications", [])
        }
        patch_url = SUPA_URL + f"/rest/v1/analyses?id=eq.{analysis_id}"
        _skey = SUPA_SERVICE_KEY or SUPA_KEY
        patch_headers = {
            "apikey": _skey,
            "Authorization": "Bearer " + _skey,
            "Content-Type": "application/json",
            "Prefer": "return=representation"
        }
        r = requests.patch(patch_url, headers=patch_headers, json=patch, timeout=10)
        if not r.ok:
            err = r.json() if r.content else {}
            return jsonify({"error": err.get("message", f"Erreur Supabase {r.status_code}")}), 500
        rows = r.json() if r.content else []
        if not rows:
            return jsonify({"error": "Analyse introuvable ou droits insuffisants"}), 403
        return jsonify({"status": "ok", "updated": len(rows)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

        return jsonify({"status": "ok", "message": "Suggestion rejetee par le directeur"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ===== ADMIN USER CREATION =====

@app.route("/admin/users", methods=["GET", "OPTIONS"])
def admin_list_users():
    """Return all user_accounts rows (admin only)."""
    if request.method == "OPTIONS": return "", 204
    caller_email = request.args.get("caller_email", "").strip()
    if not caller_email:
        return jsonify({"error": "caller_email requis"}), 400
    caller = supa_get("user_accounts", {"email": f"eq.{caller_email}", "limit": "1"})
    if not caller or not caller[0].get("is_admin"):
        return jsonify({"error": "Accès refusé"}), 403
    key = SUPA_SERVICE_KEY or SUPA_KEY
    url = SUPA_URL + "/rest/v1/user_accounts"
    headers = {
        "apikey": key,
        "Authorization": "Bearer " + key,
        "Accept": "application/json",
        "Prefer": "count=exact"
    }
    r = requests.get(url, headers=headers, params={"select": "*", "order": "created_at.desc"}, timeout=15)
    if not r.ok:
        return jsonify({"error": f"Supabase error {r.status_code}"}), 500
    users = r.json() if r.content else []
    return jsonify({"users": users, "count": len(users)})

@app.route("/admin/activate-user", methods=["POST", "OPTIONS"])
def admin_activate_user():
    """Manually activate or update a user's subscription (admin only)."""
    if request.method == "OPTIONS": return "", 204
    try:
        data = request.get_json() or {}
        caller_email = data.get("caller_email", "").strip()
        target_email = data.get("target_email", "").strip()
        role = data.get("role", "directeur")
        nb_juristes_max = int(data.get("nb_juristes_max", 0))
        analyses = int(data.get("analyses_remaining", 20))
        if not caller_email or not target_email:
            return jsonify({"error": "caller_email et target_email requis"}), 400
        caller = supa_get("user_accounts", {"email": f"eq.{caller_email}", "limit": "1"})
        if not caller or not caller[0].get("is_admin"):
            return jsonify({"error": "Accès refusé"}), 403
        sub_end = (datetime.datetime.now() + datetime.timedelta(days=30)).isoformat()
        upd = {
            "email": target_email, "role": role,
            "payment_status": "active", "analyses_remaining": analyses,
            "subscription_end": sub_end, "nb_juristes_max": nb_juristes_max
        }
        r = supa_upsert("user_accounts", upd, on_conflict="email")
        if not r.ok:
            return jsonify({"error": f"Supabase {r.status_code}: {r.text[:200]}"}), 500
        return jsonify({"status": "ok", "message": f"{target_email} activé avec succès"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/admin/create-user", methods=["POST", "OPTIONS"])
def admin_create_user():
    if request.method == "OPTIONS": return "", 204
    try:
        data = request.get_json() or {}
        email = data.get("email", "").strip()
        password = data.get("password", "").strip()
        role = data.get("role", "directeur")
        parent_email = data.get("parent_email", "")
        if not email or not password:
            return jsonify({"error": "Email et mot de passe requis"}), 400
        if role == "juriste" and not parent_email:
            return jsonify({"error": "Un juriste doit être rattaché à un directeur (parent_email requis)"}), 400
        # Use service_role key to create Auth user
        service_key = SUPA_SERVICE_KEY
        free_reset = (datetime.datetime.now() + datetime.timedelta(days=7)).isoformat()
        if not service_key:
            # Fallback: only insert metadata, warn about Auth
            supa_insert("user_accounts", {
                "email": email, "role": role,
                "parent_email": parent_email if parent_email else None,
                "temp_password": password,
                "analyses_remaining": 3,
                "payment_status": "free",
                "subscription_end": free_reset
            })
            return jsonify({"status": "partial", "message": "Metadata enregistree. Configurez SUPABASE_SERVICE_KEY dans Railway pour creer automatiquement le compte Auth.", "auth_created": False})
        # Create Supabase Auth user via admin API
        auth_url = SUPA_URL.rstrip("/") + "/auth/v1/admin/users"
        auth_headers = {
            "apikey": service_key,
            "Authorization": "Bearer " + service_key,
            "Content-Type": "application/json"
        }
        auth_resp = requests.post(auth_url, headers=auth_headers, json={
            "email": email,
            "password": password,
            "email_confirm": True
        }, timeout=15)
        if not auth_resp.ok:
            err = auth_resp.json()
            return jsonify({"error": "Auth creation failed: " + err.get("message", str(err))}), 400
        auth_user = auth_resp.json()
        # Insert metadata into user_accounts
        supa_insert("user_accounts", {
            "email": email, "role": role,
            "parent_email": parent_email if parent_email else None,
            "temp_password": password,
            "analyses_remaining": 3,
            "payment_status": "free",
            "subscription_end": free_reset
        })
        return jsonify({"status": "ok", "message": "Compte cree avec succes", "auth_created": True, "user_id": auth_user.get("id")})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/health", methods=["GET"])
def health():
    rag = load_rag()
    return jsonify({"status": "ok", "rag_docs": len(rag["documents"])})

@app.route("/app-v2.html", methods=["GET"])
@app.route("/", methods=["GET"])
def serve_frontend():
    return send_file(os.path.join(os.path.dirname(__file__), "static", "app-v2.html"))

@app.route("/identify-parties", methods=["POST", "OPTIONS"])
def identify_parties_route():
    if request.method == "OPTIONS": return "", 204
    try:
        file = request.files.get("file")
        api_key = os.environ.get("ANTHROPIC_API_KEY") or request.form.get("api_key", "")
        lang = request.form.get("lang", "fr")
        if not file:
            return jsonify({"error": "Fichier manquant"}), 400
        contract_text, _, _ = read_file(file)
        if not contract_text or len(contract_text.strip()) < 50:
            return jsonify({"error": "Fichier vide ou illisible"}), 400
        result = identify_parties(contract_text, lang, api_key)
        return jsonify(result)
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
        user_email = request.form.get("user_email", "").strip()

        # Require login
        if not user_email:
            return jsonify({"error": "Connexion requise pour analyser un contrat."}), 401

        # Check analyses_remaining — upsert row if missing (3 free analyses by default)
        rows = supa_get("user_accounts", {"email": f"eq.{user_email}", "select": "analyses_remaining,is_admin", "limit": "1"})
        if not rows:
            # First time user — create free account with 3 analyses
            import datetime as _dt
            reset_date = (_dt.datetime.now() + _dt.timedelta(days=7)).isoformat()
            supa_insert("user_accounts", {
                "email": user_email, "role": "directeur",
                "analyses_remaining": 3, "payment_status": "free",
                "subscription_end": reset_date
            })
            remaining = 3
        else:
            acc = rows[0]
            if acc.get("is_admin"):
                remaining = 9999  # admin = unlimited
            else:
                remaining = acc.get("analyses_remaining", 0) or 0

        if remaining <= 0:
            return jsonify({"error": "Quota d'analyses épuisé. Veuillez renouveler votre abonnement."}), 403

        if not file:
            return jsonify({"error": "Fichier manquant"}), 400
        contract_text, file_bytes, filename = read_file(file)
        if not contract_text or len(contract_text.strip()) < 50:
            return jsonify({"error": "Fichier vide ou illisible"}), 400
        result = analyze_contract(contract_text, lang, contract_type, api_key, partie, file_bytes, filename)

        # Decrement analyses_remaining after successful analysis
        if user_email and remaining is not None:
            supa_patch("user_accounts", {"analyses_remaining": remaining - 1}, f"email=eq.{user_email}")

        # ── Cache en mémoire (toujours disponible dans la session serveur) ───
        file_cache_id = None
        if file_bytes:
            file_cache_id = str(uuid.uuid4())
            _cache_store(file_cache_id, file_bytes)
        result["file_cache_id"] = file_cache_id

        # ── Supabase Storage (persistance longue durée, optionnel) ───────────
        file_storage_path = None
        if file_bytes and SUPA_URL and (SUPA_SERVICE_KEY or SUPA_KEY):
            try:
                ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "docx"
                if ext in ("docx", "pdf", "doc", "txt"):
                    storage_path = str(uuid.uuid4()) + "." + ext
                    ct_map = {
                        "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                        "pdf": "application/pdf",
                        "doc": "application/msword",
                        "txt": "text/plain",
                    }
                    upload_r = supa_storage_upload("contracts", storage_path, file_bytes, ct_map.get(ext, "application/octet-stream"))
                    if upload_r.ok:
                        file_storage_path = storage_path
                    else:
                        print(f"Storage upload failed {upload_r.status_code}: {upload_r.text[:200]}")
            except Exception as _e:
                print(f"Storage upload error: {_e}")
        result["file_storage_path"] = file_storage_path

        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/analyze-clause", methods=["POST", "OPTIONS"])
def analyze_clause():
    if request.method == "OPTIONS": return "", 204
    try:
        data = request.get_json() or {}
        clause_name = (data.get("clause_name") or "").strip()
        clause_text = (data.get("clause_text") or "").strip()
        contract_type = data.get("type", "general")
        partie = data.get("partie", "la partie bénéficiaire")
        if not clause_name:
            return jsonify({"error": "clause_name requis"}), 400

        prompt = f"""Tu es un juriste expert. Analyse la clause suivante extraite d'un contrat de type "{contract_type}".

Nom de la clause : {clause_name}
Texte de la clause :
{clause_text or "(texte non fourni — analyse sur la base du nom uniquement)"}

Réponds UNIQUEMENT avec un objet JSON valide (sans markdown, sans backticks) :
{{
  "original": "texte original de la clause (ou synthèse si non fourni)",
  "proposed": "rédaction améliorée protégeant {partie}",
  "risk": "high|medium|low",
  "reason": "explication concise du risque et de la modification proposée"
}}"""

        client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", ""))
        msg = client.messages.create(
            model="claude-opus-4-6",
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}]
        )
        raw = msg.content[0].text.strip()
        # Nettoyer si markdown
        if raw.startswith("```"):
            raw = re.sub(r"^```[a-z]*\n?", "", raw)
            raw = re.sub(r"\n?```$", "", raw)
        result = json.loads(raw)
        return jsonify(result)
    except json.JSONDecodeError:
        return jsonify({"error": "Réponse IA invalide", "raw": raw[:200]}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/export", methods=["POST"])
def export():
    try:
        file = request.files.get("file")
        file_storage_path = request.form.get("file_storage_path", "").strip()
        file_cache_id = request.form.get("file_cache_id", "").strip()
        modifications = json.loads(request.form.get("modifications", "[]"))
        decisions = json.loads(request.form.get("decisions", "{}"))

        # Strip internal metadata entries before processing
        modifications = [m for m in modifications if not m.get("_isClauseMeta") and not m.get("_isFileMeta")]

        file_bytes = None
        filename = ""

        # 1. Cache mémoire (priorité : même session serveur, 100% fiable)
        if file_cache_id:
            cached = _cache_get(file_cache_id)
            if cached:
                file_bytes = cached
                filename = "contrat.docx"

        # 2. Supabase Storage (persistance longue durée)
        if file_bytes is None and file_storage_path and SUPA_URL and (SUPA_SERVICE_KEY or SUPA_KEY):
            downloaded = supa_storage_download("contracts", file_storage_path)
            if downloaded:
                file_bytes = downloaded
                filename = file_storage_path.rsplit("/", 1)[-1].lower()

        # 3. Fallback : fichier uploadé directement dans la requête
        if file_bytes is None:
            if not file:
                return jsonify({"error": "Fichier manquant"}), 400
            file_bytes = file.read()
            filename = file.filename.lower()

        if filename.endswith(".docx"):
            try:
                output = apply_track_changes(file_bytes, modifications, decisions)
            except Exception as zip_err:
                # File is not a valid DOCX (e.g. text content with .docx extension)
                text_content = file_bytes.decode("utf-8", errors="ignore")
                output = create_docx_with_changes(text_content, modifications, decisions)
        elif filename.endswith(".doc"):
            # Old .doc format ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ extract text then create new DOCX
            doc_text = extract_text_from_docx(file_bytes) or ""
            output = create_docx_with_changes(doc_text, modifications, decisions)
        else:
            doc = Document()
            doc.add_heading('ContractSense - Modifications acceptÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ©es', 0)
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

# ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ Queue: Supabase REST storage ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ
def load_queue():
    try:
        items = supa_get("queue_pending", {"select": "*", "order": "submitted_at", "limit": "200"})
        return {"pending": items or []}
    except Exception as e:
        print("load_queue error: " + str(e))
        return {"pending": []}

def save_queue_item(item):
    try:
        item_copy = dict(item)
        for field in ["key_clauses", "accepted_modifications"]:
            if field in item_copy and not isinstance(item_copy[field], str):
                item_copy[field] = json.dumps(item_copy.get(field, []))
        supa_insert("queue_pending", item_copy)
    except Exception as e:
        print("save_queue_item error: " + str(e))

def delete_queue_item(item_id):
    try:
        supa_delete("queue_pending", {"id": "eq." + item_id})
    except Exception as e:
        print("delete_queue_item error: " + str(e))

@app.route("/rag/contribute", methods=["POST"])
def rag_contribute():
    """Auto-queue full contract with AI scoring for admin validation"""
    try:
        file = request.files.get("file")
        modifications = json.loads(request.form.get("modifications", "[]"))
        decisions = json.loads(request.form.get("decisions", "{}"))
        partie = request.form.get("partie", "")
        contract_type = request.form.get("contract_type", "generic")
        api_key = os.environ.get("ANTHROPIC_API_KEY") or request.form.get("api_key", "")

        if not file:
            return jsonify({"error": "Fichier manquant"}), 400

        contract_text, _, filename = read_file(file)
        accepted = [m for m in modifications if decisions.get(str(m["id"])) == "accepted"]
        rejected = [m for m in modifications if decisions.get(str(m["id"])) == "rejected"]

        # Use user-edited version if available ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ higher quality for RAG
        for m in accepted:
            if m.get("proposed_edited"):
                m["proposed"] = m["proposed_edited"]
                m["user_refined"] = True

        if rejected:
            print("Rejected clauses (" + str(len(rejected)) + "): " + ", ".join([m.get("clause_name","?") for m in rejected]))

        # AI scoring of contract quality for RAG
        client = anthropic.Anthropic(api_key=api_key)
        scoring_prompt = """Evalue ce contrat pour une base de connaissances juridiques.
Reponds UNIQUEMENT en JSON valide, sans markdown:
{
  "score": 0-100,
  "category": "nda|saas|purchase|employment|partnership|service|collaboration|generic",
  "party_label": "favorable """ + (partie if partie else "neutre") + """",
  "quality_reason": "1 phrase expliquant le score",
  "key_clauses": ["clause1", "clause2", "clause3"]
}
Regles:
- category: deduis du CONTENU du contrat, pas du type selectionne par l utilisateur
  * service = contrat de prestation de services, collaboration, mission
  * nda = confidentialite
  * employment = travail, salarie
  * partnership = association, joint-venture
  * purchase = achat, vente
  * saas = logiciel, abonnement
- party_label: utilise un label GENERIQUE selon le role de la partie dans CE contrat
  * service/prestation/collaboration/mission ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ "favorable client" ou "favorable prestataire"
  * travail/salarie ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ "favorable employeur" ou "favorable employe"
  * nda/confidentialite ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ "favorable divulgateur" ou "favorable destinataire"
  * achat/vente ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ "favorable acheteur" ou "favorable vendeur"
  * partenariat/association ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ "favorable partenaire A" ou "favorable partenaire B"
  NE JAMAIS utiliser le nom d une societe ou d une personne dans party_label.
  La partie favorisee dans ce contrat est: """ + (partie if partie else "neutre") + """
- score: 0-100 selon la qualite et completude du contrat
Score eleve = contrat complet avec clauses interessantes a reutiliser."""

        message = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            system=scoring_prompt,
            messages=[{"role": "user", "content": "Contrat:\n\n" + contract_text[:5000]}]
        )
        raw = message.content[0].text
        match = re.search(r'\{[\s\S]*\}', raw)
        scoring = json.loads(match.group(0)) if match else {"score": 50, "category": contract_type, "party_label": f"favorable {partie}", "quality_reason": "Scoring indisponible", "key_clauses": []}

        import uuid
        import uuid as _uuid
        save_queue_item({
            "id": str(_uuid.uuid4()),
            "contract_text": contract_text[:50000],
            "filename": filename,
            "partie": partie,
            "party_label": normalize_party_label(scoring.get("party_label", partie), contract_type),
            "contract_type": contract_type,
            "score": scoring.get("score", 50),
            "category": scoring.get("category", contract_type),
            "quality_reason": scoring.get("quality_reason", ""),
            "key_clauses": scoring.get("key_clauses", []),
            "accepted_count": len(accepted),
            "rejected_count": len(rejected),
            "accepted_modifications": accepted,
            "submitted_at": datetime.datetime.now().isoformat()
        })
        return jsonify({"success": True, "score": scoring.get("score", 50)})

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/queue/list", methods=["GET"])
def queue_list():
    """Liste les analyses en attente de validation admin"""
    try:
        # Try analyses_queue table first
        docs = supa_get("analyses_queue", {
            "select": "id,filename,contract_type,partie,submitted_by,score,status,accepted_modifications,decisions,created_at",
            "status": "eq.pending",
            "order": "created_at.desc",
            "limit": "100"
        })
        if docs is None:
            docs = []
        # Parse modifications
        result = []
        for d in docs:
            try:
                mods = json.loads(d.get("accepted_modifications") or "[]")
            except:
                mods = []
            # Count accepted/rejected
            accepted = [m for m in mods if not isinstance(m, dict) or m.get("decision") != "rejected"]
            rejected_mods = [m for m in mods if isinstance(m, dict) and m.get("decision") == "rejected"]
            result.append({
                "id": d.get("id"),
                "filename": d.get("filename", "Contrat"),
                "contract_type": d.get("contract_type", ""),
                "category": d.get("contract_type", "contract"),
                "partie": d.get("partie", ""),
                "party_label": d.get("partie", ""),
                "submitted_by": d.get("submitted_by", ""),
                "score": d.get("score", 75),
                "quality_reason": d.get("quality_reason", "Analyse automatique"),
                "status": d.get("status", "pending"),
                "accepted_modifications": mods,
                "key_clauses": mods,
                "accepted_count": len(mods),
                "rejected_count": 0,
                "decisions": json.loads(d.get("decisions") or "{}"),
                "submitted_at": d.get("created_at", ""),
                "created_at": d.get("created_at", "")
            })
        return jsonify({"pending": result, "total": len(result)})
    except Exception as e:
        print(f"queue_list error: {e}")
        return jsonify({"pending": [], "total": 0, "error": str(e)})

@app.route("/queue/validate", methods=["POST"])
def queue_validate():
    """Admin validates contract ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ indexes full text into RAG"""
    try:
        body = request.get_json()
        contract_id = body.get("id")
        admin_category = body.get("category", "")
        admin_party_label = body.get("party_label", "")
        voyage_key = os.environ.get("VOYAGE_API_KEY", "")

        queue = load_queue()
        pending = queue.get("pending", [])
        contract = next((c for c in pending if c["id"] == contract_id), None)
        if not contract:
            return jsonify({"error": "Contrat introuvable"}), 404

        contract_text = contract.get("contract_text", "")
        category = admin_category or contract.get("category", "generic")
        party_label = admin_party_label or contract.get("party_label", "")

        # Use admin-edited modifications if provided
        edited_mods = body.get("edited_modifications", [])
        if edited_mods:
            # Merge edited mods back into contract
            edited_map = {m.get("id"): m for m in edited_mods if m.get("proposed")}
            accepted_mods = contract.get("accepted_modifications", [])
            if isinstance(accepted_mods, str):
                accepted_mods = json.loads(accepted_mods)
            for mod in accepted_mods:
                if mod.get("id") in edited_map:
                    mod.update(edited_map[mod["id"]])
            contract["accepted_modifications"] = accepted_mods
        title_base = f"[{category.upper()}] {party_label}"

        # Split contract into chunks and index
        import uuid
        words = contract_text.split()
        chunk_size = 400
        chunks = []
        for i in range(0, len(words), chunk_size):
            chunks.append(" ".join(words[i:i+chunk_size]))

        data = load_rag()
        for i, chunk in enumerate(chunks):
            embedding = get_embedding(chunk, voyage_key)
            title = f"{title_base} (partie {i+1})" if len(chunks) > 1 else title_base
            data["documents"].append({
                "id": str(uuid.uuid4()),
                "title": title,
                "category": category,
                "party_label": party_label,
                "partie": contract.get("partie", ""),
                "contract_type": category,
                "content": chunk,
                "embedding": embedding,
                "source": title_base,
                "key_clauses": contract.get("key_clauses", []),
                "score": contract.get("score", 50),
                "validated_at": datetime.datetime.now().isoformat()
            })

        # Also index accepted modifications as separate entries
        accepted_mods = contract.get("accepted_modifications", [])
        if isinstance(accepted_mods, str):
            accepted_mods = json.loads(accepted_mods)
        for mod in accepted_mods:
            mod_text = "CLAUSE VALIDEE [" + party_label + "]: " + mod.get('clause_name','') + "\n" + mod.get('proposed','')
            embedding = get_embedding(mod_text, voyage_key)
            normalized_label = normalize_party_label(party_label, category)
            save_rag_doc({
                "id": str(uuid.uuid4()),
                "title": "[" + CONTRACT_CATEGORIES.get(category, category.upper()) + "] " + mod.get("clause_name","") + " ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ " + normalized_label,
                "category": "validated_clause",
                "party_label": normalized_label,
                "partie": contract.get("partie", ""),
                "contract_type": category,
                "content": mod_text,
                "embedding": json.dumps(embedding),
                "source": "admin_validated_clause",
                "validated_at": datetime.datetime.now().isoformat()
            })

        delete_queue_item(contract_id)

        return jsonify({"success": True, "chunks_indexed": len(chunks)})

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/queue/reject", methods=["POST"])
def queue_reject():
    """Admin rejects contract ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ removes from queue"""
    try:
        body = request.get_json()
        contract_id = body.get("id")
        delete_queue_item(contract_id)
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/rag/upload", methods=["POST"])
def rag_upload():
    try:
        file = request.files.get("file")
        title = request.form.get("source_name") or request.form.get("title") or (file.filename.rsplit(".",1)[0] if file else "Document")
        category = request.form.get("doc_type") or request.form.get("category", "general")
        jurisdiction_override = request.form.get("jurisdiction", "")
        title_base = title  # Use as source key
        api_key = os.environ.get("ANTHROPIC_API_KEY") or request.form.get("api_key", "")
        if not file:
            return jsonify({"error": "Fichier manquant"}), 400

        file_bytes = file.read()
        filename = file.filename.lower()
        if filename.endswith(".docx") or filename.endswith(".doc"):
            content = extract_text_from_docx(file_bytes)
        else:
            content = file_bytes.decode("utf-8", errors="ignore")

        if not content or len(content.strip()) < 50:
            return jsonify({"error": "Document vide ou illisible"}), 400

        # Limit content size for large documents
        if len(content) > 200000:
            content = content[:200000]

        # Split into chunks of ~400 words
        words = content.split()
        chunk_size = 400
        max_chunks = 50  # Max 50 chunks per upload to avoid timeout
        chunks = []
        for i in range(0, min(len(words), chunk_size * max_chunks), chunk_size):
            chunk = " ".join(words[i:i+chunk_size])
            chunks.append(chunk)

        # Auto-detect jurisdiction from document content (can be overridden by form field)
        doc_jurisdiction = jurisdiction_override or detect_jurisdiction(content, title)
        print(f"RAG upload: jurisdiction={doc_jurisdiction} (override={bool(jurisdiction_override)})")

        import uuid
        voyage_key = os.environ.get("VOYAGE_API_KEY") or request.form.get("voyage_key", "")
        for i, chunk in enumerate(chunks):
            embedding = get_embedding(chunk, voyage_key)
            chunk_title = (title + " (partie " + str(i+1) + ")") if len(chunks) > 1 else title
            save_rag_doc({
                "id": str(uuid.uuid4()),
                "title": chunk_title,
                "category": category,
                "content": chunk,
                "embedding": json.dumps(embedding),
                "source": title,
                "jurisdiction": doc_jurisdiction,
                "validated_at": datetime.datetime.now().isoformat()
            })

        total = load_rag()
        return jsonify({"success": True, "chunks": len(chunks), "source": title, "total_docs": len(total["documents"])})

    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/rag/test", methods=["GET"])
def rag_test():
    """Diagnostic endpoint: tests Supabase connectivity and RAG document availability"""
    import traceback as _tb
    out = {
        "env": {
            "SUPA_URL": (SUPA_URL[:30] + "...") if SUPA_URL else "MISSING",
            "SUPA_KEY": "SET" if SUPA_KEY else "MISSING",
            "SUPA_SERVICE_KEY": "SET" if SUPA_SERVICE_KEY else "MISSING",
            "VOYAGE_KEY": "SET" if os.environ.get("VOYAGE_API_KEY") else "MISSING",
        },
        "steps": []
    }
    key = SUPA_SERVICE_KEY or SUPA_KEY
    try:
        # 1. Fetch all docs (no filter)
        r = requests.get(SUPA_URL + "/rest/v1/rag_documents",
            headers={"apikey": key, "Authorization": "Bearer " + key},
            params={"select": "id,title,category", "limit": "20"}, timeout=10)
        out["steps"].append({"name": "fetch_all", "status": r.status_code,
            "count": len(r.json()) if r.ok else 0,
            "sample": [{"t": d.get("title","?")[:40], "c": d.get("category","?")} for d in (r.json() or [])[:5]] if r.ok else r.text[:200]})
    except Exception as e:
        out["steps"].append({"name": "fetch_all", "error": str(e)})
    for cat in ["contract", "law", "doctrine", "jurisprudence", "general"]:
        try:
            r = requests.get(SUPA_URL + "/rest/v1/rag_documents",
                headers={"apikey": key, "Authorization": "Bearer " + key},
                params={"select": "id,title", "category": "eq." + cat, "limit": "100"}, timeout=10)
            docs = r.json() if r.ok else []
            out["steps"].append({"name": f"cat_{cat}", "status": r.status_code,
                "count": len(docs) if r.ok else 0,
                "titles": [d.get("title","?")[:40] for d in (docs or [])[:3]]})
        except Exception as e:
            out["steps"].append({"name": f"cat_{cat}", "error": str(e)})
    try:
        # pgvector test with dummy embedding
        test_vec = [0.0] * 1024
        pvr = requests.post(SUPA_URL + "/rest/v1/rpc/search_rag",
            headers={"apikey": key, "Authorization": "Bearer " + key, "Content-Type": "application/json"},
            json={"query_embedding": "[" + ",".join(["0.0"]*1024) + "]", "match_count": 3}, timeout=10)
        out["steps"].append({"name": "pgvector_rpc", "status": pvr.status_code,
            "count": len(pvr.json()) if pvr.ok else 0,
            "error": pvr.text[:200] if not pvr.ok else None})
    except Exception as e:
        out["steps"].append({"name": "pgvector_rpc", "error": str(e)})
    return jsonify(out)


@app.route("/rag/list", methods=["GET"])
def rag_list():
    try:
        # Load ALL docs from Supabase with pagination
        all_docs = []
        offset = 0
        while True:
            batch = supa_get("rag_documents", {
                "select": "id,source,category,party_label",
                "limit": "1000",
                "offset": str(offset)
            })
            if not batch:
                break
            all_docs.extend(batch)
            if len(batch) < 1000:
                break
            offset += 1000

        grouped = {}
        for doc in all_docs:
            src = re.sub(r" \(partie \d+/\d+\)$", "", doc.get("source",""))
            src = re.sub(r" ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ partie \d+/\d+$", "", src)
            if src not in grouped:
                grouped[src] = {
                    "source": src,
                    "chunks": 0,
                    "type": doc.get("category",""),
                    "party_label": doc.get("party_label",""),
                    "warning": False
                }
            grouped[src]["chunks"] += 1

        for src, d in grouped.items():
            if d["chunks"] < 5:
                d["warning"] = True
                d["warning_msg"] = "Trop peu de chunks"

        result = sorted(grouped.values(), key=lambda x: (x.get("type",""), x.get("source","")))
        return jsonify({
            "documents": result,
            "total": sum(d["chunks"] for d in result),
            "total_docs": len(result)
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/rag/delete/<doc_id>", methods=["DELETE"])
def rag_delete_by_id(doc_id):
    try:
        sb = get_supabase()
        sb.table("rag_documents").delete().eq("id", doc_id).execute()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/rag/delete", methods=["POST", "DELETE", "OPTIONS"])
def rag_delete():
    if request.method == "OPTIONS":
        return "", 204
    try:
        body = request.get_json() or {}
        source = body.get("source", "")
        count = delete_rag_by_source(source)
        return jsonify({"success": True, "deleted": count})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ── Account info + free tier weekly reset ────────────────────────────────────

@app.route("/account/info", methods=["POST", "OPTIONS"])
def account_info():
    if request.method == "OPTIONS": return "", 204
    data = request.get_json() or {}
    email = data.get("email", "").strip()
    if not email:
        return jsonify({"error": "email requis"}), 400
    rows = supa_get("user_accounts", {"email": f"eq.{email}", "limit": "1"})
    if not rows:
        return jsonify({"error": "compte introuvable"}), 404
    acc = rows[0]

    # Admin → toujours illimité
    if acc.get("is_admin"):
        return jsonify({**acc, "analyses_remaining": -1, "can_analyze": True})

    # Juriste → couvert uniquement par son directeur, pas de free tier
    if acc.get("role") == "juriste":
        parent_email = acc.get("parent_email")
        if not parent_email:
            return jsonify({**acc, "can_analyze": False,
                            "blocked_reason": "no_director",
                            "message": "Votre compte n'est rattaché à aucun directeur."})
        parent = supa_get("user_accounts", {"email": f"eq.{parent_email}", "limit": "1"})
        if not parent:
            return jsonify({**acc, "can_analyze": False,
                            "blocked_reason": "director_not_found"})
        p = parent[0]
        if p.get("payment_status") != "active":
            return jsonify({**acc, "can_analyze": False,
                            "blocked_reason": "director_inactive",
                            "message": "Votre directeur n'a pas d'abonnement actif."})
        sub_end = p.get("subscription_end")
        if sub_end and parse_dt(sub_end) < datetime.datetime.now():
            return jsonify({**acc, "can_analyze": False,
                            "blocked_reason": "director_expired",
                            "message": "L'abonnement de votre directeur a expiré."})
        return jsonify({**acc, "can_analyze": True, "payment_status": "active"})

    # Directeur (solo ou équipe) — abonnement actif → vérifier expiration
    if acc.get("payment_status") == "active":
        sub_end = acc.get("subscription_end")
        if sub_end and parse_dt(sub_end) < datetime.datetime.now():
            reset = (datetime.datetime.now() + datetime.timedelta(days=7)).isoformat()
            supa_patch("user_accounts",
                       {"payment_status": "free", "analyses_remaining": 3, "subscription_end": reset},
                       f"email=eq.{email}")
            acc["payment_status"] = "free"
            acc["analyses_remaining"] = 3
            acc["subscription_end"] = reset
        return jsonify({**acc, "can_analyze": acc.get("analyses_remaining", 0) > 0})

    # Directeur free → reset hebdomadaire auto
    sub_end = acc.get("subscription_end")
    if sub_end and parse_dt(sub_end) < datetime.datetime.now():
        reset = (datetime.datetime.now() + datetime.timedelta(days=7)).isoformat()
        supa_patch("user_accounts",
                   {"analyses_remaining": 3, "subscription_end": reset},
                   f"email=eq.{email}")
        acc["analyses_remaining"] = 3
        acc["subscription_end"] = reset

    rem = acc.get("analyses_remaining", 0) or 0
    return jsonify({**acc, "can_analyze": rem > 0})

# ── CMI Payment ──────────────────────────────────────────────────────────────

def cmi_hash(params, store_key):
    excluded = {"HASH", "encoding"}
    sorted_keys = sorted([k for k in params if k not in excluded], key=lambda x: x.lower())
    s = "|".join(str(params[k]) for k in sorted_keys) + "|" + store_key
    print(f"[CMI DEBUG] fields_order: {sorted_keys}", flush=True)
    for k in sorted_keys:
        print(f"[CMI DEBUG]   {k} = {params[k]}", flush=True)
    print(f"[CMI DEBUG] storekey_len={len(store_key)} storekey_start={store_key[:4]}...", flush=True)
    result = base64.b64encode(hashlib.sha512(s.encode("utf-8")).digest()).decode()
    print(f"[CMI DEBUG] HASH: {result}", flush=True)
    return result

@app.route("/payment/initiate", methods=["POST", "OPTIONS"])
def payment_initiate():
    if request.method == "OPTIONS": return "", 204
    data = request.get_json() or {}
    director_email = data.get("director_email", "")
    nb_users = int(data.get("nb_users", 1))
    role = data.get("role", "directeur")  # "juriste" = 950 DH solo, "directeur" = 850 DH/user
    price = 950 if role == "juriste" else 850
    total = nb_users * price
    order_id = f"WF-{datetime.datetime.now().strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:8].upper()}"

    supa_insert("payments", {
        "director_email": director_email, "order_id": order_id,
        "amount": total, "nb_users": nb_users, "status": "pending"
    })

    params = {
        "clientid":      CMI_CLIENT_ID,
        "storetype":     "3D_PAY_HOSTING",
        "trantype":      "PreAuth",
        "amount":        f"{total:.2f}",
        "currency":      "504",
        "oid":           order_id,
        "okUrl":         f"{APP_URL}/app-v2.html?payment=success",
        "failUrl":       f"{APP_URL}/app-v2.html?payment=failed",
        "shopurl":       APP_URL,
        "callbackUrl":   "https://web-production-f96f7.up.railway.app/payment/callback",
        "lang":          "fr",
        "rnd":           datetime.datetime.now().strftime("%Y%m%d%H%M%S"),
        "hashAlgorithm": "ver3",
        "encoding":      "UTF-8",
        "email":         director_email,
    }
    params["HASH"] = cmi_hash(params, CMI_STORE_KEY)
    return jsonify({"form_url": CMI_PAYMENT_URL, "params": params, "total": total})

@app.route("/payment/callback", methods=["POST"])
def payment_callback():
    data = request.form.to_dict()
    order_id = data.get("oid", "")
    if data.get("ProcReturnCode") == "00":
        supa_patch("payments", {"status": "success", "paid_at": datetime.datetime.now().isoformat()},
                   f"order_id=eq.{order_id}")
        payments = supa_get("payments", {"order_id": f"eq.{order_id}", "limit": "1"})
        if payments:
            p = payments[0]
            sub_end = (datetime.datetime.now() + datetime.timedelta(days=30)).isoformat()
            nb_users = p.get("nb_users", 1)
            nb_juristes_max = max(0, nb_users - 1)  # nb_users includes director
            upd_dir = {
                "email": p["director_email"],
                "role": "directeur",
                "payment_status": "active", "analyses_remaining": 20,
                "subscription_end": sub_end, "nb_juristes_max": nb_juristes_max
            }
            upd_jur = {"payment_status": "active", "analyses_remaining": 20, "subscription_end": sub_end}
            # Use upsert so the row is created even if the director never used /analyze before
            supa_upsert("user_accounts", upd_dir, on_conflict="email")
            juristes = supa_get("user_accounts", {"parent_email": f"eq.{p['director_email']}", "select": "email"}) or []
            for j in juristes:
                supa_patch("user_accounts", upd_jur, f"email=eq.{j['email']}")
    else:
        supa_patch("payments", {"status": "failed"}, f"order_id=eq.{order_id}")
    return "APPROVED", 200


@app.route("/director/create-juriste", methods=["POST", "OPTIONS"])
def director_create_juriste():
    if request.method == "OPTIONS": return "", 204
    data = request.get_json() or {}
    director_email = data.get("director_email", "").strip()
    juriste_email  = data.get("juriste_email", "").strip()
    juriste_password = data.get("juriste_password", "").strip()

    if not director_email or not juriste_email or not juriste_password:
        return jsonify({"error": "Champs requis manquants"}), 400

    # Check director exists and has slots available
    rows = supa_get("user_accounts", {"email": f"eq.{director_email}", "limit": "1"})
    if not rows:
        return jsonify({"error": "Directeur introuvable"}), 404
    director = rows[0]
    is_admin = director.get("role") == "admin"

    if not is_admin and director.get("payment_status") != "active":
        return jsonify({"error": "Abonnement inactif — souscrivez d'abord un abonnement"}), 403

    if not is_admin:
        nb_juristes_max = director.get("nb_juristes_max", 0) or 0
        existing = supa_get("user_accounts", {"parent_email": f"eq.{director_email}", "select": "id"}) or []
        if len(existing) >= nb_juristes_max:
            return jsonify({
                "error": f"Quota atteint : votre abonnement inclut {nb_juristes_max} juriste(s). Modifiez votre abonnement pour en ajouter."
            }), 403

    # Create Supabase auth user via admin API
    # Si l'utilisateur existe déjà dans Auth, on met juste à jour son mot de passe
    try:
        r = requests.post(
            SUPA_URL + "/auth/v1/admin/users",
            headers={"apikey": SUPA_SERVICE_KEY, "Authorization": f"Bearer {SUPA_SERVICE_KEY}", "Content-Type": "application/json"},
            json={"email": juriste_email, "password": juriste_password, "email_confirm": True},
            timeout=15
        )
        if not r.ok:
            err_text = r.text.lower()
            if any(x in err_text for x in ["already registered", "already exists", "user already", "email_exists"]):
                # Trouver l'UUID et mettre à jour le mot de passe
                list_r = requests.get(
                    SUPA_URL + "/auth/v1/admin/users",
                    headers={"apikey": SUPA_SERVICE_KEY, "Authorization": f"Bearer {SUPA_SERVICE_KEY}"},
                    params={"filter": f"email=={juriste_email}", "per_page": "1000"},
                    timeout=15
                )
                if list_r.ok:
                    for u in (list_r.json().get("users") or []):
                        if u.get("email") == juriste_email:
                            requests.put(
                                SUPA_URL + f"/auth/v1/admin/users/{u['id']}",
                                headers={"apikey": SUPA_SERVICE_KEY, "Authorization": f"Bearer {SUPA_SERVICE_KEY}", "Content-Type": "application/json"},
                                json={"password": juriste_password},
                                timeout=15
                            )
                            break
            else:
                return jsonify({"error": "Erreur création compte auth: " + r.text[:200]}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    # Upsert user_accounts row (supprimer l'ancienne si elle existe, puis réinsérer)
    existing_row = supa_get("user_accounts", {"email": f"eq.{juriste_email}", "limit": "1"})
    if existing_row:
        supa_patch("user_accounts", {
            "role": "juriste", "parent_email": director_email,
            "payment_status": "active", "analyses_remaining": 20,
            "subscription_end": director.get("subscription_end", "")
        }, f"email=eq.{juriste_email}")
    else:
        supa_insert("user_accounts", {
            "email": juriste_email, "role": "juriste",
            "parent_email": director_email,
            "payment_status": "active",
            "analyses_remaining": 20,
            "subscription_end": director.get("subscription_end", "")
        })

    # Envoyer email de bienvenue avec identifiants
    app_url = os.environ.get("APP_URL", "https://contractsense.fr")
    send_email(
        to=juriste_email,
        subject="Votre accès ContractSense",
        html=f"""
<div style="font-family:Arial,sans-serif;max-width:520px;margin:auto;padding:32px;background:#f9fafb;border-radius:12px">
  <h2 style="color:#1e293b;margin-bottom:8px">Bienvenue sur ContractSense</h2>
  <p style="color:#475569">Votre directeur vous a ajouté à son équipe. Voici vos identifiants de connexion :</p>
  <div style="background:#fff;border-radius:8px;padding:20px;margin:20px 0;border:1px solid #e2e8f0">
    <p style="margin:0 0 8px 0"><strong>Email :</strong> {juriste_email}</p>
    <p style="margin:0"><strong>Mot de passe :</strong> {juriste_password}</p>
  </div>
  <a href="{app_url}" style="display:inline-block;background:linear-gradient(135deg,#5b7cfa,#8b5cf6);color:#fff;padding:12px 24px;border-radius:8px;text-decoration:none;font-weight:700">
    Accéder à ContractSense
  </a>
  <p style="color:#94a3b8;font-size:12px;margin-top:24px">Pensez à changer votre mot de passe après votre première connexion.</p>
</div>
"""
    )

    return jsonify({"status": "ok", "message": f"Compte juriste {juriste_email} créé avec succès"})


@app.route("/director/delete-juriste", methods=["POST", "OPTIONS"])
def director_delete_juriste():
    if request.method == "OPTIONS": return "", 204
    data = request.get_json() or {}
    director_email = data.get("director_email", "").strip()
    juriste_email  = data.get("juriste_email", "").strip()
    if not director_email or not juriste_email:
        return jsonify({"error": "Champs requis manquants"}), 400

    # Vérifier que le juriste appartient bien à ce directeur
    rows = supa_get("user_accounts", {"email": f"eq.{juriste_email}", "limit": "1"})
    if not rows:
        return jsonify({"error": "Juriste introuvable"}), 404
    juriste = rows[0]
    if juriste.get("parent_email") != director_email:
        return jsonify({"error": "Ce juriste n'appartient pas à votre équipe"}), 403

    # Supprimer de Supabase Auth — chercher dans toutes les pages
    auth_headers = {"apikey": SUPA_SERVICE_KEY, "Authorization": f"Bearer {SUPA_SERVICE_KEY}"}
    deleted_auth = False
    for page in range(1, 20):
        list_r = requests.get(
            SUPA_URL + "/auth/v1/admin/users",
            headers=auth_headers,
            params={"page": page, "per_page": "1000"},
            timeout=15
        )
        if not list_r.ok:
            break
        users = list_r.json().get("users") or []
        for u in users:
            if u.get("email") == juriste_email:
                requests.delete(
                    SUPA_URL + f"/auth/v1/admin/users/{u['id']}",
                    headers=auth_headers, timeout=15
                )
                deleted_auth = True
                break
        if deleted_auth or len(users) < 1000:
            break

    # Supprimer de user_accounts
    requests.delete(
        SUPA_URL + f"/rest/v1/user_accounts?email=eq.{juriste_email}",
        headers={**supa_headers(), "apikey": SUPA_SERVICE_KEY, "Authorization": f"Bearer {SUPA_SERVICE_KEY}"},
        timeout=10
    )

    return jsonify({"status": "ok", "message": f"Juriste {juriste_email} supprimé"})


def _init_storage():
    """Crée le bucket Supabase Storage au démarrage si inexistant."""
    if not SUPA_URL or not (SUPA_SERVICE_KEY or SUPA_KEY):
        return
    try:
        r = supa_storage_ensure_bucket("contracts")
        if r.ok:
            print("Storage bucket 'contracts' pret.")
        elif "already exists" in r.text.lower() or r.status_code == 409:
            print("Storage bucket 'contracts' deja existant.")
        else:
            print(f"Storage bucket init: {r.status_code} {r.text[:100]}")
    except Exception as e:
        print(f"Storage bucket init error: {e}")

_init_storage()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, timeout=120)
