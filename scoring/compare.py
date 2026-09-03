"""Taxonomie d'erreur à 5 niveaux (voir §3.5 du document projet) appliquée
champ par champ entre une extraction et sa vérité terrain.

Simplifications assumées pour cette première version (à affiner plus tard
si besoin) :
- Le sous-arbre "metadata" est exclu de la comparaison : cote_archive,
  modele, tokens et enrichissements sont injectés par le pipeline, pas
  extraits de l'image ; type_fiche/photographie pourraient être ajoutés
  plus tard s'ils s'avèrent utiles à mesurer.
- Les clés "confiance" ne sont jamais comparées : la vérité terrain ne
  les renseigne pas (toujours null), ce n'est pas une valeur à évaluer.
- Les tableaux (materiaux, dates_alternatives, mentions.liste) sont
  appariés par similarité de contenu, jamais par position : le modèle
  n'a aucune raison de restituer ses lignes dans l'ordre de la vérité
  terrain. L'ordre n'est donc pas pénalisé, seules les inversions sont
  comptées et reportées. Un élément de la vérité terrain sans
  correspondant est une lacune, un élément extrait en trop une
  hallucination : dans les deux cas niveau 4.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import unicodedata

from rapidfuzz.distance import Levenshtein

NIVEAU_LABELS = {
    0: "Correspondance exacte",
    1: "Erreur bénigne",
    2: "Erreur graphique",
    3: "Erreur sémantique",
    4: "Erreur critique",
}

CLES_IGNOREES = {"confiance"}
SOUS_ARBRES_IGNORES = {"metadata"}

# Deux compétences distinctes sont mesurées et ne doivent pas être agrégées :
# la LECTURE de l'archive (déchiffrer ce qui est écrit, verbatim) et la
# NORMALISATION (appliquer une règle d'écriture à ce qui a été lu). Un
# enrichissement de type référentiel n'agit que sur la seconde, un modèle plus
# capable surtout sur la première. Voir §3.6 du document projet.
CHAMPS_LECTURE = {
    "libelle",
    "date_brute",
    "quantite",
    "poids",
    "valeur",
    "valeur_brute",
}
CHAMPS_NORMALISATION = {
    "matiere_normalisee",
    "date_normalisee",
    "poids_normalise",
    "unite",
    "type",
    "valeur_normalisee",
}


# Sous-arbres relevant d'une analyse qualitative et non d'un score. Les
# mentions n'ont pas de périmètre définissable a priori : le prompt laisse au
# modèle le soin de juger ce qui mérite d'être relevé. Les compter en erreur
# reviendrait à sanctionner une liberté qu'on lui a explicitement accordée.
# Elles restent extraites, exportées et consultables, mais hors des deux taux.
SOUS_ARBRES_QUALITATIFS = {"mentions"}


def nature_champ(champ: str) -> str:
    """Classe un chemin de champ en 'lecture', 'normalisation' ou 'qualitatif'."""
    racine = champ.split(".", 1)[0].split("[", 1)[0]
    if racine in SOUS_ARBRES_QUALITATIFS:
        return "qualitatif"
    feuille = champ.rsplit(".", 1)[-1]
    if feuille in CHAMPS_LECTURE:
        return "lecture"
    if feuille in CHAMPS_NORMALISATION:
        return "normalisation"
    return "autre"


@dataclass
class Ecart:
    champ: str
    niveau: int
    valeur_gt: Any
    valeur_extraite: Any

    @property
    def label(self) -> str:
        return NIVEAU_LABELS[self.niveau]

    @property
    def nature(self) -> str:
        return nature_champ(self.champ)


def _normaliser(s: str) -> str:
    """Espace, casse, ponctuation — pour détecter les écarts de niveau 1."""
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
    s = s.lower().strip()
    s = "".join(ch for ch in s if ch.isalnum() or ch.isspace())
    return " ".join(s.split())


def _classifier_valeurs(gt: Any, extrait: Any) -> int:
    if gt is None and extrait is None:
        return 0
    if gt is None or extrait is None:
        return 4  # hallucination (gt null, extrait rempli) ou lacune non signalée (inverse)

    if isinstance(gt, bool) or isinstance(extrait, bool):
        return 0 if gt == extrait else 3

    if isinstance(gt, (int, float)) and isinstance(extrait, (int, float)):
        return 0 if gt == extrait else 3

    gt_s, ext_s = str(gt), str(extrait)
    if gt_s == ext_s:
        return 0
    if _normaliser(gt_s) == _normaliser(ext_s):
        return 1
    distance = Levenshtein.distance(_normaliser(gt_s), _normaliser(ext_s))
    if distance <= 2:
        return 2
    return 3


def _aplatir(obj: Any, prefixe: str = "") -> dict[str, Any]:
    """Aplatit récursivement un dict/list JSON en {chemin: valeur_scalaire}."""
    resultat: dict[str, Any] = {}
    if isinstance(obj, dict):
        for cle, val in obj.items():
            if cle in CLES_IGNOREES:
                continue
            chemin = f"{prefixe}.{cle}" if prefixe else cle
            racine = chemin.split(".", 1)[0].split("[", 1)[0]
            if racine in SOUS_ARBRES_IGNORES:
                continue
            resultat.update(_aplatir(val, chemin))
    elif isinstance(obj, list):
        for i, val in enumerate(obj):
            resultat.update(_aplatir(val, f"{prefixe}[{i}]"))
    else:
        resultat[prefixe] = obj
    return resultat


def confiances_par_champ(fiche: dict) -> dict[str, Any]:
    """Associe à chaque chemin de champ la confiance déclarée par son bloc parent.

    Le schéma porte un score de confiance par bloc (une ligne de matériau, un
    champ date, une description), pas par champ scalaire : tous les champs d'un
    même bloc héritent donc de la même valeur. À n'appeler que sur une
    extraction — la vérité terrain laisse ces scores à null par convention
    (voir §3.6.2 du document projet).
    """

    def _walk(obj: Any, prefixe: str = "", courante: Any = None) -> dict[str, Any]:
        resultat: dict[str, Any] = {}
        if isinstance(obj, dict):
            courante = obj.get("confiance", courante)
            for cle, val in obj.items():
                if cle in CLES_IGNOREES:
                    continue
                chemin = f"{prefixe}.{cle}" if prefixe else cle
                if chemin.split(".", 1)[0].split("[", 1)[0] in SOUS_ARBRES_IGNORES:
                    continue
                resultat.update(_walk(val, chemin, courante))
        elif isinstance(obj, list):
            for i, val in enumerate(obj):
                resultat.update(_walk(val, f"{prefixe}[{i}]", courante))
        else:
            resultat[prefixe] = courante
        return resultat

    return _walk(fiche)


# --------------------------------------------------------------------------
# Appariement des tableaux
#
# Les tableaux (materiaux, dates_alternatives, mentions.liste) ne peuvent pas
# être comparés position par position : le modèle n'a aucune raison de produire
# ses lignes dans l'ordre exact de la vérité terrain, et une seule ligne omise
# décalerait tout le tableau, transformant une extraction correcte en série
# d'erreurs. Les éléments sont donc appariés par similarité de contenu, l'ordre
# n'étant pas pénalisé — seul le nombre d'inversions est reporté (§3.6.4).
# --------------------------------------------------------------------------

# Par tableau : clés de texte comparées en similarité, clés comparées à
# l'identique (elles départagent deux lignes de libellé proche, ex. deux lignes
# "D" de poids différents).
#
# Les clés exactes sont volontairement les formes NORMALISÉES et non les formes
# verbatim. Apparier n'est pas scorer : pour rapprocher deux lignes il faut les
# identifiants les plus stables, alors que les champs bruts sont précisément les
# plus sensibles aux variations d'écriture. Un poids transcrit ",48" d'un côté
# et "48" de l'autre désigne le même diamant — poids_normalise vaut 0.48 des
# deux côtés et les rapproche, après quoi la comparaison verbatim peut faire son
# travail et signaler l'écart de transcription là où il est.
CLES_APPARIEMENT: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "materiaux": (
        ("libelle", "matiere_normalisee"),
        ("poids_normalise", "quantite", "date_normalisee"),
    ),
    "dates.dates_alternatives": (("valeur_brute",), ("valeur_normalisee",)),
    "mentions.liste": (("valeur",), ()),
}

# En dessous de ce score, deux éléments ne sont pas considérés comme se
# correspondre : mieux vaut une lacune et une hallucination franches qu'un
# appariement arbitraire qui masquerait les deux.
SEUIL_APPARIEMENT = 0.45


def _similarite(a: dict, b: dict, cles_texte: tuple[str, ...], cles_exactes: tuple[str, ...]) -> float:
    texte = 0.0
    for cle in cles_texte:
        va, vb = a.get(cle), b.get(cle)
        if va is None and vb is None:
            texte += 1.0
        elif va is not None and vb is not None:
            texte += Levenshtein.normalized_similarity(_normaliser(str(va)), _normaliser(str(vb)))
    texte /= len(cles_texte) or 1

    if not cles_exactes:
        return texte
    # Texte et clés exactes pèsent également. Les clés exactes — poids
    # normalisé, quantité, date — sont au moins aussi discriminantes qu'un
    # libellé souvent réduit à une lettre ("D", "S") ou mal découpé par le
    # modèle. Les sous-pondérer empêchait d'apparier deux lignes désignant
    # manifestement la même matière au seul motif que leur libellé différait.
    exact = sum(a.get(c) == b.get(c) for c in cles_exactes) / len(cles_exactes)
    return 0.5 * texte + 0.5 * exact


def _apparier(gt_liste: list, ext_liste: list, cles: tuple) -> tuple[dict[int, int], int]:
    """Apparie deux tableaux par similarité décroissante (glouton).

    Retourne la correspondance {index vérité terrain: index extraction} et le
    nombre d'inversions d'ordre entre les couples appariés.
    """
    cles_texte, cles_exactes = cles
    candidats = sorted(
        (
            (_similarite(g, e, cles_texte, cles_exactes), i, j)
            for i, g in enumerate(gt_liste)
            for j, e in enumerate(ext_liste)
            if isinstance(g, dict) and isinstance(e, dict)
        ),
        key=lambda t: (-t[0], t[1], t[2]),
    )
    correspondance: dict[int, int] = {}
    pris_ext: set[int] = set()
    for score, i, j in candidats:
        if score < SEUIL_APPARIEMENT:
            break
        if i in correspondance or j in pris_ext:
            continue
        correspondance[i] = j
        pris_ext.add(j)

    couples = sorted(correspondance.items())
    inversions = sum(
        1
        for a in range(len(couples))
        for b in range(a + 1, len(couples))
        if couples[a][1] > couples[b][1]
    )
    return correspondance, inversions


def _liste_alignee(gt_liste: list, ext_liste: list, cles: tuple) -> tuple[list, dict[int, int], int]:
    """Réordonne le tableau extrait pour le faire coïncider avec celui de la VT.

    Les positions de la VT sans correspondant reçoivent un élément entièrement
    nul (lacune) ; les éléments extraits surnuméraires sont ajoutés à la suite
    (hallucination). La comparaison champ à champ classique s'applique ensuite
    sans modification.
    """
    correspondance, inversions = _apparier(gt_liste, ext_liste, cles)
    aligne: list = []
    for i, element_gt in enumerate(gt_liste):
        j = correspondance.get(i)
        if j is not None:
            aligne.append(ext_liste[j])
        elif isinstance(element_gt, dict):
            aligne.append({cle: None for cle in element_gt})
        else:
            aligne.append(None)
    for j, element in enumerate(ext_liste):
        if j not in set(correspondance.values()):
            aligne.append(element)
    return aligne, correspondance, inversions


def aligner_extraction(verite_terrain: dict, extraction: dict) -> tuple[dict, dict, dict]:
    """Renvoie l'extraction réalignée sur la VT, les correspondances et les inversions."""
    aligne = json_copie(extraction)
    correspondances: dict[str, dict[int, int]] = {}
    inversions: dict[str, int] = {}
    for chemin, cles in CLES_APPARIEMENT.items():
        conteneur_gt, conteneur_ext = verite_terrain, aligne
        *parents, feuille = chemin.split(".")
        for cle in parents:
            conteneur_gt = (conteneur_gt or {}).get(cle) or {}
            conteneur_ext = (conteneur_ext or {}).get(cle) or {}
        liste_gt = conteneur_gt.get(feuille) or []
        liste_ext = conteneur_ext.get(feuille) or []
        if not liste_gt and not liste_ext:
            continue
        conteneur_ext[feuille], correspondances[chemin], inversions[chemin] = _liste_alignee(
            liste_gt, liste_ext, cles
        )
    return aligne, correspondances, inversions


def json_copie(obj: Any) -> Any:
    """Copie profonde suffisante pour des structures JSON (dict/list/scalaires)."""
    if isinstance(obj, dict):
        return {c: json_copie(v) for c, v in obj.items()}
    if isinstance(obj, list):
        return [json_copie(v) for v in obj]
    return obj


@dataclass
class Comparaison:
    ecarts: list[Ecart]
    correspondances: dict[str, dict[int, int]]
    inversions: dict[str, int]

    def index_extraction(self, champ: str) -> int | None:
        """Index d'origine, dans l'extraction, de l'élément comparé pour ce champ.

        Permet de vérifier à la main que l'appariement retenu était le bon.
        """
        match = re.match(r"([\w.]+)\[(\d+)\]", champ)
        if not match:
            return None
        return self.correspondances.get(match.group(1), {}).get(int(match.group(2)))


def comparer(verite_terrain: dict, extraction: dict) -> Comparaison:
    """Compare deux fiches après appariement des tableaux."""
    aligne, correspondances, inversions = aligner_extraction(verite_terrain, extraction)
    gt_plat = _aplatir(verite_terrain)
    ext_plat = _aplatir(aligne)

    ecarts = []
    for champ in sorted(set(gt_plat) | set(ext_plat)):
        gt_val = gt_plat.get(champ)
        ext_val = ext_plat.get(champ)
        ecarts.append(
            Ecart(
                champ=champ,
                niveau=_classifier_valeurs(gt_val, ext_val),
                valeur_gt=gt_val,
                valeur_extraite=ext_val,
            )
        )
    return Comparaison(ecarts=ecarts, correspondances=correspondances, inversions=inversions)


def comparer_fiches(verite_terrain: dict, extraction: dict) -> list[Ecart]:
    """Compatibilité : ne renvoie que la liste des écarts."""
    return comparer(verite_terrain, extraction).ecarts
