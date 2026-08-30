"""Extraction Gemini pour une fiche : charge la ou les image(s), appelle
l'API, parse et valide la réponse contre models.FicheChaumet.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import httpx
from google import genai
from google.genai import types
from pydantic import ValidationError
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

import config
from models import FicheChaumet


class ExtractionError(Exception):
    """Levée quand l'extraction échoue de façon non transitoire (JSON invalide,
    ou structure ne validant pas le modèle) — distincte des erreurs réseau
    transitoires que tenacity réessaie."""


@dataclass
class ExtractionResult:
    cote_archive: str
    fiche: FicheChaumet | None
    raw_text: str
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.fiche is not None


def creer_client(timeout_ms: int = 240_000) -> genai.Client:
    """Construit un client Gemini, à créer une fois par passe et à réutiliser.

    Le délai d'attente est explicite : sans lui, une requête qui n'aboutit pas
    peut suspendre la passe indéfiniment, sans le moindre message. Mieux vaut un
    échec net au bout de quelques minutes qu'un silence sans fin.

    Attention au cycle de vie : `Client.__del__` ferme la connexion HTTP
    sous-jacente. Un client temporaire (`genai.Client(...).models.list()`) peut
    donc être détruit avant que la requête ne parte, avec l'erreur « Cannot send
    a request, as the client has been closed ». Il faut toujours le garder dans
    une variable pendant toute la durée des appels.
    """
    if not config.GEMINI_API_KEY:
        raise RuntimeError(
            "GEMINI_API_KEY absente. Copie .env.example vers .env et renseigne ta clé."
        )
    return genai.Client(
        api_key=config.GEMINI_API_KEY,
        http_options=types.HttpOptions(timeout=timeout_ms),
    )


def charger_images(cote_archive: str, images_dir: Path = config.IMAGES_DIR) -> list[Path]:
    """Résout les images d'une cote : recto (_1) et verso (_2) s'il existe."""
    chemins = []
    for suffixe in ("_1", "_2"):
        candidat = images_dir / f"{cote_archive}{suffixe}.jpg"
        if candidat.exists():
            chemins.append(candidat)
    if not chemins:
        raise FileNotFoundError(f"Aucune image trouvée pour {cote_archive} dans {images_dir}")
    return chemins


def _nettoyer_json(texte: str) -> str:
    """Retire les éventuelles balises ```json ... ``` autour de la réponse."""
    brut = texte.strip()
    if brut.startswith("```"):
        brut = brut.split("```")[1]
        if brut.startswith("json"):
            brut = brut[4:]
    return brut.strip()


def _est_transitoire(exc: BaseException) -> bool:
    """Décide si une exception mérite un réessai.

    Sont transitoires : l'indisponibilité serveur, le dépassement de quota, et
    surtout les incidents de transport — connexion réinitialisée, délai
    dépassé, coupure en cours de lecture. Ces derniers sont les plus fréquents
    sur une passe longue : téléverser cinquante fois plusieurs mégaoctets suffit
    à ce qu'une connexion tombe au moins une fois, sans que rien ne soit en
    cause du côté de la requête.

    Ne sont pas transitoires les erreurs 400 ou 403 (requête malformée, clé
    invalide) : les réessayer ne ferait que retarder un échec certain, sur
    chacune des fiches.
    """
    if isinstance(exc, genai.errors.ServerError):
        return True
    if isinstance(exc, genai.errors.ClientError):
        return getattr(exc, "code", None) == 429  # quota / rate limit
    if isinstance(exc, httpx.TransportError):
        return True  # ConnectError, ReadError, RemoteProtocolError, timeouts…
    # ConnectionResetError (« Connection reset by peer ») dérive d'OSError et
    # peut remonter sans être enveloppée par httpx.
    return isinstance(exc, OSError)


def est_fatal(exc: BaseException) -> bool:
    """Vrai si l'erreur se reproduira à l'identique sur toutes les fiches.

    Un modèle retiré, une clé refusée ou une requête malformée ne dépendent pas
    de la fiche traitée : insister cinquante fois ne fait que perdre du temps et
    noyer la cause dans une avalanche de messages identiques. La passe doit
    s'interrompre au premier cas.
    """
    return isinstance(exc, genai.errors.ClientError) and getattr(exc, "code", None) in (
        400,  # requête malformée (paramètre non supporté par ce modèle)
        401,  # clé absente ou invalide
        403,  # accès refusé
        404,  # modèle inconnu ou retiré
    )


def _texte_reponse(response) -> str:
    """Extrait le texte d'une réponse, en distinguant les cas où il n'y en a pas.

    `response.text` vaut None lorsque la génération a été interrompue (sortie
    tronquée, contenu bloqué) : y accéder directement produirait une
    AttributeError opaque au lieu d'un échec documenté.
    """
    candidats = getattr(response, "candidates", None) or []
    if not candidats:
        motif = getattr(getattr(response, "prompt_feedback", None), "block_reason", None)
        raise ExtractionError(f"Réponse sans candidat (requête bloquée ? motif : {motif!r})")
    texte = getattr(response, "text", None)
    if not texte:
        fin = getattr(candidats[0], "finish_reason", None)
        raise ExtractionError(
            f"Réponse sans texte exploitable (finish_reason={fin!r}) — "
            "MAX_TOKENS signale une sortie tronquée, SAFETY un blocage de contenu."
        )
    return texte


def _signaler_reessai(etat) -> None:
    """Rend les réessais visibles, avec leur cause.

    Sans cela, une passe qui retente une requête reste muette plusieurs minutes
    et paraît figée — et l'on ne sait pas si le serveur est lent, saturé ou
    absent, alors que ces trois cas appellent des réponses différentes.
    """
    exc = etat.outcome.exception()
    code = getattr(exc, "code", None)
    detail = f"{type(exc).__name__}" + (f" {code}" if code else "")
    attente = getattr(etat.next_action, "sleep", 0)
    print(f"\n      tentative {etat.attempt_number} échouée ({detail}) — "
          f"nouvelle tentative dans {attente:.0f}s…", end=" ", flush=True)


# Cinq tentatives et une attente pouvant aller jusqu'à deux minutes : une
# saturation de modèle (503) ne se résorbe pas en quelques secondes, contrairement
# à une coupure réseau. Trois tentatives espacées de deux à trente secondes,
# comme initialement, ne laissaient aucune chance à ce cas.
@retry(
    reraise=True,
    stop=stop_after_attempt(5),
    wait=wait_exponential(multiplier=4, min=5, max=120),
    retry=retry_if_exception(_est_transitoire),
    before_sleep=_signaler_reessai,
)
def _appeler_gemini(
    client: genai.Client,
    model: str,
    image_paths: list[Path],
    prompt: str,
    generation: dict | None = None,
):
    parts = [
        types.Part.from_bytes(data=p.read_bytes(), mime_type="image/jpeg") for p in image_paths
    ]
    parts.append(types.Part.from_text(text=prompt))
    return client.models.generate_content(
        model=model,
        contents=parts,
        config=types.GenerateContentConfig(**(generation or {})),
    )


def extraire_fiche(
    cote_archive: str,
    prompt_path: Path,
    model: str,
    enrichissements: dict[str, bool],
    images_dir: Path = config.IMAGES_DIR,
    client: genai.Client | None = None,
    generation: dict | None = None,
) -> ExtractionResult:
    """Extrait une fiche complète (recto + verso si présent) en un seul appel."""
    client = client or creer_client()
    image_paths = charger_images(cote_archive, images_dir)
    prompt = prompt_path.read_text(encoding="utf-8")

    response = _appeler_gemini(client, model, image_paths, prompt, generation)
    try:
        raw = _nettoyer_json(_texte_reponse(response))
    except ExtractionError as e:
        return ExtractionResult(cote_archive=cote_archive, fiche=None, raw_text="", error=str(e))

    # raw_decode plutôt que loads : il s'arrête à la fin du premier document
    # JSON complet et ignore ce qui suit. Certains modèles concatènent une
    # seconde réponse ou du texte résiduel, ce que loads rejette en bloc
    # (« Extra data ») alors que la première réponse est parfaitement valide.
    try:
        data, fin = json.JSONDecoder().raw_decode(raw)
    except json.JSONDecodeError as e:
        return ExtractionResult(
            cote_archive=cote_archive,
            fiche=None,
            raw_text=raw,
            error=f"JSON invalide : {e}",
        )
    residu = raw[fin:].strip()

    # Le mode JSON natif garantit un JSON valide, pas qu'il soit un objet : le
    # modèle peut encapsuler sa réponse dans un tableau. Un tableau d'un seul
    # élément se déballe sans perte ; tout autre cas est un échec propre plutôt
    # qu'une AttributeError opaque au milieu de la passe.
    if isinstance(data, list):
        if len(data) == 1 and isinstance(data[0], dict):
            data = data[0]
        else:
            return ExtractionResult(
                cote_archive=cote_archive,
                fiche=None,
                raw_text=raw,
                error=(
                    f"Réponse JSON valide mais de type inattendu : tableau de "
                    f"{len(data)} élément(s) au lieu d'un objet."
                ),
            )
    if not isinstance(data, dict):
        return ExtractionResult(
            cote_archive=cote_archive,
            fiche=None,
            raw_text=raw,
            error=f"Réponse JSON de type inattendu : {type(data).__name__}.",
        )

    # Champs déterministes que le modèle ne doit pas deviner : le pipeline
    # les impose après coup plutôt que de faire confiance à la sortie du modèle.
    data.setdefault("metadata", {})
    data["metadata"]["cote_archive"] = cote_archive
    data["metadata"]["modele"] = model
    data["metadata"]["enrichissements"] = enrichissements
    usage = getattr(response, "usage_metadata", None)
    data["metadata"]["tokens_entree"] = getattr(usage, "prompt_token_count", None)
    data["metadata"]["tokens_sortie"] = getattr(usage, "candidates_token_count", None)
    # candidates_token_count ne compte que la sortie VISIBLE. Les tokens de
    # raisonnement sont exposés à part et facturés au tarif de sortie ; le total
    # publié par l'API les inclut. Voir models.Metadata.
    data["metadata"]["tokens_raisonnement"] = getattr(usage, "thoughts_token_count", None)
    data["metadata"]["tokens_total"] = getattr(usage, "total_token_count", None)

    try:
        fiche = FicheChaumet.model_validate(data)
    except ValidationError as e:
        # Les mentions ne sont pas notées : perdre une fiche entière parce que
        # le modèle y a mal nommé une clé serait absurde. Si toutes les erreurs
        # y sont confinées, on les écarte et l'on garde le reste, en le
        # signalant. Une erreur ailleurs reste bloquante.
        if e.error_count() and all(
            err["loc"] and err["loc"][0] == "mentions" for err in e.errors()
        ):
            data["mentions"] = {"liste": []}
            try:
                fiche = FicheChaumet.model_validate(data)
            except ValidationError:
                pass
            else:
                return ExtractionResult(
                    cote_archive=cote_archive,
                    fiche=fiche,
                    raw_text=raw,
                    error=(
                        f"Mentions écartées ({e.error_count()} clés non conformes) ; "
                        f"le reste de la fiche est conservé."
                    ),
                )
        return ExtractionResult(
            cote_archive=cote_archive,
            fiche=None,
            raw_text=json.dumps(data, ensure_ascii=False, indent=2),
            error=f"Validation Pydantic échouée : {e}",
        )

    return ExtractionResult(
        cote_archive=cote_archive,
        fiche=fiche,
        raw_text=raw,
        error=(f"Contenu superflu ignoré après le JSON ({len(residu)} caractères)."
               if residu else None),
    )
