# pipeline_chaumet

Chaîne d'extraction de données et d'aide à la description archivistique appliquée aux fiches de gestion de la maison Chaumet, avec l'outillage de mesure qui l'accompagne.

Ce dépôt est le livrable technique du mémoire de master *Technologies numériques appliquées à l'histoire* de l'École nationale des chartes, **Extraction de données et description automatiques d'archives. Le cas des fiches de gestion Chaumet** (2026). Il en constitue la partie reproductible, à savoir la méthode, et rien de ce qui procède de l'archive elle-même.

---

## Ce que ce dépôt contient

| | |
|---|---|
| `models.py` | Le modèle de données Pydantic, avec `extra="forbid"`, qui définit ce qu'est une fiche extraite et refuse tout champ non prévu. |
| `pipeline/config.py` | Chemins, lecture de la clé d'accès depuis l'environnement, déclaration des passes et grille tarifaire. Les paramètres de génération y sont fixés une fois pour toutes, ce qui conditionne la comparabilité des passes. |
| `pipeline/extract.py` | Traitement d'une fiche, appel à l'interface d'inférence, validation de la sortie, relevé des compteurs de tokens y compris ceux de raisonnement. |
| `pipeline/run_pass.py` | Conduite d'une campagne sur un corpus, reprises, coupe-circuit, écriture du manifeste horodaté. |
| `schema/schema_verite_terrain.json` | Le schéma JSON de la référence. |
| `schema/fiche_vierge.json` | Un gabarit vide, utile pour amorcer une transcription à la main. |
| `scoring/` | Les neuf outils de mesure : comparaison à la référence, contrôle de conformité, métriques, stabilité entre exécutions répétées, décomposition des coûts, journaux et récapitulatifs. |

## Ce que ce dépôt ne contient pas

Ces exclusions sont déclarées dans `.gitignore` depuis le premier commit.

**Les consignes.** Le dossier `prompts/` est vide. Les consignes rédigées pour ce projet citent des passages transcrits des documents afin d'illustrer les règles de lecture, en sorte qu'elles portent des fragments d'archive et ne sont pas publiables. Il revient à qui reprend ce dépôt d'y placer les siennes, voir la section suivante.

**Les images d'archives.** Elles ne quittent pas l'entreprise et n'ont vocation à être transmises à personne. Le dossier `images/` est attendu par le pipeline mais demeure vide ici.

**La vérité terrain.** Les cinquante fiches transcrites à la main qui servent de référence à la mesure procèdent de l'archive et ne sont pas publiées. Le dossier `fiches/` demeure vide.

**Les sorties d'extraction.** Elles contiennent le texte des documents et sont, de surcroît, reproductibles depuis le pipeline. Le dossier `outputs/` demeure vide.

**Les référentiels.** Le référentiel des matières et l'index des noms sont extraits de bases internes et ne sont pas publiables.

**La clé d'accès.** Elle est lue depuis un fichier `.env` que le dépôt exclut par construction. Le fichier `.env.example` en documente le nom sans en porter la valeur.

Il en résulte que ce dépôt **ne se rejoue pas en l'état**. Il vaut comme documentation, en ce qu'il énonce dans une forme non ambiguë ce qu'aucune description en prose ne dirait avec la même précision, à savoir l'ordre exact des opérations, les paramètres transmis à l'interface, le traitement réservé aux échecs et les règles selon lesquelles un écart est compté.

---

## Fournir une consigne

Le pipeline ne fonctionne pas sans un fichier de consigne. Il faut donc en déposer un avant toute exécution.

```bash
# le nom attendu par défaut, déclaré dans pipeline/config.py
touch prompts/passe1_baseline.txt
```

Le nom du fichier est modifiable, chaque passe déclarant le sien dans `pipeline/config.py`. Les fichiers `prompts/*.txt` sont ignorés par git, afin qu'une consigne portant des extraits de documents ne puisse pas être versée par inadvertance.

La consigne adressée au modèle constitue un cahier des charges de description rédigé en langue naturelle, du même genre que les instructions dont les archivistes se dotent, à ceci près qu'elle est exécutable et par conséquent vérifiable. Celle qui a servi ici comportait sept ensembles de règles, dont voici la nature sans leur contenu.

1. **Le périmètre.** Ce qui doit être extrait, ce qui doit être ignoré, et l'interdiction de produire une valeur sans support dans le document.
2. **La fidélité.** La distinction entre la forme brute, qui enregistre ce que la fiche donne à lire, et la forme normalisée, qui enregistre l'interprétation, chaque champ concerné portant les deux.
3. **Les ratures et les annotations postérieures.** La distinction entre la correction locale, le grand trait de sortie de stock qui n'invalide pas les données barrées, et l'ajout tardif d'une autre main, qui relève de l'événement et non de la composition.
4. **La confiance.** L'obligation de produire un nombre entre 0 et 1 et de le moduler réellement. On notera que cette instruction n'a pas été suivie, les modèles se bornant à choisir parmi une poignée de valeurs conventionnelles.
5. **Les unités et les conventions d'écriture.** Poids, décimales, graphies concurrentes du gramme, unité nulle des pierres.
6. **Le tableau des matériaux.** Ce qu'est une ligne et ce qui n'en est pas une, point sur lequel le type de fiche le plus régulier échoue le plus, faute d'une définition assez explicite.
7. **La sortie.** Un JSON conforme au schéma, sans texte d'encadrement ni bloc de code.

Le sixième point est celui qui mérite le plus d'attention. C'est de son insuffisance que procède l'essentiel des erreurs mesurées.

---

## Installation

Python 3.13 recommandé, 3.11 suffit.

```bash
git clone https://github.com/flo-far/pipeline_chaumet.git
cd pipeline_chaumet
python3 -m venv venv
source venv/bin/activate        # Windows : venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env             # puis renseigner GEMINI_API_KEY
```

## Usage

La documentation d'installation prescrit une montée en charge en trois temps, prescription qui mérite d'être suivie parce qu'elle a été rédigée après avoir été apprise. Un nom de modèle erroné échoue cinquante fois de suite si l'on démarre par le corpus complet.

```bash
# 1. une seule fiche, pour vérifier que la chaîne complète fonctionne
python pipeline/run_pass.py --config passe_1 --cotes AHC-2016003-0075_004

# 2. un petit lot, pour exécuter le rapport et le contrôle de conformité
python pipeline/run_pass.py --config passe_1 --limite 5

# 3. le corpus complet
python pipeline/run_pass.py --config passe_1
```

Mesures, une fois la passe terminée :

```bash
python scoring/conformite.py --passe passe_1     # contrôle des règles structurelles
python scoring/compare.py    --passe passe_1     # écarts à la référence, par niveau
python scoring/metriques.py  --passe passe_1     # taux par champ et par type de fiche
python scoring/stabilite.py  --passe passe_1     # plancher de bruit entre répétitions
python scoring/couts.py                          # décomposition du coût, réflexion comprise
```

## Reproductibilité et traçabilité

Chaque exécution dépose un manifeste horodaté qui porte l'empreinte SHA-256 de la consigne, celle du schéma, la version exacte du modèle appelé, les paramètres de génération et les versions des bibliothèques employées. C'est la seule manière d'affirmer, plus tard, que deux passes ont bien été conduites dans les mêmes conditions.

Deux avertissements méritent d'être lus avant toute comparaison.

**Le déterminisme annoncé ne s'observe pas.** Température nulle, graine fixée, sortie contrainte, consigne et images rigoureusement identiques, et pourtant deux exécutions du même dispositif se séparent d'environ 1,65 point d'exactitude. Le décompte des tokens d'entrée, lui, est invariable à l'unité près. Il faut donc mesurer le plancher de bruit avant de comparer quoi que ce soit, ce à quoi sert `scoring/stabilite.py`, et tenir pour non avenu tout écart qui lui serait inférieur.

**Les tokens de raisonnement sont facturés.** Une première version de ce pipeline ne relevait que l'entrée et la sortie visible, d'où un coût calculé valant environ le quart du coût réel. `extract.py` relève désormais `thoughts_token_count` et `total_token_count`, et `scoring/couts.py` inclut la réflexion au tarif de sortie.

## Structure

```
pipeline_chaumet/
├── models.py                    modèle de données Pydantic
├── pipeline/
│   ├── config.py                chemins, passes, tarifs, paramètres de génération
│   ├── extract.py               traitement d'une fiche
│   └── run_pass.py              conduite d'une campagne
├── schema/                      schéma de la vérité terrain, gabarit vide
├── scoring/                     les neuf outils de mesure
├── prompts/                     (vide) à renseigner, voir « Fournir une consigne »
├── images/                      (vide) images d'archives, non publiées
├── fiches/                      (vide) vérité terrain, non publiée
├── referentiels/                (vide) référentiels internes, non publiés
└── outputs/                     (vide) sorties d'extraction, non publiées
```

## Licence

Code et schéma sous licence MIT, voir `LICENSE`.

## Citer ce dépôt

Voir `CITATION.cff`.

---

*Ce dépôt accompagne un mémoire dont il ne reprend ni les résultats ni les données. Les chiffres cités ici le sont à titre d'avertissement méthodologique et sont établis dans le mémoire lui-même.*
