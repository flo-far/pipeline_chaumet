"""Reconstitue l'historique complet des exécutions depuis le journal du terminal.

Les manifestes ne consignent que le résultat final de chaque fiche : ils ignorent
les réessais, et ne sont pas écrits du tout lorsqu'une passe est suspendue par
Ctrl+Z ou interrompue par Ctrl+C. Le journal du terminal, lui, garde tout.

Ce module en extrait deux tableaux : une ligne par exécution et une ligne par
tentative d'extraction, avec le nombre de réessais et leur cause. C'est la seule
source permettant de mesurer la fiabilité d'exécution, laquelle distingue les
deux modèles bien plus nettement que leur exactitude.

Pour produire le journal : dans le Terminal, menu « Shell › Exporter le texte
sous… », enregistrer sous journal_terminal.txt à la racine du projet.

Usage :
    python scoring/journal.py                      # journal_terminal.txt
    python scoring/journal.py --journal fichier.txt
"""
from __future__ import annotations

import argparse
import csv
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "pipeline"))
import config  # noqa: E402

ENTETE = re.compile(r"^── (\S+) : (.+) ──$")
MODELE = re.compile(r"^Modèle\s+:\s*(\S+)")
REPETITION = re.compile(r"^Répétition\s+:\s*(\d+)")
REPRISE = re.compile(r"^Reprise : (\d+) fiche")
A_TRAITER = re.compile(r"^Fiches à traiter : (\d+)")
FICHE = re.compile(r"^\[(\d+)/(\d+)\]\s+(AHC-\S+)\s*\.\.\.\s*(.*)$")
TENTATIVE = re.compile(r"tentative (\d+) échouée \(([^)]+)\)")
OK = re.compile(r"OK \(([\d.]+)s\)")
ECHEC = re.compile(r"ÉCHEC(?: \(([\d.]+)s\))?(?: \(([^)]*)\))?\s*:\s*(.*)")
SUSPENDU = re.compile(r"^zsh: suspended|KeyboardInterrupt")
RESUME = re.compile(r"^── Résumé ──")
MANIFESTE = re.compile(r"^Manifeste\s+:")


def famille(msg: str) -> str:
    for motif, nom in [
        (r"prepayment credits are depleted", "crédits prépayés épuisés (429)"),
        (r"per_model_per_day|limit: 250", "quota journalier atteint (429)"),
        (r"429|RESOURCE_EXHAUSTED", "quota dépassé (429)"),
        (r"504|DEADLINE_EXCEEDED", "délai passerelle dépassé (504)"),
        (r"503|UNAVAILABLE", "modèle saturé (503)"),
        (r"499|CANCELLED", "opération annulée (499)"),
        (r"404|no longer available", "modèle retiré (404)"),
        (r"read operation timed out|ReadTimeout", "délai client dépassé"),
        (r"Connection reset|Errno 54", "connexion réinitialisée"),
        (r"setdefault", "réponse en tableau (bug pipeline, corrigé)"),
        (r"Extra data", "JSON suivi de contenu superflu (corrigé)"),
        (r"JSON invalide", "JSON malformé"),
        (r"Validation Pydantic", "structure non conforme au schéma"),
    ]:
        if re.search(motif, msg, re.I):
            return nom
    return "autre" if msg else ""


def analyser(texte: str) -> tuple[list[dict], list[dict]]:
    executions, tentatives = [], []
    courante: dict | None = None
    numero = 0
    attente: dict | None = None   # fiche en cours, dont les réessais s'accumulent

    def clore(statut: str) -> None:
        nonlocal courante, attente
        if attente:
            attente["issue"] = attente["issue"] or "interrompue"
            tentatives.append(attente); attente = None
        if courante:
            courante["fin"] = statut
            executions.append(courante); courante = None

    for ligne in texte.split("\n"):
        l = ligne.rstrip()

        if m := ENTETE.match(l.strip()):
            clore("terminée")
            numero += 1
            courante = {"n": numero, "config": m.group(1), "label": m.group(2),
                        "modele": "", "repetition": "référence", "reprise": 0,
                        "demandees": 0, "reussies": 0, "echouees": 0,
                        "tentatives_totales": 0, "manifeste": "non", "fin": ""}
            continue
        if courante is None:
            continue

        if m := MODELE.match(l.strip()): courante["modele"] = m.group(1); continue
        if m := REPETITION.match(l.strip()): courante["repetition"] = f"rep_{m.group(1)}"; continue
        if m := REPRISE.match(l.strip()): courante["reprise"] = int(m.group(1)); continue
        if m := A_TRAITER.match(l.strip()): courante["demandees"] = int(m.group(1)); continue
        if MANIFESTE.match(l.strip()): courante["manifeste"] = "oui"; continue

        if SUSPENDU.search(l):
            clore("suspendue (aucun manifeste)")
            continue

        if m := FICHE.match(l.strip()):
            if attente:
                attente["issue"] = attente["issue"] or "interrompue"
                tentatives.append(attente); attente = None
            attente = {"execution": courante["n"], "config": courante["config"],
                       "repetition": courante["repetition"], "modele": courante["modele"],
                       "rang": int(m.group(1)), "cote_archive": m.group(3),
                       "reessais": 0, "causes_reessais": [], "issue": "",
                       "duree_s": "", "famille": "", "message": ""}
            reste = m.group(4)
        else:
            reste = l.strip()

        if attente is None:
            continue

        for t in TENTATIVE.finditer(reste):
            attente["reessais"] += 1
            attente["causes_reessais"].append(t.group(2))
            courante["tentatives_totales"] += 1

        if m := OK.search(reste):
            attente["issue"] = "réussite"; attente["duree_s"] = float(m.group(1))
            courante["reussies"] += 1
            attente["causes_reessais"] = " | ".join(attente["causes_reessais"])
            tentatives.append(attente); attente = None
        elif m := ECHEC.search(reste):
            attente["issue"] = "échec"
            attente["duree_s"] = float(m.group(1)) if m.group(1) else ""
            msg = " ".join((m.group(3) or "").split())
            attente["message"] = msg[:200]; attente["famille"] = famille(msg)
            courante["echouees"] += 1
            attente["causes_reessais"] = " | ".join(attente["causes_reessais"])
            tentatives.append(attente); attente = None

    clore("terminée")
    return executions, tentatives


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--journal", default="journal_terminal.txt")
    args = p.parse_args()

    chemin = config.BASE_DIR / args.journal
    if not chemin.exists():
        raise SystemExit(
            f"Journal introuvable : {chemin}\n"
            "Dans le Terminal : menu « Shell › Exporter le texte sous… », "
            "enregistrer sous ce nom à la racine du projet."
        )

    executions, tentatives = analyser(chemin.read_text(encoding="utf-8", errors="ignore"))
    if not executions:
        raise SystemExit("Aucune exécution reconnue dans ce journal.")

    sortie = config.BASE_DIR / "verification_croisee"
    sortie.mkdir(parents=True, exist_ok=True)
    for nom, lignes in [("journal_executions.csv", executions),
                        ("journal_fiches.csv", tentatives)]:
        with (sortie / nom).open("w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(lignes[0]))
            w.writeheader(); w.writerows(lignes)

    print("── Journal reconstitué ──")
    print(f"{len(executions)} exécutions, {len(tentatives)} tentatives d'extraction\n")
    print(f"  {'#':>2} {'config':13}{'série':11}{'modèle':24}"
          f"{'dem.':>5}{'ok':>4}{'ko':>4}{'rées.':>7}  manif.  fin")
    for e in executions:
        print(f"  {e['n']:>2} {e['config']:13}{e['repetition']:11}{e['modele'][:23]:24}"
              f"{e['demandees']:>5}{e['reussies']:>4}{e['echouees']:>4}"
              f"{e['tentatives_totales']:>7}  {e['manifeste']:6}  {e['fin']}")

    print(f"\n  Écrits dans {sortie}/journal_executions.csv et journal_fiches.csv")


if __name__ == "__main__":
    main()
