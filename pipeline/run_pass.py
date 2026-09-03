"""Lance une passe d'extraction sur tout ou partie du corpus.

Usage :
    python pipeline/run_pass.py --config passe_1 --sample 5
    python pipeline/run_pass.py --config passe_1 --cotes AHC-2016003-0075_004,AHC-2016003-0075_006
    python pipeline/run_pass.py --config passe_1              # corpus complet (50 fiches)
    python pipeline/run_pass.py --config passe_1 --repetition 2   # mesure du bruit
"""
from __future__ import annotations

import argparse
import hashlib
import json
import platform
import time
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version

import config
from extract import creer_client, est_fatal, extraire_fiche, ExtractionResult


def _empreinte(chemin) -> str | None:
    """SHA-256 d'un fichier — identifie une version de prompt sans ambiguïté."""
    try:
        return hashlib.sha256(chemin.read_bytes()).hexdigest()
    except OSError:
        return None


def _versions() -> dict[str, str]:
    paquets = ("google-genai", "pydantic", "tenacity", "rapidfuzz", "pandas")
    versions = {"python": platform.python_version()}
    for nom in paquets:
        try:
            versions[nom] = version(nom)
        except PackageNotFoundError:
            versions[nom] = "absent"
    return versions


def get_corpus() -> list[str]:
    """La liste des cotes de référence = les noms de fichiers de la vérité terrain."""
    return sorted(p.stem for p in config.FICHES_DIR.glob("*.json"))


def selectionner_cotes(corpus: list[str], sample: int | None, cotes_arg: str | None) -> list[str]:
    if cotes_arg:
        demandees = [c.strip() for c in cotes_arg.split(",") if c.strip()]
        inconnues = [c for c in demandees if c not in corpus]
        if inconnues:
            raise SystemExit(f"Cotes inconnues (absentes de fiches/) : {inconnues}")
        return demandees
    if sample:
        return corpus[:sample]
    return corpus


def run(
    config_name: str,
    cotes: list[str],
    repetition: int | None = None,
    reprendre: bool = False,
) -> None:
    if config_name not in config.CONFIGS:
        raise SystemExit(
            f"Config inconnue: {config_name!r}. Disponibles : {list(config.CONFIGS)}"
        )
    cfg = config.CONFIGS[config_name]
    # Une répétition écrit dans un sous-dossier dédié pour ne pas écraser la
    # passe de référence : elle sert à mesurer le bruit résiduel du modèle,
    # pas à produire un résultat (voir scoring/stabilite.py).
    sortie_dir = cfg["output_dir"] if repetition is None else cfg["output_dir"] / f"rep_{repetition}"
    sortie_dir.mkdir(parents=True, exist_ok=True)

    print(f"── {config_name} : {cfg['label']} ──")
    print(f"Modèle       : {cfg['model']}")
    print(f"Génération   : {cfg.get('generation') or 'paramètres par défaut de l’API'}")
    if repetition is not None:
        print(f"Répétition   : {repetition} → {sortie_dir}")
    if reprendre:
        # Reprise : on ne redemande que ce qui manque. Indispensable sur un
        # modèle qui échoue sur une partie des fiches — relancer la même
        # commande suffit alors à compléter la passe, sans refacturer ce qui
        # est déjà extrait.
        deja = {p.stem for p in sortie_dir.glob("*.json") if p.stem in set(cotes)}
        if deja:
            print(f"Reprise : {len(deja)} fiche(s) déjà extraite(s), ignorée(s).")
        cotes = [c for c in cotes if c not in deja]
        if not cotes:
            print("Rien à faire : toutes les fiches demandées sont déjà extraites.")
            return

    print(f"Fiches à traiter : {len(cotes)}")
    print()

    # Un seul client pour toute la passe : évite 50 poignées de main TLS et
    # permet la réutilisation des connexions. Le créer ici fait aussi échouer
    # immédiatement, et non fiche par fiche, si la clé API est absente.
    try:
        client = creer_client(cfg.get("timeout_ms", 240_000))
    except RuntimeError as e:
        raise SystemExit(str(e))

    debut = datetime.now(timezone.utc)
    t_passe = time.monotonic()
    durees: dict[str, float] = {}
    reussies: list[str] = []
    echouees: list[tuple[str, str]] = []
    tokens_entree_total = 0
    tokens_sortie_total = 0
    tokens_raisonnement_total = 0
    tokens_total_total = 0
    # Une saturation de modèle touche toutes les fiches, pas une seule :
    # au-delà de quelques échecs consécutifs, insister ne fait que perdre des
    # heures. Le compteur se remet à zéro dès qu'une fiche aboutit.
    echecs_consecutifs = 0
    SEUIL_ABANDON = 3

    # L'interruption est mémorisée au lieu d'être levée immédiatement : le
    # manifeste doit être écrit avant de sortir, faute de quoi les fiches déjà
    # extraites resteraient sans trace de provenance — c'est exactement ce qui
    # s'est produit sur passe_1_pro/rep_2, resté avec 36 fiches et aucun
    # manifeste après le déclenchement du coupe-circuit.
    interruption: str | None = None
    try:
        for i, cote in enumerate(cotes, start=1):
            print(f"[{i}/{len(cotes)}] {cote} ...", end=" ", flush=True)
            t0 = time.monotonic()
            try:
                resultat: ExtractionResult = extraire_fiche(
                    cote_archive=cote,
                    prompt_path=cfg["prompt_path"],
                    model=cfg["model"],
                    enrichissements=cfg["enrichissements"],
                    generation=cfg.get("generation"),
                    client=client,
                )
            except FileNotFoundError as e:
                print(f"ÉCHEC (images manquantes) : {e}")
                echouees.append((cote, str(e)))
                continue
            except Exception as e:  # erreurs API non transitoires après retries
                print(f"ÉCHEC (API) : {e}")
                echouees.append((cote, str(e)))
                echecs_consecutifs += 1
                if est_fatal(e):
                    interruption = (
                        f"\nInterruption : cette erreur se reproduira sur les "
                        f"{len(cotes) - i} fiches restantes.\n"
                        f"Modèle demandé : {cfg['model']!r} — vérifiez qu'il est "
                        f"toujours servi par votre clé."
                    )
                    break
                if echecs_consecutifs >= SEUIL_ABANDON:
                    interruption = (
                        f"\nInterruption : {echecs_consecutifs} échecs consécutifs.\n"
                        f"Dernière cause : {e}\n"
                        f"{len(reussies)} fiche(s) extraite(s) et conservée(s) ; "
                        f"relancez la même commande avec --reprendre."
                    )
                    break
                continue

            dt = time.monotonic() - t0
            if not resultat.ok:
                print(f"ÉCHEC ({dt:.1f}s) : {resultat.error}")
                echouees.append((cote, resultat.error or "inconnu"))
                (sortie_dir / f"{cote}.erreur.txt").write_text(
                    f"{resultat.error}\n\n--- réponse brute ---\n{resultat.raw_text}",
                    encoding="utf-8",
                )
                continue

            sortie = sortie_dir / f"{cote}.json"
            sortie.write_text(
                resultat.fiche.model_dump_json(indent=2, exclude_none=False),
                encoding="utf-8",
            )
            tokens_entree_total += resultat.fiche.metadata.tokens_entree or 0
            tokens_sortie_total += resultat.fiche.metadata.tokens_sortie or 0
            tokens_raisonnement_total += resultat.fiche.metadata.tokens_raisonnement or 0
            tokens_total_total += resultat.fiche.metadata.tokens_total or 0
            reussies.append(cote)
            durees[cote] = round(dt, 2)
            echecs_consecutifs = 0
            print(f"OK ({dt:.1f}s)" + (f"  [{resultat.error}]" if resultat.error else ""))

    finally:
        client.close()

    print()
    print("── Résumé ──")
    print(f"Réussies : {len(reussies)}/{len(cotes)}")
    if echouees:
        print(f"Échouées : {len(echouees)}")
        for cote, err in echouees:
            print(f"  - {cote} : {err}")
    print(f"Tokens entrée total  : {tokens_entree_total}")
    print(f"Tokens sortie total  : {tokens_sortie_total}")
    print(f"Tokens réflexion     : {tokens_raisonnement_total}")
    print(f"Tokens total (API)   : {tokens_total_total}")
    ecart = tokens_total_total - (tokens_entree_total + tokens_sortie_total + tokens_raisonnement_total)
    if tokens_total_total and ecart:
        print(f"  /!\\ écart inexpliqué de {ecart} tokens entre le total de l'API et la somme des postes")

    prix = config.PRICING_USD_PER_MILLION_TOKENS.get(cfg["model"], {})
    cout = None
    if prix.get("input") is not None and prix.get("output") is not None:
        # Les tokens de raisonnement sont facturés au tarif de sortie : les
        # omettre sous-estimait le coût d'un facteur quatre sur la campagne
        # initiale (voir models.Metadata et §7.3 du mémoire).
        cout = (tokens_entree_total / 1e6) * prix["input"] + (
            (tokens_sortie_total + tokens_raisonnement_total) / 1e6
        ) * prix["output"]
        print(f"Coût estimé          : ${cout:.4f}")
    else:
        print("Coût estimé          : tarifs non renseignés (config.PRICING_USD_PER_MILLION_TOKENS)")

    duree_totale = time.monotonic() - t_passe
    print(f"Durée totale         : {duree_totale:.0f}s "
          f"({duree_totale / max(len(cotes), 1):.1f}s par fiche)")

    # Manifeste : sans lui, une sortie n'est attribuable à aucune version du
    # dispositif. L'empreinte du prompt et la version exacte du modèle sont les
    # deux informations qui permettront, dans six mois ou entre deux passes, de
    # distinguer un effet réel d'un changement d'instrument (voir §3.6.3).
    manifeste = {
        "config": config_name,
        "label": cfg["label"],
        "repetition": repetition,
        "horodatage_debut": debut.isoformat(timespec="seconds"),
        "horodatage_fin": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "duree_totale_s": round(duree_totale, 1),
        "duree_par_fiche_s": durees,
        "modele": cfg["model"],
        "generation": cfg.get("generation"),
        "enrichissements": cfg["enrichissements"],
        "prompt": {
            "chemin": cfg["prompt_path"].name,
            "sha256": _empreinte(cfg["prompt_path"]),
            "octets": cfg["prompt_path"].stat().st_size,
        },
        "schema_sha256": _empreinte(config.SCHEMA_PATH),
        "versions": _versions(),
        "cotes_demandees": cotes,
        "reussies": reussies,
        "echouees": [{"cote_archive": c, "erreur": e} for c, e in echouees],
        "tokens": {
            "entree": tokens_entree_total,
            "sortie": tokens_sortie_total,
            "raisonnement": tokens_raisonnement_total,
            "total_api": tokens_total_total,
        },
        "tarifs_usd_par_million": prix or None,
        "cout_estime_usd": round(cout, 6) if cout is not None else None,
    }
    # Le manifeste s'accumule au lieu de s'écraser : une passe interrompue puis
    # reprise sur les seules fiches en échec produirait sinon un manifeste ne
    # décrivant que la reprise, et l'historique des fiches déjà extraites
    # disparaîtrait. Le cumul donne le coût et la couverture réels de la passe,
    # toutes exécutions confondues.
    chemin_manifeste = sortie_dir / "run.json"
    runs: list[dict] = []
    if chemin_manifeste.exists():
        try:
            ancien = json.loads(chemin_manifeste.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            ancien = {}
        if isinstance(ancien.get("runs"), list):
            runs = ancien["runs"]
        elif "horodatage_debut" in ancien:
            # Manifeste au format initial, plat : le convertir en première
            # exécution de l'historique plutôt que de le perdre.
            runs = [ancien]
    runs.append(manifeste)
    historique: dict = {"config": config_name, "runs": runs}

    couvertes = {c for r in historique["runs"] for c in r["reussies"]}
    historique["config"] = config_name
    historique["cumul"] = {
        "executions": len(historique["runs"]),
        "cotes_couvertes": len(couvertes),
        "tokens_entree": sum(r["tokens"]["entree"] for r in historique["runs"]),
        "tokens_sortie": sum(r["tokens"]["sortie"] for r in historique["runs"]),
        "tokens_raisonnement": sum(r["tokens"].get("raisonnement", 0) for r in historique["runs"]),
        "tokens_total_api": sum(r["tokens"].get("total_api", 0) for r in historique["runs"]),
        "duree_totale_s": round(sum(r["duree_totale_s"] for r in historique["runs"]), 1),
        "cout_estime_usd": round(
            sum(r["cout_estime_usd"] or 0 for r in historique["runs"]), 6
        ),
        "en_echec": sorted(
            {e["cote_archive"] for r in historique["runs"] for e in r["echouees"]} - couvertes
        ),
    }
    chemin_manifeste.write_text(
        json.dumps(historique, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"Manifeste            : {chemin_manifeste} "
          f"({historique['cumul']['executions']} exécution(s), "
          f"{historique['cumul']['cotes_couvertes']} cotes couvertes)")

    # L'interruption n'est signalée qu'ici : le manifeste doit être écrit
    # d'abord, faute de quoi les fiches déjà extraites resteraient sans trace
    # de provenance — c'est précisément ce qui s'est produit sur passe_1_pro/rep_2.
    if interruption:
        raise SystemExit(interruption)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="passe_1", help="Nom de la config (voir config.CONFIGS)")
    parser.add_argument("--sample", type=int, default=None, help="Traiter seulement les N premières cotes")
    parser.add_argument("--cotes", default=None, help="Liste explicite de cotes, séparées par des virgules")
    parser.add_argument(
        "--reprendre",
        action="store_true",
        help="Ne traiter que les fiches absentes du dossier de sortie. Relancer "
             "la même commande complète alors la passe sans repayer l'existant.",
    )
    parser.add_argument(
        "--repetition",
        type=int,
        default=None,
        help="Numéro de répétition : écrit dans outputs/<config>/rep_<n>/ au lieu "
             "d'écraser la passe de référence. Sert à mesurer le bruit résiduel "
             "du modèle (voir scoring/stabilite.py).",
    )
    args = parser.parse_args()

    corpus = get_corpus()
    if not corpus:
        raise SystemExit(f"Aucune fiche trouvée dans {config.FICHES_DIR}")

    cotes = selectionner_cotes(corpus, args.sample, args.cotes)
    run(args.config, cotes, args.repetition, args.reprendre)


if __name__ == "__main__":
    main()
