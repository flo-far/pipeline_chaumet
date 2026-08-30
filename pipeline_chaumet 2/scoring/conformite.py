"""Mesure le respect des consignes du prompt par le modèle, sans rien rejeter.

Le principe est celui retenu en §3.5 : le prompt prescrit une convention, un
contrôle vérifie a posteriori qu'elle a été suivie. Une sortie non conforme
n'est jamais écartée — elle serait perdue pour la mesure, et une extraction
correcte à 95 % vaut mieux qu'un échec. Le taux de conformité constitue une
variable du benchmark à part entière : les enrichissements des passes 2 et
suivantes sont censés l'améliorer.

Une non-conformité et l'erreur de scoring qu'elle entraîne portent sur le même
fait : les deux indicateurs se lisent côte à côte, jamais additionnés.

Usage :
    python scoring/conformite.py --config passe_1
    python scoring/conformite.py --source verite-terrain   # doit afficher 100 %
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "pipeline"))
import config  # noqa: E402
from metriques import chemin_metriques, fusionner  # noqa: E402
from models import COTE_ARCHIVE_PATTERN  # noqa: E402

TYPES_AUTORISES = {"Métal", "Pierre", "Pierre précieuse", "Divers"}
UNITES_AUTORISEES = {"g", "carat", None}
UNITE_ATTENDUE = {"Métal": "g", "Pierre précieuse": "carat", "Pierre": None}


@dataclass
class Regle:
    code: str
    libelle: str
    verifier: Callable[[dict], list[str]]
    extraction_seulement: bool = False


def _materiaux(fiche: dict) -> list[dict]:
    return [m for m in fiche.get("materiaux") or [] if isinstance(m, dict)]


def _r_type_fiche(f: dict) -> list[str]:
    v = (f.get("metadata") or {}).get("type_fiche")
    return [] if isinstance(v, int) else [f"type_fiche = {v!r}"]


def _r_photographie(f: dict) -> list[str]:
    v = (f.get("metadata") or {}).get("photographie")
    return [] if isinstance(v, bool) else [f"photographie = {v!r}"]


def _r_type_materiau(f: dict) -> list[str]:
    return [
        f"materiaux[{i}].type = {m.get('type')!r}"
        for i, m in enumerate(_materiaux(f))
        if m.get("type") not in TYPES_AUTORISES
    ]


def _r_unite_valeur(f: dict) -> list[str]:
    return [
        f"materiaux[{i}].unite = {m.get('unite')!r}"
        for i, m in enumerate(_materiaux(f))
        if m.get("unite") not in UNITES_AUTORISEES
    ]


def _r_unite_coherente(f: dict) -> list[str]:
    anomalies = []
    for i, m in enumerate(_materiaux(f)):
        attendue = UNITE_ATTENDUE.get(m.get("type"), "__libre__")
        if attendue != "__libre__" and m.get("unite") != attendue:
            anomalies.append(
                f"materiaux[{i}] : type {m.get('type')!r} → unite attendue "
                f"{attendue!r}, obtenue {m.get('unite')!r}"
            )
    return anomalies


def _dates_normalisees(f: dict):
    dates = f.get("dates") or {}
    yield "dates.date_entree_stock", (dates.get("date_entree_stock") or {}).get("valeur_normalisee")
    for i, d in enumerate(dates.get("dates_alternatives") or []):
        yield f"dates.dates_alternatives[{i}]", (d or {}).get("valeur_normalisee")
    vente = (f.get("vente") or {}).get("date_vente") or {}
    yield "vente.date_vente", vente.get("valeur_normalisee")
    for i, m in enumerate(_materiaux(f)):
        yield f"materiaux[{i}]", m.get("date_normalisee")


def _r_date_reelle(f: dict) -> list[str]:
    anomalies = []
    for chemin, valeur in _dates_normalisees(f):
        if valeur is None:
            continue
        try:
            dt.date.fromisoformat(str(valeur))
        except ValueError:
            anomalies.append(f"{chemin}.valeur_normalisee = {valeur!r} n'est pas une date")
    return anomalies


def _confiances(f: dict):
    def parcourir(obj, chemin=""):
        if isinstance(obj, dict):
            if "confiance" in obj:
                porte_une_valeur = any(
                    v is not None for c, v in obj.items() if c != "confiance"
                )
                yield chemin or "racine", obj["confiance"], porte_une_valeur
            for cle, val in obj.items():
                if cle not in ("confiance", "metadata"):
                    yield from parcourir(val, f"{chemin}.{cle}" if chemin else cle)
        elif isinstance(obj, list):
            for i, val in enumerate(obj):
                yield from parcourir(val, f"{chemin}[{i}]")

    return parcourir(f)


def _r_confiance_numerique(f: dict) -> list[str]:
    """Le prompt autorise « un score de confiance bas (ou null) » sur un champ
    absent : une confiance nulle n'est donc une anomalie que si le bloc porte
    effectivement une valeur."""
    anomalies = []
    for chemin, valeur, renseigne in _confiances(f):
        if valeur is None:
            if renseigne:
                anomalies.append(f"{chemin}.confiance non renseignée alors que le bloc l'est")
        elif isinstance(valeur, bool) or not isinstance(valeur, (int, float)):
            anomalies.append(f"{chemin}.confiance = {valeur!r} (attendu un nombre)")
        elif not 0.0 <= float(valeur) <= 1.0:
            anomalies.append(f"{chemin}.confiance = {valeur!r} hors de [0, 1]")
    return anomalies


def _r_matiere_renseignee(f: dict) -> list[str]:
    return [
        f"materiaux[{i}] : libelle {m.get('libelle')!r} sans matiere_normalisee"
        for i, m in enumerate(_materiaux(f))
        if m.get("libelle") and not m.get("matiere_normalisee")
    ]


REGLES: list[Regle] = [
    Regle("type_fiche", "metadata.type_fiche déterminé (entier)", _r_type_fiche),
    Regle("photographie", "metadata.photographie déterminé (booléen)", _r_photographie),
    Regle("type_materiau", "materiaux[].type parmi les quatre valeurs", _r_type_materiau),
    Regle("unite_valeur", "materiaux[].unite parmi g / carat / null", _r_unite_valeur),
    Regle("unite_coherente", "unite déduite du type (§3.5)", _r_unite_coherente),
    Regle("date_reelle", "dates normalisées réellement valides", _r_date_reelle),
    Regle("matiere_renseignee", "matiere_normalisee présente si libelle présent", _r_matiere_renseignee),
    Regle(
        "confiance",
        "confiance numérique renseignée dans [0, 1]",
        _r_confiance_numerique,
        extraction_seulement=True,
    ),
]


def charger(source: str, config_name: str) -> list[tuple[str, dict]]:
    """Charge les fiches d'un dossier, à l'exclusion des fichiers de service.

    Le dossier de sortie contient aussi `run.json` (le manifeste de passe) :
    n'y retenir que les fichiers dont le nom est une cote d'archive, faute de
    quoi le manifeste serait contrôlé comme s'il s'agissait d'une fiche.
    """
    dossier = (
        config.FICHES_DIR
        if source == "verite-terrain"
        else config.CONFIGS[config_name]["output_dir"]
    )
    return [
        (p.stem, json.loads(p.read_text(encoding="utf-8")))
        for p in sorted(dossier.glob("*.json"))
        if re.fullmatch(COTE_ARCHIVE_PATTERN, p.stem)
    ]


def rapport(source: str, config_name: str) -> None:
    fiches = charger(source, config_name)
    if not fiches:
        raise SystemExit("Aucune fiche à contrôler.")
    regles = [r for r in REGLES if source != "verite-terrain" or not r.extraction_seulement]

    print(f"── Conformité aux consignes ({source}) ──")
    print(f"Fiches contrôlées : {len(fiches)}\n")
    print(f"{'règle':22} {'fiches conformes':18} {'anomalies':>10}   libellé")

    detail = []
    mesures = {}
    for regle in regles:
        conformes = 0
        total_anomalies = 0
        for cote, fiche in fiches:
            anomalies = regle.verifier(fiche)
            conformes += not anomalies
            total_anomalies += len(anomalies)
            detail += [{"cote_archive": cote, "regle": regle.code, "anomalie": a} for a in anomalies]
        taux = conformes / len(fiches)
        mesures[regle.code] = {
            "libelle": regle.libelle,
            "fiches_conformes": conformes,
            "fiches": len(fiches),
            "taux": round(taux, 4),
            "anomalies": total_anomalies,
        }
        marque = "  " if taux == 1 else "!!"
        print(f"{marque}{regle.code:20} {conformes:>4}/{len(fiches)} ({taux:5.1%}) "
              f"{total_anomalies:>10}   {regle.libelle}")

    cible = fusionner(
        chemin_metriques(config.BASE_DIR, config_name),
        "conformite",
        {"source": source, "fiches": len(fiches), "regles": mesures},
        config_name,
    )

    if not detail:
        print("\nAucune anomalie : toutes les consignes contrôlables sont respectées.")
        print(f"Métriques agrégées écrites dans {cible}")
        return

    print(f"\n{len(detail)} anomalies au total. Premiers cas :")
    for d in detail[:10]:
        print(f"  {d['cote_archive']}  [{d['regle']}]  {d['anomalie']}")

    suffixe = "verite_terrain" if source == "verite-terrain" else config_name
    out = config.BASE_DIR / "verification_croisee" / f"conformite_{suffixe}.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    import csv

    with out.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=["cote_archive", "regle", "anomalie"])
        writer.writeheader()
        writer.writerows(detail)
    print(f"\nDétail complet écrit dans {out}")
    print(f"Métriques agrégées écrites dans {cible}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="passe_1")
    parser.add_argument(
        "--source",
        default="extraction",
        choices=("extraction", "verite-terrain"),
        help="Contrôler les sorties d'une passe, ou la vérité terrain elle-même",
    )
    args = parser.parse_args()
    rapport(args.source, args.config)


if __name__ == "__main__":
    main()
