"""Agrège les écarts d'une passe en CSV, et isole les écarts à revoir pour
la vérification croisée (§3.4) avant verrouillage de la vérité terrain.

Usage :
    python scoring/report.py --config passe_1
    python scoring/report.py --config passe_1 --seuil-revue 2
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "pipeline"))
import config  # noqa: E402
from compare import comparer, confiances_par_champ  # noqa: E402
from metriques import chemin_metriques, fusionner  # noqa: E402


def construire_dataframe(config_name: str, output_dir: Path | None = None) -> pd.DataFrame:
    """Compare la vérité terrain aux extractions d'une passe.

    `output_dir` permet de pointer un dossier de répétition
    (outputs/<config>/rep_n/) au lieu de la passe de référence.
    """
    output_dir = output_dir or config.CONFIGS[config_name]["output_dir"]
    lignes: list[dict] = []
    inversions: list[dict] = []
    for fiche_gt_path in sorted(config.FICHES_DIR.glob("*.json")):
        cote = fiche_gt_path.stem
        extraction_path = output_dir / f"{cote}.json"
        if not extraction_path.exists():
            continue  # pas encore extraite (ou échec, voir <cote>.erreur.txt)

        gt = json.loads(fiche_gt_path.read_text(encoding="utf-8"))
        extrait = json.loads(extraction_path.read_text(encoding="utf-8"))
        comparaison = comparer(gt, extrait)
        confiances = confiances_par_champ(extrait)
        # Les lignes de matériau dont la VT porte le type "Divers" sont la seule
        # population où le prompt demande explicitement de moduler la confiance :
        # elle s'analyse à part et n'entre pas dans la courbe de calibration (§3.6.2).
        inversions.append({"cote_archive": cote, **comparaison.inversions})
        divers = {
            f"materiaux[{i}]"
            for i, m in enumerate(gt.get("materiaux", []))
            if m.get("type") == "Divers"
        }

        for e in comparaison.ecarts:
            lignes.append(
                {
                    "cote_archive": cote,
                    "type_fiche": gt["metadata"]["type_fiche"],
                    "champ": e.champ,
                    "nature": e.nature,
                    "niveau": e.niveau,
                    "label": e.label,
                    "confiance": confiances.get(e.champ),
                    "index_extraction": comparaison.index_extraction(e.champ),
                    "bloc_divers": e.champ.split(".", 1)[0] in divers,
                    "valeur_gt": e.valeur_gt,
                    "valeur_extraite": e.valeur_extraite,
                }
            )
    df = pd.DataFrame(lignes)
    # Attaché au DataFrame plutôt qu'en colonne : c'est une propriété de la
    # fiche, pas du champ. L'ordre des tableaux n'est pas pénalisé (§3.6.4),
    # seule son ampleur est reportée.
    #
    # Stocké en liste de dictionnaires et non en DataFrame : pandas compare les
    # `attrs` lors d'un concat, et comparer deux DataFrames y lève une erreur.
    df.attrs["inversions"] = inversions
    return df


def taux_par_nature(df: pd.DataFrame) -> pd.DataFrame:
    """Ventile les écarts selon la compétence mesurée.

    « lecture » = déchiffrer l'archive et la restituer verbatim ;
    « normalisation » = appliquer une règle d'écriture à ce qui a été lu.
    Les deux taux ne doivent pas être agrégés : un référentiel n'améliore
    que le second, un modèle plus capable surtout le premier. Agrégés, les
    deux effets se diluent et la comparaison entre passes perd sa lisibilité.
    """
    lignes = []
    for nature, sous_df in df[df["nature"] != "qualitatif"].groupby("nature", sort=False):
        n = len(sous_df)
        renseignes = sous_df[sous_df["valeur_gt"].notna()]
        lignes.append(
            {
                "nature": nature,
                "champs": n,
                "exact (niv. 0)": f"{(sous_df['niveau'] == 0).mean():.1%}",
                "à revoir (niv. >= 2)": f"{(sous_df['niveau'] >= 2).mean():.1%}",
                "critique (niv. 4)": f"{(sous_df['niveau'] == 4).mean():.1%}",
                "exact hors champs vides": (
                    f"{(renseignes['niveau'] == 0).mean():.1%}" if len(renseignes) else "n/a"
                ),
            }
        )
    return pd.DataFrame(lignes).set_index("nature")


def rapport_mentions(df: pd.DataFrame) -> None:
    """Relevé qualitatif des mentions, qui ne sont pas notées.

    Le prompt laisse le modèle juger de ce qui mérite d'être relevé : il n'y a
    donc pas de « bonne » liste de mentions à laquelle comparer la sienne. Ce
    relevé sert à l'analyse qualitative prévue au §3.5, et à documenter ce que
    l'archive contient au-delà du périmètre indexé.
    """
    qual = df[df["nature"] == "qualitatif"]
    if qual.empty:
        return
    extraites = qual[qual["valeur_extraite"].notna()]
    print("\n── Mentions (qualitatif, non noté) ──")
    print(f"{len(extraites)} mentions relevées sur "
          f"{qual['cote_archive'].nunique()} fiche(s), "
          f"{len(qual[qual['valeur_gt'].notna()])} présentes dans la vérité terrain.")
    apercu = extraites["valeur_extraite"].astype(str).str.slice(0, 58)
    for v in apercu.head(12):
        print(f"   · {v}")
    if len(apercu) > 12:
        print(f"   … et {len(apercu) - 12} autres (voir le CSV détaillé)")


def rapport_confiance(df: pd.DataFrame) -> None:
    """Analyse le score de confiance déclaré comme covariable du niveau d'erreur.

    Voir §3.6.2 du document projet pour les précautions d'interprétation. Trois
    choses sont mesurées séparément, jamais agrégées : la forme de la
    distribution (un score qui ne varie pas ne porte aucune information), son
    pouvoir discriminant sur la population de contrôle, et le comportement sur
    les lignes "Divers", seule population où le prompt demande explicitement
    d'abaisser la confiance en cas de doute — et où la réponse correcte est donc
    un niveau 0 assorti d'une confiance basse.
    """
    print("\n── Score de confiance déclaré ──")
    renseigne = df[df["confiance"].notna()]
    if renseigne.empty:
        print("Le modèle n'a renseigné aucun score de confiance : analyse impossible.")
        return

    couverture = len(renseigne) / len(df)
    valeurs = renseigne["confiance"]
    print(f"Champs avec confiance : {len(renseigne)}/{len(df)} ({couverture:.0%})")
    print(f"Valeurs distinctes    : {valeurs.nunique()}  (min {valeurs.min():.2f}, "
          f"médiane {valeurs.median():.2f}, max {valeurs.max():.2f})")
    if valeurs.nunique() <= 10:
        print("Distribution :")
        print(valeurs.value_counts().sort_index().to_string())
    if valeurs.nunique() < 3:
        print("/!\\ Score quasi constant : aucun pouvoir discriminant exploitable.")

    controle = renseigne[~renseigne["bloc_divers"]]
    if not controle.empty:
        print("\nConfiance moyenne par niveau (population de contrôle, hors « Divers ») :")
        print(controle.groupby("niveau")["confiance"].agg(["count", "mean", "median"]).to_string())
        corrects = controle[controle["niveau"] == 0]["confiance"]
        errones = controle[controle["niveau"] >= 2]["confiance"]
        if not corrects.empty and not errones.empty:
            ecart = corrects.mean() - errones.mean()
            print(f"\nPouvoir discriminant : {ecart:+.3f} "
                  f"(confiance moyenne des champs exacts moins celle des champs erronés ;"
                  f" une valeur nulle ou négative rend le score inexploitable pour le triage)")

        critiques = controle[controle["niveau"] == 4]
        if not critiques.empty:
            sures = (critiques["confiance"] >= 0.8).mean()
            print(f"Erreurs critiques (niveau 4) : {len(critiques)}, dont "
                  f"{sures:.0%} émises avec une confiance >= 0.80 — cas indétectables "
                  f"sans retour à l'archive.")

    divers = renseigne[renseigne["bloc_divers"]]
    print(f"\nLignes « Divers » (analysées à part, effectif faible) : {len(divers)} champs")
    if not divers.empty:
        print(f"  confiance moyenne : {divers['confiance'].mean():.2f} "
              f"contre {controle['confiance'].mean():.2f} sur le contrôle")
        print("  rappel : sur cette population, un niveau 0 à confiance basse est le "
              "résultat attendu, non une anomalie de calibration.")


def collecter_metriques(df: pd.DataFrame) -> dict:
    """Rassemble sous forme relisible ce que rapport_synthese affiche au terminal."""
    note = df[df["nature"] != "qualitatif"]
    inv = pd.DataFrame(df.attrs.get("inversions") or [])
    colonnes_inv = [c for c in inv.columns if c != "cote_archive"]

    resultat = {
        "fiches": int(df["cote_archive"].nunique()),
        "champs_notes": int(len(note)),
        "champs_qualitatifs": int(len(df) - len(note)),
        "repartition_niveaux": {str(k): int(v) for k, v in note["niveau"].value_counts().items()},
        "par_nature": {},
        "par_type_fiche": {
            str(t): {str(n): int(c) for n, c in g["niveau"].value_counts().items()}
            for t, g in note.groupby("type_fiche")
        },
        "inversions_ordre": int(inv[colonnes_inv].sum().sum()) if colonnes_inv else 0,
    }
    for nature, sous in note.groupby("nature"):
        renseignes = sous[sous["valeur_gt"].notna()]
        resultat["par_nature"][nature] = {
            "champs": int(len(sous)),
            "exact": round(float((sous["niveau"] == 0).mean()), 4),
            "a_revoir": round(float((sous["niveau"] >= 2).mean()), 4),
            "critique": round(float((sous["niveau"] == 4).mean()), 4),
            "exact_hors_champs_vides": (
                round(float((renseignes["niveau"] == 0).mean()), 4) if len(renseignes) else None
            ),
        }

    conf = df[df["confiance"].notna()]
    if not conf.empty:
        controle = conf[~conf["bloc_divers"]]
        corrects = controle[controle["niveau"] == 0]["confiance"]
        errones = controle[controle["niveau"] >= 2]["confiance"]
        resultat["confiance"] = {
            "couverture": round(float(len(conf) / len(df)), 4),
            "valeurs_distinctes": int(conf["confiance"].nunique()),
            "mediane": float(conf["confiance"].median()),
            "moyenne_par_niveau": {
                str(k): round(float(v), 4)
                for k, v in controle.groupby("niveau")["confiance"].mean().items()
            },
            "pouvoir_discriminant": (
                round(float(corrects.mean() - errones.mean()), 4)
                if len(corrects) and len(errones) else None
            ),
        }
    return resultat


def rapport_synthese(df: pd.DataFrame) -> None:
    if df.empty:
        print("Aucune extraction trouvée pour cette config (lance run_pass.py d'abord).")
        return

    note = df[df["nature"] != "qualitatif"]
    print(f"Fiches couvertes : {df['cote_archive'].nunique()}")
    print(f"Champs notés     : {len(note)}   "
          f"(+ {len(df) - len(note)} champs qualitatifs, hors score)\n")

    print("Répartition par niveau :")
    print(note["niveau"].value_counts().sort_index().to_string())

    inv = pd.DataFrame(df.attrs.get("inversions") or [])
    if not inv.empty:
        colonnes = [c for c in inv.columns if c != "cote_archive"]
        total = int(inv[colonnes].sum().sum())
        touchees = int((inv[colonnes].sum(axis=1) > 0).sum())
        print(f"\nOrdre des tableaux : {total} inversions sur {touchees} fiche(s) "
              f"— non pénalisé, reporté à titre indicatif.")

    print("\n── Lecture vs normalisation ──")
    print(taux_par_nature(df).to_string())

    print("\nRépartition par type de fiche et niveau :")
    print(pd.crosstab(note["type_fiche"], note["niveau"]).to_string())

    rapport_mentions(df)

    print("\nChamps les plus problématiques (niveau >= 2) :")
    problematiques = note[note["niveau"] >= 2]
    if not problematiques.empty:
        # normalise le nom de champ pour regrouper materiaux[0].x et materiaux[1].x
        champ_generique = problematiques["champ"].str.replace(r"\[\d+\]", "[]", regex=True)
        print(champ_generique.value_counts().head(15).to_string())
    else:
        print("(aucun)")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="passe_1")
    parser.add_argument(
        "--seuil-revue",
        type=int,
        default=2,
        help="Niveau minimum à isoler dans le CSV de vérification croisée (défaut 2)",
    )
    parser.add_argument(
        "--out",
        default=None,
        help="Chemin du CSV détaillé (défaut : verification_croisee/ecarts_<config>.csv)",
    )
    args = parser.parse_args()

    df = construire_dataframe(args.config)
    rapport_synthese(df)
    rapport_confiance(df)

    if not df.empty:
        cible = fusionner(
            chemin_metriques(config.BASE_DIR, args.config),
            "scoring",
            collecter_metriques(df),
            args.config,
        )
        print(f"\nMétriques agrégées écrites dans {cible}")

    if df.empty:
        return

    out_path = Path(args.out) if args.out else (
        config.BASE_DIR / "verification_croisee" / f"ecarts_{args.config}.csv"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out_path, index=False)
    print(f"\nDétail complet écrit dans {out_path}")

    a_revoir = df[df["niveau"] >= args.seuil_revue]
    revue_path = out_path.parent / f"a_revoir_{args.config}.csv"
    a_revoir.to_csv(revue_path, index=False)
    print(f"{len(a_revoir)} écarts (niveau >= {args.seuil_revue}) à revoir dans {revue_path}")


if __name__ == "__main__":
    main()
