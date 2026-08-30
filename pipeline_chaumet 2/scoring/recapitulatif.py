"""Récapitulatif de toutes les tentatives d'extraction, une ligne par item.

Les manifestes sont organisés par exécution ; ce module les retourne pour
produire une vue par fiche : quelle passe, quand, combien de temps, avec quel
résultat et pour quelle raison en cas d'échec. C'est la forme qui permet de
retrouver l'histoire d'une cote particulière, ou de croiser les échecs avec les
caractéristiques des documents.

Si `journal_terminal.txt` a été produit (voir scoring/journal.py), le nombre de
réessais y est ajouté — les manifestes ne le consignent pas.

Usage :
    python scoring/recapitulatif.py
"""
from __future__ import annotations

import collections
import csv
import glob
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "pipeline"))
import config  # noqa: E402
from journal import famille  # taxonomie des causes, partagée  # noqa: E402

MODELES = {"gemini-3-flash-preview": "Flash 3",
           "gemini-3.1-pro-preview": "Pro 3.1",
           "gemini-3-pro-preview": "Pro 3 (retiré)"}


def reessais_depuis_journal() -> dict[tuple, int]:
    """Nombre de réessais par (cote, série, rang d'exécution), si disponible."""
    fichier = config.BASE_DIR / "verification_croisee" / "journal_fiches.csv"
    if not fichier.exists():
        return {}
    with fichier.open(encoding="utf-8-sig") as f:
        return {(r["cote_archive"], r["repetition"], r["issue"]): int(r["reessais"])
                for r in csv.DictReader(f)}


def collecter() -> list[dict]:
    reessais = reessais_depuis_journal()
    lignes = []
    for manifeste in sorted(glob.glob(str(config.OUTPUTS_DIR / "*/run.json")) +
                            glob.glob(str(config.OUTPUTS_DIR / "*/rep_*/run.json"))):
        dossier = Path(manifeste).parent
        passe = dossier.parent.name if dossier.name.startswith("rep_") else dossier.name
        if passe not in config.CONFIGS:
            continue
        serie = dossier.name if dossier.name.startswith("rep_") else "référence"
        h = json.loads(Path(manifeste).read_text(encoding="utf-8"))

        for rang, r in enumerate(h["runs"], 1):
            commun = {
                "passe": passe,
                "modele": MODELES.get(r["modele"], r["modele"]),
                "serie": serie,
                "execution": rang,
                "horodatage": r["horodatage_debut"],
                "prompt_sha256": r["prompt"]["sha256"][:12],
            }
            for cote in r["reussies"]:
                lignes.append({**commun, "cote_archive": cote, "issue": "réussite",
                               "duree_s": r["duree_par_fiche_s"].get(cote, ""),
                               "reessais": reessais.get((cote, serie, "réussite"), ""),
                               "cause": "", "message": ""})
            for e in r["echouees"]:
                msg = " ".join(e["erreur"].split())
                lignes.append({**commun, "cote_archive": e["cote_archive"], "issue": "échec",
                               "duree_s": "",
                               "reessais": reessais.get((e["cote_archive"], serie, "échec"), ""),
                               "cause": famille(msg), "message": msg[:220]})

    ordre = ["cote_archive", "passe", "modele", "serie", "execution", "horodatage",
             "duree_s", "issue", "reessais", "cause", "message", "prompt_sha256"]
    lignes.sort(key=lambda x: (x["cote_archive"], x["passe"], x["serie"], x["execution"]))
    return [{c: l[c] for c in ordre} for l in lignes]


def main() -> None:
    lignes = collecter()
    if not lignes:
        raise SystemExit("Aucun manifeste exploitable.")
    sortie = config.BASE_DIR / "verification_croisee" / "recapitulatif_fiches.csv"
    sortie.parent.mkdir(parents=True, exist_ok=True)
    with sortie.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(lignes[0]))
        w.writeheader(); w.writerows(lignes)

    ok = sum(1 for l in lignes if l["issue"] == "réussite")
    print(f"── Récapitulatif : {len(lignes)} tentatives ──")
    print(f"   {ok} réussites, {len(lignes) - ok} échecs\n")
    print(f"   {'modèle':16}{'tentatives':>11}{'réussites':>11}{'échecs':>8}{'taux':>8}")
    par_modele = collections.defaultdict(list)
    for l in lignes:
        par_modele[l["modele"]].append(l)
    for mod, grp in sorted(par_modele.items()):
        r = sum(1 for l in grp if l["issue"] == "réussite")
        print(f"   {mod:16}{len(grp):>11}{r:>11}{len(grp)-r:>8}{r/len(grp):>8.1%}")
    print("\n   CAUSES D'ÉCHEC")
    for c, n in collections.Counter(l["cause"] for l in lignes if l["cause"]).most_common():
        print(f"     {c:44}{n:>3}")
    print(f"\n   Écrit dans {sortie}")


if __name__ == "__main__":
    main()
