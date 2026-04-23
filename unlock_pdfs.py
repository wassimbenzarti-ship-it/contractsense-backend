"""
unlock_pdfs.py — Déverrouille tous les PDFs protégés d'un dossier.

Usage:
  pip install pikepdf
  python unlock_pdfs.py --folder ./mes_pdfs/
  python unlock_pdfs.py --file codeCommerce.pdf

Les fichiers déverrouillés sont créés à côté avec le suffixe _unlocked.pdf.
Les fichiers déjà lisibles sont ignorés.
"""

import sys
from pathlib import Path


def unlock_pdf(src: Path) -> bool:
    """
    Tente de retirer la protection d'un PDF.
    Retourne True si succès, False si le fichier nécessite un mot de passe.
    """
    try:
        import pikepdf
    except ImportError:
        print("ERREUR: pikepdf non installé. Lance: pip install pikepdf")
        sys.exit(1)

    dest = src.with_name(src.stem + "_unlocked.pdf")

    try:
        # password="" fonctionne pour les PDFs "protégés sans mot de passe"
        # (restrictions copier/imprimer mais ouverture libre — cas LexisMA)
        with pikepdf.open(src, password="") as pdf:
            pdf.save(dest)
        print(f"  ✓ {src.name} → {dest.name}")
        return True
    except pikepdf.PasswordError:
        print(f"  ✗ {src.name} — mot de passe requis, impossible de déverrouiller automatiquement")
        return False
    except Exception as e:
        print(f"  ✗ {src.name} — erreur: {e}")
        return False


def main():
    args = sys.argv[1:]
    if not args or "--help" in args:
        print(__doc__)
        return

    files = []
    i = 0
    while i < len(args):
        if args[i] == "--folder" and i + 1 < len(args):
            folder = Path(args[i + 1])
            files = sorted(folder.rglob("*.pdf"))
            print(f"{len(files)} PDF(s) trouvé(s) dans {folder}")
            i += 2
        elif args[i] == "--file" and i + 1 < len(args):
            files = [Path(args[i + 1])]
            i += 2
        else:
            i += 1

    if not files:
        print("Aucun fichier à traiter.")
        return

    ok, fail = 0, 0
    for f in files:
        if f.name.endswith("_unlocked.pdf"):
            continue  # déjà traité
        if unlock_pdf(f):
            ok += 1
        else:
            fail += 1

    print(f"\n{'='*40}")
    print(f"Déverrouillés : {ok}")
    print(f"Échecs        : {fail}")
    if ok:
        print(f"\nRe-upload avec:")
        print(f"  python upload_rag.py --folder {files[0].parent} --jurisdiction droit_marocain")


if __name__ == "__main__":
    main()
