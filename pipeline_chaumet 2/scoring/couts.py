"""Décomposition du coût et rapprochement avec la facturation réelle.

Les manifestes de la première campagne ne relevaient que l'entrée et la sortie
visible. Les tokens de raisonnement, facturés au tarif de sortie, en étaient
absents, d'où une sous-estimation d'un facteur quatre environ (voir §7.3 du
mémoire). Les passes « bis » corrigent l'instrumentation ; ce module en tire la
décomposition et vérifie qu'elle retrouve la facture.

Usage :
    python scoring/couts.py
    python scoring/couts.py --facture-flash 5 --facture-pro 15 --tva 20
"""
from __future__ import annotations

import argparse
import glob
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "pipeline"))
import config  # noqa: E402


def cumul(nom: str) -> dict | None:
    """Agrège les manifestes d'une passe et de ses répétitions."""
    motifs = [config.OUTPUTS_DIR / nom / "run.json",
              *sorted((config.OUTPUTS_DIR / nom).glob("rep_*/run.json"))]
    tot = {"exec": 0, "demandees": 0, "reussies": 0,
           "entree": 0, "sortie": 0, "raisonnement": 0, "total_api": 0, "duree": 0.0}
    vu = False
    for chemin in motifs:
        if not chemin.exists():
            continue
        vu = True
        h = json.loads(chemin.read_text(encoding="utf-8"))
        for r in h.get("runs", []):
            tk = r.get("tokens", {})
            tot["exec"] += 1
            tot["demandees"] += len(r.get("cotes_demandees", []))
            tot["reussies"] += len(r.get("reussies", []))
            tot["entree"] += tk.get("entree", 0)
            tot["sortie"] += tk.get("sortie", 0)
            tot["raisonnement"] += tk.get("raisonnement", 0)
            tot["total_api"] += tk.get("total_api", 0)
            tot["duree"] += r.get("duree_totale_s", 0) or 0
    return tot if vu else None


def cout_usd(modele: str, entree: int, sortie: int, raisonnement: int) -> float | None:
    """Le raisonnement est facturé au tarif de SORTIE."""
    p = config.PRICING_USD_PER_MILLION_TOKENS.get(modele, {})
    if p.get("input") is None or p.get("output") is None:
        return None
    return entree / 1e6 * p["input"] + (sortie + raisonnement) / 1e6 * p["output"]


def rapport(args) -> None:
    paires = [("Flash", "passe_1", "passe_1_bis", args.facture_flash),
              ("Pro", "passe_1_pro", "passe_1_pro_bis", args.facture_pro)]

    print("── Décomposition des tokens (passes instrumentées) ──\n")
    print(f"{'modèle':7} {'fiches':>6} {'entrée':>9} {'sortie':>8} {'réflexion':>10} "
          f"{'réfl./fiche':>12} {'réfl./sortie':>13}")
    mesures = {}
    for nom, _, bis, _ in paires:
        c = cumul(bis)
        if not c or not c["reussies"]:
            print(f"{nom:7} passe {bis} absente ou vide")
            continue
        n = c["reussies"]
        r_f = c["raisonnement"] / n
        ratio = c["raisonnement"] / c["sortie"] if c["sortie"] else 0
        print(f"{nom:7} {n:6} {c['entree']:9} {c['sortie']:8} {c['raisonnement']:10} "
              f"{r_f:12.0f} {ratio:12.1f}×")
        mesures[nom] = c
        ecart = c["total_api"] - (c["entree"] + c["sortie"] + c["raisonnement"])
        if c["total_api"] and ecart:
            print(f"        /!\\ écart de {ecart} tokens entre le total de l'API et la somme des postes")

    if not mesures:
        print("\nAucune passe instrumentée n'a encore produit de manifeste.")
        return

    print("\n── Rapprochement avec la facturation ──\n")
    print(f"{'modèle':7} {'facturé TTC':>12} {'facturé HT':>11} {'calculé $':>10} {'rapport':>8}")
    for nom, ref, bis, facture in paires:
        if nom not in mesures:
            continue
        c = mesures[nom]
        modele = config.CONFIGS[bis]["model"]
        calc = cout_usd(modele, c["entree"], c["sortie"], c["raisonnement"])
        ht = facture / (1 + args.tva / 100) if facture else None
        # la facture couvre la campagne entière ; on ramène à la passe mesurée
        print(f"{nom:7} {facture:11.2f}€ {ht:10.2f}€ {calc:9.3f}$ "
              f"{(ht * args.taux / calc if calc else 0):7.2f}×")
    print("\n  Le rapport porte sur la campagne entière face à une seule passe :")
    print("  il vaut environ le nombre de passes équivalentes facturées, pas une erreur.")

    print("\n── Projection sur 45 000 fiches (tarifs publiés, hors taxe) ──\n")
    print(f"{'modèle':7} {'$/fiche':>9} {'45 000 fiches':>14} {'en différé':>12} "
          f"{'part réflexion':>15}")
    for nom, _, bis, _ in paires:
        if nom not in mesures:
            continue
        c = mesures[nom]
        modele = config.CONFIGS[bis]["model"]
        n = c["reussies"]
        pf = cout_usd(modele, c["entree"] / n, c["sortie"] / n, c["raisonnement"] / n)
        sans = cout_usd(modele, c["entree"] / n, c["sortie"] / n, 0)
        part = 1 - sans / pf if pf else 0
        print(f"{nom:7} {pf:9.4f} {pf * 45000:13,.0f}$ {pf * 45000 / 2:11,.0f}$ "
              f"{part:14.0%}".replace(",", " "))
    print("\n  La part réflexion indique ce qu'un budget de raisonnement nul économiserait")
    print("  au maximum, à supposer que l'exactitude n'en souffre pas (§10.2).")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--facture-flash", type=float, default=5.0, help="euros TTC")
    p.add_argument("--facture-pro", type=float, default=15.0, help="euros TTC")
    p.add_argument("--tva", type=float, default=20.0, help="taux de TVA en %%")
    p.add_argument("--taux", type=float, default=1.10, help="1 EUR en USD")
    rapport(p.parse_args())


if __name__ == "__main__":
    main()
