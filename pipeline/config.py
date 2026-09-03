"""Configuration centrale du pipeline : chemins, clé API, et liste des passes."""
from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))  # permet `import models` depuis pipeline/ et scoring/
IMAGES_DIR = BASE_DIR / "images"
FICHES_DIR = BASE_DIR / "fiches"  # vérité terrain
PROMPTS_DIR = BASE_DIR / "prompts"
REFERENTIELS_DIR = BASE_DIR / "referentiels"
OUTPUTS_DIR = BASE_DIR / "outputs"
SCHEMA_PATH = BASE_DIR / "schema" / "schema_verite_terrain.json"

load_dotenv(BASE_DIR / ".env")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# Un CONFIG par passe du document (§3.3). Seule la passe 1 (baseline) est
# câblée pour l'instant ; les suivantes seront ajoutées au fur et à mesure
# (modèle Pro, enrichissements, hybride, Gemma — voir le plan).

# Paramètres d'échantillonnage communs à toutes les passes. Sans eux, l'API
# applique sa température par défaut (~1.0) : deux exécutions de la même passe
# sur la même fiche donneraient des résultats différents, et un écart entre
# deux passes ne serait plus attribuable à l'enrichissement testé. Ces valeurs
# doivent rester identiques d'une passe à l'autre — c'est la condition pour que
# les six passes soient comparables. Le bruit résiduel se mesure avec
# scoring/stabilite.py (voir §3.6.3 du document projet).
GENERATION = {
    "temperature": 0.0,
    "seed": 42,
    # Mode JSON natif : l'API garantit une sortie parsable et le modèle ne peut
    # plus encadrer sa réponse d'un bloc ```json ni d'une phrase d'introduction.
    "response_mime_type": "application/json",
}

AUCUN_ENRICHISSEMENT = {
    "referentiel_materiaux": False,
    "referentiel_abreviations": False,
    "index_scripteurs": False,
    "few_shot": False,
}


def _passe(
    nom: str,
    label: str,
    model: str,
    prompt: str = "passe1_baseline.txt",
    enrichissements: dict | None = None,
    generation: dict | None = None,
) -> tuple[str, dict]:
    """Fabrique une configuration de passe.

    Le dossier de sortie porte le nom de la passe, ce qui garantit qu'aucune
    passe n'écrase une autre. Le prompt et les paramètres de génération sont
    partagés par défaut : c'est la condition pour que la comparaison entre
    modèles ne mesure que le modèle.
    """
    return nom, {
        "label": label,
        "model": model,
        "prompt_path": PROMPTS_DIR / prompt,
        "output_dir": OUTPUTS_DIR / nom,
        "generation": GENERATION if generation is None else generation,
        "enrichissements": enrichissements or AUCUN_ENRICHISSEMENT,
    }


# Les baselines partagent le MÊME prompt et les MÊMES paramètres de génération :
# seul le modèle varie. Toute autre différence rendrait la comparaison
# inexploitable.
#
# La variable testée est le rapport coût/performance : un modèle léger et bon
# marché (Flash, 0,50/3,00 $) contre un modèle lourd et cher (Pro, 2,00/12,00 $),
# soit un facteur quatre. C'est la question que doit trancher un établissement
# qui envisage de traiter 45 000 fiches.
#
# Limite assumée : les deux modèles ne sont pas de la même génération (3 et 3.1),
# le prédécesseur direct de Pro ayant été retiré par Google en cours de projet.
# Un avantage de Pro serait donc en partie imputable à sa génération plus
# récente. Voir §6.2 du document.
#
# Les identifiants ci-dessous doivent être ceux exposés par votre clé. Pour les
# vérifier sans consommer de quota :
#     python -c "import sys; sys.path.insert(0,'pipeline'); from extract import creer_client; \
#                c = creer_client(); print(*sorted(m.name for m in c.models.list()), sep='\\n'); c.close()"
CONFIGS: dict[str, dict] = dict(
    [
        _passe("passe_1", "Baseline Gemini Flash — aucun enrichissement",
               "gemini-3-flash-preview"),
        _passe("passe_1_pro", "Baseline Gemini 3.1 Pro — aucun enrichissement",
               "gemini-3.1-pro-preview"),
        # --- Passes « bis » : instrumentation corrigée -----------------------
        # Strictement identiques aux deux références (même prompt, mêmes
        # paramètres de génération, même corpus) : SEULE change la mesure, le
        # pipeline relevant désormais les tokens de raisonnement et le total
        # publié par l'API, absents des manifestes de la première campagne.
        # Objet unique : chiffrer la décomposition du coût. Elles n'entrent pas
        # dans les résultats d'exactitude, lesquels ne dépendent d'aucun
        # décompte de tokens. L'empreinte du schéma diffère de celle des passes
        # initiales, models.py ayant gagné deux champs : c'est le comportement
        # attendu d'un protocole versionné.
        _passe("passe_1_bis", "Flash — instrumentation corrigée (coût)",
               "gemini-3-flash-preview"),
        _passe("passe_1_pro_bis", "Pro — instrumentation corrigée (coût)",
               "gemini-3.1-pro-preview"),
        # Gemma est différé : modèle à poids ouverts, il ne partage ni la même
        # API ni nécessairement le mode JSON natif et la graine, ce qui poserait
        # une question de comparabilité à traiter à part. Sa configuration
        # s'ajoutera ici le moment venu, sans rien changer au reste.
    ]
)

# Tarifs Gemini, en dollars par million de tokens.
#
# Relevés le 8 août 2026 sur https://ai.google.dev/gemini-api/docs/pricing,
# tarif « Standard », prompts de moins de 200 000 tokens — nos requêtes tournent
# autour de 7 500 tokens visibles en entrée, donc toujours dans le premier palier. Le mode Batch,
# non utilisé ici, coûterait moitié moins pour un traitement différé.
#
# Les tokens de raisonnement sont facturés au tarif de sortie sur les trois
# modèles, et peuvent dominer la facture. À revérifier avant tout chiffrage
# définitif : les tarifs des modèles "preview" changent sans préavis.
PRICING_USD_PER_MILLION_TOKENS: dict[str, dict[str, float | None]] = {
    "gemini-3-flash-preview": {"input": 0.50, "output": 3.00},
    "gemini-3.6-flash": {"input": 1.50, "output": 7.50},
    "gemini-3.1-pro-preview": {"input": 2.00, "output": 12.00},

}
