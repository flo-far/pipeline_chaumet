"""Mesure le bruit résiduel du modèle : de combien le score d'une passe varie
d'une exécution à l'autre, à configuration strictement identique.

Ce plancher de bruit conditionne la lecture de toutes les comparaisons entre
passes : en dessous de lui, un écart n'est pas interprétable. Voir §3.6.3 du
document projet.

Prérequis — produire plusieurs exécutions de la même config :

    python pipeline/run_pass.py --config passe_1 --repetition 1
    python pipeline/run_pass.py --config passe_1 --repetition 2
    python pipeline/run_pass.py --config passe_1 --repetition 3

Usage :
    python scoring/stabilite.py --config passe_1
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
from metriques import chemin_metriques, fusionner  # noqa: E402
from report import construire_dataframe  # noqa: E402

# Une différence entre deux passes porte la variabilité des deux exécutions.
# Si chacune a un écart-type sigma, celui de leur différence vaut sigma*racine(2) ;
# un seuil à deux écarts-types sur la différence revient donc à 2*racine(2) ≈ 2.83.
FACTEUR_SEUIL = 2 * 2 ** 0.5


def dossiers_repetitions(config_name: str) -> list[Path]:
    """Exécutions comparables d'une même passe.

    La passe de référence compte comme une mesure à part entière : elle a été
    produite dans les mêmes conditions que les répétitions. L'ignorer
    obligerait à repayer une exécution pour rien.
    """
    base = config.CONFIGS[config_name]["output_dir"]
    dossiers = [base] if any(base.glob("AHC-*.json")) else []
    dossiers += sorted(
        (d for d in base.glob("rep_*") if d.is_dir() and any(d.glob("*.json"))),
        key=lambda d: d.name,
    )
    return dossiers


def controler_comparabilite(dossiers: list[Path]) -> None:
    """Refuse de mesurer un bruit entre exécutions qui ne sont pas comparables.

    Mesurer la dispersion suppose que seul le hasard du tirage ait varié. Si le
    prompt ou le modèle ont changé entre deux exécutions, l'écart observé n'est
    plus du bruit mais un effet — et le publier comme seuil de significativité
    serait une faute. Les manifestes permettent de le vérifier.
    """
    signatures = {}
    for d in dossiers:
        manifeste = d / "run.json"
        if not manifeste.exists():
            print(f"/!\\ {d.name} : aucun manifeste, comparabilité invérifiable.")
            continue
        runs = json.loads(manifeste.read_text(encoding="utf-8")).get("runs", [])
        for r in runs:
            signatures.setdefault(d.name, set()).add(
                (r["modele"], r["prompt"]["sha256"], json.dumps(r["generation"], sort_keys=True))
            )

    toutes = {s for v in signatures.values() for s in v}
    if len(toutes) > 1:
        print("/!\\ ATTENTION — les exécutions ne partagent pas la même configuration :")
        for nom, sigs in signatures.items():
            for modele, sha, gen in sigs:
                print(f"      {nom:12} modèle {modele}  prompt {sha[:12]}…")
        print("      La dispersion mesurée mélange le bruit du modèle et un "
              "changement de dispositif : elle n'est pas un plancher de bruit.\n")
    elif toutes:
        modele, sha, _ = next(iter(toutes))
        print(f"Configuration commune vérifiée : {modele}, prompt {sha[:12]}…\n")


def taux_exactitude(df: pd.DataFrame) -> dict[str, float]:
    """Part de correspondances exactes, globalement et par nature de champ.

    Les champs qualitatifs (mentions) sont exclus : ils ne sont pas notés, et
    leur faire produire un « taux d'exactitude » n'aurait aucun sens. Leur
    variabilité reste visible dans la section des champs instables.
    """
    note = df[df["nature"] != "qualitatif"]
    taux = {"global": (note["niveau"] == 0).mean() * 100}
    for nature, sous_df in note.groupby("nature"):
        taux[nature] = (sous_df["niveau"] == 0).mean() * 100
    return taux


def rapport(config_name: str) -> None:
    dossiers = dossiers_repetitions(config_name)
    if len(dossiers) < 2:
        raise SystemExit(
            f"Il faut au moins 2 exécutions pour mesurer une dispersion "
            f"(trouvé : {len(dossiers)}).\n"
            f"La passe de référence compte pour une ; ajoutez-en avec "
            f"run_pass.py --config {config_name} --repetition N."
        )

    controler_comparabilite(dossiers)
    dfs = {d.name: construire_dataframe(config_name, output_dir=d) for d in dossiers}
    vides = [nom for nom, df in dfs.items() if df.empty]
    if vides:
        raise SystemExit(f"Répétitions sans extraction exploitable : {vides}")

    taux = pd.DataFrame({nom: taux_exactitude(df) for nom, df in dfs.items()}).T
    fiches = {len(df["cote_archive"].unique()) for df in dfs.values()}

    print(f"── Stabilité de {config_name} ──")
    print(f"Répétitions : {len(dossiers)}   Fiches par répétition : {sorted(fiches)}")
    if len(fiches) > 1:
        print("/!\\ Les répétitions ne couvrent pas le même nombre de fiches : "
              "la dispersion mesurée mélange le bruit du modèle et un effet d'échantillon.")
    print(f"Paramètres de génération : {config.CONFIGS[config_name].get('generation')}\n")

    print("Taux de correspondance exacte (%) :")
    print(taux.round(2).to_string())

    # L'écart-type est très mal estimé sur 3 ou 4 mesures : l'étendue observée
    # est retenue comme indicateur publiable, l'écart-type comme indication.
    #
    # Deux seuils sont donnés car ils répondent à deux protocoles distincts.
    # Comparer deux exécutions isolées expose à la variabilité des deux, d'où un
    # facteur racine(2). Comparer les moyennes de n exécutions divise cette
    # variabilité par racine(n) : répéter chaque passe resserre donc le seuil
    # sans rien changer au dispositif, et c'est le protocole à privilégier.
    n = len(dossiers)
    ecart_type = taux.std(ddof=1)
    synthese = pd.DataFrame(
        {
            "écart-type": ecart_type,
            "étendue": taux.max() - taux.min(),
            "seuil 1 exéc. vs 1 exéc.": ecart_type * FACTEUR_SEUIL,
            f"seuil moyenne de {n} vs moyenne de {n}": ecart_type * FACTEUR_SEUIL / n ** 0.5,
        }
    )
    print("\nDispersion entre répétitions (points de pourcentage) :")
    print(synthese.round(2).to_string())
    print(
        f"\nLecture : un écart entre deux passes inférieur au seuil correspondant "
        f"n'est pas interprétable.\n"
        f"Le premier seuil vaut {FACTEUR_SEUIL:.2f} écarts-types — la variabilité d'une "
        f"différence est plus grande que celle d'un score isolé.\n"
        f"Le second s'applique si chaque passe est répétée {n} fois et que l'on compare "
        f"les moyennes ; c'est le protocole à privilégier, il resserre le seuil de "
        f"{(1 - 1 / n ** 0.5):.0%} sans coût méthodologique."
    )

    print("\n── Champs instables ──")
    colonnes = ["cote_archive", "champ", "niveau"]
    fusion = pd.concat(
        [df[colonnes].assign(repetition=nom).reset_index(drop=True)
         for nom, df in dfs.items()],
        ignore_index=True,
    )
    par_champ = fusion.groupby(["cote_archive", "champ"])["niveau"].nunique()
    instables = par_champ[par_champ > 1]
    total = len(par_champ)
    print(f"{len(instables)} / {total} champs changent de niveau selon la répétition "
          f"({len(instables) / total:.1%})")
    if not instables.empty:
        generique = (
            instables.reset_index()["champ"].str.replace(r"\[\d+\]", "[]", regex=True)
        )
        print("\nRépartition (les zones où le modèle hésite d'une exécution à l'autre "
              "sont un indicateur de difficulté, obtenu sans annotation) :")
        print(generique.value_counts().head(15).to_string())

    fusionner(
        chemin_metriques(config.BASE_DIR, config_name),
        "stabilite",
        {
            "executions": n,
            "fiches_par_execution": sorted(fiches),
            "taux_par_execution": {k: {a: round(b, 4) for a, b in v.items()}
                                   for k, v in taux.to_dict("index").items()},
            "ecart_type": {k: round(v, 4) for k, v in ecart_type.items()},
            "etendue": {k: round(v, 4) for k, v in (taux.max() - taux.min()).items()},
            "seuil_execution_unique": {k: round(v * FACTEUR_SEUIL, 4)
                                       for k, v in ecart_type.items()},
            f"seuil_moyenne_de_{n}": {k: round(v * FACTEUR_SEUIL / n ** 0.5, 4)
                                      for k, v in ecart_type.items()},
            "champs_instables": int(len(instables)),
            "champs_compares": int(total),
        },
        config_name,
    )

    out = config.BASE_DIR / "verification_croisee" / f"stabilite_{config_name}.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    instables.rename("niveaux_distincts").reset_index().to_csv(out, index=False)
    print(f"\nDétail des champs instables écrit dans {out}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="passe_1")
    rapport(parser.parse_args().config)


if __name__ == "__main__":
    main()
