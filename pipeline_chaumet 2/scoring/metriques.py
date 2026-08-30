"""Persistance des métriques agrégées d'une passe.

Les rapports affichent leurs résultats au terminal, où ils disparaissent à la
fermeture de la fenêtre. Or comparer six passes suppose de disposer des chiffres
sous une forme relisible, sans avoir à relancer chaque rapport ni à recopier des
tableaux à la main. Chaque outil de scoring dépose donc sa section dans un
fichier unique par passe, `verification_croisee/metriques_<config>.json`.

Le fichier est fusionné et non écrasé : `report.py` y écrit la section
« scoring », `conformite.py` la section « conformite », dans n'importe quel
ordre et sans se détruire mutuellement.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def chemin_metriques(base_dir: Path, config_name: str) -> Path:
    return base_dir / "verification_croisee" / f"metriques_{config_name}.json"


def fusionner(chemin: Path, section: str, donnees: dict[str, Any], config_name: str) -> Path:
    """Écrit ou remplace une section, en conservant les autres."""
    contenu: dict[str, Any] = {}
    if chemin.exists():
        try:
            contenu = json.loads(chemin.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            contenu = {}

    contenu["config"] = config_name
    contenu.setdefault("sections", {})
    contenu["sections"][section] = {
        "horodatage": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        **donnees,
    }

    chemin.parent.mkdir(parents=True, exist_ok=True)
    chemin.write_text(json.dumps(contenu, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return chemin
