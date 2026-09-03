"""Modèles Pydantic miroir de schema/schema_verite_terrain.json.

Utilisés à la fois pour valider la sortie du pipeline d'extraction et pour
parcourir une fiche de façon structurée dans le module de scoring. Toute
modification doit être répercutée dans schema/schema_verite_terrain.json
(et réciproquement) : les deux fichiers doivent rester en phase.
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

DATE_ISO_PATTERN = r"^\d{4}-\d{2}-\d{2}$"
COTE_ARCHIVE_PATTERN = r"^AHC-\d{7}-\d{4}_\d{3}$"


class FicheModel(BaseModel):
    """Base commune : interdit toute clé non prévue par le schéma."""

    model_config = ConfigDict(extra="forbid")


class ChampTexte(FicheModel):
    valeur: str | None = None
    confiance: float | None = Field(default=None, ge=0, le=1)


class ChampDate(FicheModel):
    valeur_brute: str | None = None
    valeur_normalisee: str | None = Field(default=None, pattern=DATE_ISO_PATTERN)
    confiance: float | None = Field(default=None, ge=0, le=1)


class Enrichissements(FicheModel):
    referentiel_materiaux: bool
    referentiel_abreviations: bool
    index_scripteurs: bool
    few_shot: bool


class Metadata(FicheModel):
    cote_archive: str = Field(pattern=COTE_ARCHIVE_PATTERN)
    # type_fiche et photographie sont obligatoires dans la vérité terrain, mais
    # tolérés à null ici : une extraction par ailleurs correcte ne doit pas être
    # perdue en totalité parce que le modèle n'a pas su déterminer l'un de ces
    # deux champs. Leur présence est mesurée par scoring/conformite.py, qui
    # signale sans rejeter. Le schéma JSON reste strict pour la vérité terrain.
    type_fiche: int | None = Field(default=None, ge=1)
    photographie: bool | None = None
    modele: str | None = None
    enrichissements: Enrichissements
    tokens_entree: int | None = Field(default=None, ge=0)
    tokens_sortie: int | None = Field(default=None, ge=0)
    # Les modèles à raisonnement produisent, avant leur réponse, des tokens dits
    # de « réflexion » qui n'entrent PAS dans candidates_token_count et sont
    # pourtant facturés au tarif de sortie. Les ignorer a fait sous-estimer le
    # coût de la première campagne d'un facteur quatre environ. On relève donc
    # aussi le total publié par l'API : total = entrée + sortie + réflexion
    # (+ tokens d'outils, non employés ici). Règle générale : préférer le total
    # du fournisseur à une somme reconstituée, l'écart entre les deux mesurant
    # exactement ce que l'on ignorait.
    # Optionnels : les extractions antérieures au correctif ne les portent pas.
    tokens_raisonnement: int | None = Field(default=None, ge=0)
    tokens_total: int | None = Field(default=None, ge=0)


class NumeroStock(FicheModel):
    valeur: int | str | None = None
    confiance: float | None = Field(default=None, ge=0, le=1)


class Identification(FicheModel):
    numero_stock: NumeroStock
    code_fabrication: ChampTexte


class Dates(FicheModel):
    """Dates relevant de l'entrée en stock, à l'exclusion de toute autre.

    L'encart supérieur droit porte souvent la même date deux fois, sous forme
    dactylographiée et manuscrite. La dactylographiée fait foi. La manuscrite
    n'est enregistrée que si elle indique une date différente.

    `dates_alternatives` est conservé bien qu'il soit vide sur l'ensemble du
    corpus du benchmark : le schéma décrit ce que l'archive peut porter, non ce
    que ces cinquante fiches contiennent. Son absence de valeur devient ainsi un
    constat mesuré et non un silence de conception (voir §3.5 et §6.2.3).
    """

    date_entree_stock: ChampDate
    dates_alternatives: list[ChampDate] = Field(default_factory=list)


class PoidsBrutTotal(FicheModel):
    valeur_brute: str | None = None
    valeur_normalisee: float | None = None
    unite: str | None = None
    confiance: float | None = Field(default=None, ge=0, le=1)


class Materiau(FicheModel):
    libelle: str | None = None
    matiere_normalisee: str | None = None
    date_brute: str | None = None
    date_normalisee: str | None = Field(default=None, pattern=DATE_ISO_PATTERN)
    quantite: int | None = Field(default=None, ge=0)
    poids: str | None = None
    poids_normalise: float | None = None
    unite: str | None = None
    type: str | None = None
    confiance: float | None = Field(default=None, ge=0, le=1)


class Client(FicheModel):
    valeur: str | None = None
    valeur_normalisee: str | None = None
    confiance: float | None = Field(default=None, ge=0, le=1)


class Vente(FicheModel):
    client: Client
    date_vente: ChampDate


class Mentions(FicheModel):
    liste: list[ChampTexte] = Field(default_factory=list)


class FicheChaumet(FicheModel):
    """Modèle racine — une fiche de stock complète (vérité terrain ou extraction)."""

    metadata: Metadata
    identification: Identification
    dates: Dates
    description: ChampTexte
    poids_brut_total: PoidsBrutTotal
    materiaux: list[Materiau] = Field(default_factory=list)
    vente: Vente
    mentions: Mentions
