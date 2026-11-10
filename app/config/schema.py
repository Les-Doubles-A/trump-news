"""Schéma de configuration validé (Pydantic)."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
STORAGE_SUBDIRS = ("articles", "scripts", "audio", "images", "videos", "thumbnails", "subtitles", "logs")


def _check_tz(value: str) -> str:
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError(f"fuseau horaire inconnu : '{value}' (ex. Europe/Paris)") from None
    return value


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AppSection(_Base):
    name: str = "Trump News"
    timezone: str = "Europe/Paris"
    language: Literal["en", "fr", "es"] = "en"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    _tz = field_validator("timezone")(_check_tz)


class LLMSection(_Base):
    provider: str = "lmstudio"
    endpoint: str = "http://localhost:1234"
    model: str = "auto"
    timeout_s: float = Field(120, gt=0)
    retries: int = Field(3, ge=1, le=10)
    temperature: float = Field(0.3, ge=0, le=2)
    max_tokens: int = Field(2048, ge=16)

    @field_validator("endpoint")
    @classmethod
    def _endpoint(cls, v: str) -> str:
        if not re.match(r"^https?://[^\s/]+", v):
            raise ValueError("l'endpoint doit commencer par http:// ou https:// (ex. http://localhost:1234)")
        return v


class StorageSection(_Base):
    data_dir: Path = Path("data")
    database: str = "trump_news.db"

    def resolve(self, base: Path) -> Path:
        d = self.data_dir
        return d if d.is_absolute() else (base / d)

    def database_path(self, base: Path) -> Path:
        db = Path(self.database)
        return db if db.is_absolute() else self.resolve(base) / db

    def ensure_dirs(self, base: Path) -> Path:
        root = self.resolve(base)
        for sub in STORAGE_SUBDIRS:
            (root / sub).mkdir(parents=True, exist_ok=True)
        return root


class NewsProviderSpec(_Base):
    type: Literal["rss", "google_news", "gdelt"]
    name: str
    enabled: bool = True
    url: str | None = None      # rss
    query: str | None = None    # google_news, gdelt
    language: str = "en"
    country: str = "US"

    @model_validator(mode="after")
    def _required_fields(self) -> "NewsProviderSpec":
        if self.type == "rss" and not self.url:
            raise ValueError(f"'{self.name}' : le champ 'url' est requis pour un fournisseur rss")
        if self.type in ("google_news", "gdelt") and not self.query:
            raise ValueError(f"'{self.name}' : le champ 'query' est requis pour {self.type}")
        return self


class ScoringSection(_Base):
    """Poids du score d'un sujet. Tous modifiables. Le score final est ramené entre 0 et 1."""

    w_importance: float = Field(1.0, ge=0)
    w_recency: float = Field(1.2, ge=0)
    w_source_count: float = Field(1.0, ge=0)
    w_relevance: float = Field(1.5, ge=0)
    w_video_potential: float = Field(0.8, ge=0)
    w_duplicates: float = Field(0.4, ge=0)       # pénalité
    w_uncertainty: float = Field(0.6, ge=0)      # pénalité
    w_low_quality: float = Field(0.8, ge=0)      # pénalité
    half_life_hours: float = Field(12, gt=0)
    breaking_terms: list[str] = [
        "breaking", "just in", "announces", "signs", "orders", "executive order", "fires", "indict",
        "ruling", "rules", "arrest", "resign", "veto", "pardon", "sanction", "deploy", "strike", "impeach",
    ]
    uncertainty_terms: list[str] = [
        "reportedly", "rumor", "rumour", "unconfirmed", "allegedly", "alleged", "sources say", "claims",
        "could", "may ", "might", "speculat", "unverified", "according to anonymous",
    ]
    video_terms: list[str] = [
        "says", "slams", "warns", "vows", "threatens", "announces", "signs", "fires", "reveals", "blasts",
        "responds", "orders", "shocks", "refuses", "demands",
    ]
    high_impact_categories: list[str] = ["white_house", "justice", "international", "economy", "elections"]


DEFAULT_PROVIDERS = [
    NewsProviderSpec(type="google_news", name="Google News - Trump", query="Donald Trump"),
    NewsProviderSpec(type="rss", name="BBC US & Canada", url="https://feeds.bbci.co.uk/news/world/us_and_canada/rss.xml"),
    NewsProviderSpec(type="rss", name="NPR Politics", url="https://feeds.npr.org/1014/rss.xml"),
    NewsProviderSpec(type="rss", name="The Guardian US Politics", url="https://www.theguardian.com/us-news/us-politics/rss"),
    NewsProviderSpec(type="rss", name="Politico", url="https://rss.politico.com/politics-news.xml"),
    NewsProviderSpec(type="rss", name="Fox News Politics", url="https://moxie.foxnews.com/google-publisher/politics.xml"),
    # Flux directs : fournissent un résumé (et un lien téléchargeable), contrairement à Google News (titre seul)
    NewsProviderSpec(type="rss", name="New York Times Politics", url="https://rss.nytimes.com/services/xml/rss/nyt/Politics.xml"),
    NewsProviderSpec(type="rss", name="Washington Post Politics", url="https://feeds.washingtonpost.com/rss/politics"),
    NewsProviderSpec(type="rss", name="PBS NewsHour Politics", url="https://www.pbs.org/newshour/feeds/rss/politics"),
    NewsProviderSpec(type="rss", name="CNBC Politics", url="https://www.cnbc.com/id/10000113/device/rss/rss.html"),
    NewsProviderSpec(type="rss", name="Al Jazeera", url="https://www.aljazeera.com/xml/rss/all.xml"),
    NewsProviderSpec(type="rss", name="NBC News Politics", url="https://feeds.nbcnews.com/nbcnews/public/politics"),
    NewsProviderSpec(type="rss", name="CBS News Politics", url="https://www.cbsnews.com/latest/rss/politics"),
    NewsProviderSpec(type="rss", name="ABC News Politics", url="https://abcnews.go.com/abcnews/politicsheadlines"),
    NewsProviderSpec(type="rss", name="The Hill", url="https://thehill.com/homenews/feed/"),
]

_DEFAULT_RELIABILITY = {
    "reuters": 0.92, "ap": 0.92, "bbc": 0.88, "npr": 0.85, "nytimes": 0.84, "washingtonpost": 0.84,
    "wsj": 0.84, "bloomberg": 0.85, "guardian": 0.8, "politico": 0.8, "axios": 0.8, "nbcnews": 0.8,
    "abcnews": 0.8, "cbsnews": 0.8, "cnn": 0.78, "foxnews": 0.78, "thehill": 0.75, "newsweek": 0.6,
    "whitehouse": 0.9, "pbs": 0.85, "dw": 0.8, "aljazeera": 0.75, "latimes": 0.8, "usatoday": 0.78,
    "economist": 0.85, "cnbc": 0.8, "forbes": 0.65,
}
_DEFAULT_ALIASES = {
    "apnews": "ap", "associatedpress": "ap", "bbc": "bbc", "nytimes": "nytimes", "newyorktimes": "nytimes",
    "washingtonpost": "washingtonpost", "wsj": "wsj", "wallstreetjournal": "wsj", "theguardian": "guardian",
    "guardian": "guardian", "foxnews": "foxnews", "nbcnews": "nbcnews", "abcnews": "abcnews",
    "cbsnews": "cbsnews", "cnn": "cnn", "npr": "npr", "reuters": "reuters", "politico": "politico",
    "axios": "axios", "thehill": "thehill", "bloomberg": "bloomberg", "newsweek": "newsweek",
    "pbs": "pbs", "dwcom": "dw", "deutschewelle": "dw", "aljazeera": "aljazeera", "latimes": "latimes",
    "losangelestimes": "latimes", "usatoday": "usatoday", "economist": "economist", "cnbc": "cnbc", "forbes": "forbes",
}
_DEFAULT_CATEGORIES = {
    "white_house": ["white house", "oval office", "press secretary", "executive order", "cabinet", "administration"],
    "elections": ["election", "campaign", "poll", "midterm", "primary", "ballot", "vote", "republican", "democrat"],
    "justice": ["court", "judge", "lawsuit", "indict", "trial", "justice department", "ruling", "doj", "fbi", "sentenc"],
    "economy": ["tariff", "economy", "inflation", "tax", "trade", "federal reserve", "stock", "budget", "jobs", "market"],
    "international": ["china", "russia", "ukraine", "nato", "putin", "israel", "gaza", "iran", "europe", "canada", "mexico", "summit", "xi "],
    "controversy": ["controvers", "backlash", "slams", "feud", "scandal", "outrage", "criticiz", "blasts"],
    "statements": ["said", "says", "told", "posted", "truth social", "interview", "speech", "remarks"],
}


class EmbeddingsSection(_Base):
    """Regroupement par le sens (modèle d'embedding de LM Studio). Repli lexical si indisponible."""

    enabled: bool = True
    model: str = "auto"                       # "auto" = premier modèle d'embedding trouvé
    threshold: float = Field(0.8, gt=0, le=1)  # similarité cosinus minimale pour fusionner deux titres


class NewsSection(_Base):
    providers: list[NewsProviderSpec] = Field(default_factory=lambda: [p.model_copy() for p in DEFAULT_PROVIDERS])
    keywords: list[str] = ["trump"]
    related_keywords: list[str] = ["white house", "oval office", "mar-a-lago", "maga", "jd vance", "vance"]
    min_relevance: float = Field(0.5, ge=0, le=1)   # 1.0 titre, 0.6 texte, 0.5 mot lié dans le titre
    max_age_hours: int = Field(72, ge=1)
    max_per_provider: int = Field(50, ge=1, le=500)
    similarity_threshold: float = Field(0.5, gt=0, le=1)
    story_match_days: int = Field(3, ge=1)
    rescore_days: int = Field(7, ge=1)
    default_reliability: float = Field(0.5, ge=0, le=1)
    source_reliability: dict[str, float] = Field(default_factory=lambda: dict(_DEFAULT_RELIABILITY))
    source_aliases: dict[str, str] = Field(default_factory=lambda: dict(_DEFAULT_ALIASES))
    categories: dict[str, list[str]] = Field(default_factory=lambda: {k: list(v) for k, v in _DEFAULT_CATEGORIES.items()})
    scoring: ScoringSection = Field(default_factory=ScoringSection)
    embeddings: EmbeddingsSection = Field(default_factory=EmbeddingsSection)


class EdgeTTSConfig(_Base):
    voices: dict[str, str] = Field(default_factory=lambda: {
        "en": "en-US-AndrewNeural", "fr": "fr-FR-HenriNeural", "es": "es-ES-AlvaroNeural"})
    pitch: str = "+0Hz"


class PiperConfig(_Base):
    executable: str = "piper"
    models_dir: Path = Path("data/voices")
    voices: dict[str, str] = Field(default_factory=lambda: {
        "en": "en_US-lessac-medium", "fr": "fr_FR-siwis-medium", "es": "es_ES-davefx-medium"})


class ElevenLabsConfig(_Base):
    model: str = "eleven_multilingual_v2"
    voices: dict[str, str] = Field(default_factory=dict)   # langue -> voice_id (à renseigner)
    stability: float = Field(0.5, ge=0, le=1)
    similarity_boost: float = Field(0.75, ge=0, le=1)
    style: float = Field(0.0, ge=0, le=1)
    cost_per_1k_chars: float = Field(0.0, ge=0)             # pour l'estimation de coût (selon votre abonnement)


class TTSSection(_Base):
    provider: Literal["edge", "piper", "elevenlabs"] = "edge"
    fallback: Literal["edge", "piper", "elevenlabs"] | None = "piper"
    speed: float = Field(1.0, ge=0.7, le=1.3)
    pause_s: float = Field(0.25, ge=0, le=2)
    hook_pause_s: float = Field(0.4, ge=0, le=2)
    normalize_loudness: bool = True
    retries: int = Field(3, ge=1, le=6)
    style_prosody: dict[str, float] = Field(default_factory=lambda: {
        "breaking_news": 1.08, "fast_news": 1.15, "dramatic": 0.95, "explainer": 0.97,
        "timeline": 1.0, "neutral_news": 1.0, "fact_check": 1.0})
    pronunciations: dict[str, str] = Field(default_factory=dict)   # mot -> graphie lue par la voix
    edge: EdgeTTSConfig = Field(default_factory=EdgeTTSConfig)
    piper: PiperConfig = Field(default_factory=PiperConfig)
    elevenlabs: ElevenLabsConfig = Field(default_factory=ElevenLabsConfig)

    @field_validator("provider", "fallback", mode="before")
    @classmethod
    def _legacy(cls, v):
        return "piper" if v == "local" else v   # ancien réglage « local »


class WikimediaConfig(_Base):
    enabled: bool = True
    contact: str = ""                   # e-mail ou URL : exigé par la politique d'usage de Wikimedia (User-Agent)
    allow_share_alike: bool = False     # CC BY-SA : plus restrictif, exclu par défaut
    min_width: int = Field(900, ge=200)
    max_candidates: int = Field(20, ge=1, le=50)


class PexelsConfig(_Base):
    enabled: bool = False               # clé : variable PEXELS_API_KEY (gratuite)
    min_width: int = Field(900, ge=200)


class AIImageConfig(_Base):
    enabled: bool = False
    backend: Literal["a1111"] = "a1111"   # Automatic1111 / Forge (API --api) ; ComfyUI : futur plugin
    url: str = "http://127.0.0.1:7860"
    steps: int = Field(25, ge=1, le=150)
    cfg_scale: float = Field(6.5, ge=1, le=30)
    width: int = 768
    height: int = 1344
    timeout_s: float = Field(300, gt=0)
    style_prompt: str = "flat vector editorial illustration, stylized, bold simple shapes, muted colors, no text"


class CardsConfig(_Base):
    enabled: bool = True
    font_path: str | None = None


class VisualsSection(_Base):
    provider: str = "local"            # ancien réglage, ignoré
    order: list[Literal["user", "wikimedia", "pexels", "ai", "cards"]] = ["user", "wikimedia", "pexels", "ai", "cards"]
    user_dir: Path = Path("assets/user_images")
    target_shot_s: float = Field(3.5, gt=0.5)
    max_shots_per_segment: int = Field(3, ge=1, le=6)
    avoid_recent_plans: int = Field(5, ge=0)     # n'utilise pas les visuels des N derniers plans
    entity_aliases: dict[str, str] = Field(default_factory=lambda: {"Trump": "Donald Trump", "Putin": "Vladimir Putin", "Vance": "JD Vance"})
    hook_entity: str = "Donald Trump"             # sujet de la photo d'ouverture si le hook ne nomme personne ("" = désactivé)
    retries: int = Field(2, ge=1, le=5)
    wikimedia: WikimediaConfig = Field(default_factory=WikimediaConfig)
    pexels: PexelsConfig = Field(default_factory=PexelsConfig)
    ai: AIImageConfig = Field(default_factory=AIImageConfig)
    cards: CardsConfig = Field(default_factory=CardsConfig)


class FactCheckSection(_Base):
    fetch_full_text: bool = True          # télécharge le texte des articles (robots.txt respecté)
    max_sources: int = Field(6, ge=1, le=12)
    max_chars_per_source: int = Field(1500, ge=200)
    max_input_chars: int = Field(9000, ge=1000)   # ~2 500 tokens : prévoir un contexte LM Studio >= 8192
    max_output_tokens: int = Field(4096, ge=512)
    max_claims: int = Field(8, ge=1, le=20)
    disable_thinking: bool = True          # ajoute /no_think (Qwen3) : plus rapide, JSON plus fiable
    confirmed_min_sources: int = Field(3, ge=1)
    likely_min_sources: int = Field(2, ge=1)
    confirmed_min_reliability: float = Field(0.7, ge=0, le=1)
    min_quote_words: int = Field(4, ge=1)
    quote_match_ratio: float = Field(0.85, gt=0, le=1)
    require_quote_support: bool = True     # la citation doit nommer les lieux/personnes de l'affirmation (pas seulement exister)


class ScriptSection(_Base):
    max_attempts: int = Field(3, ge=1, le=6)
    temperature: float = Field(0.6, ge=0, le=2)
    max_output_tokens: int = Field(3000, ge=512)
    disable_thinking: bool = True
    words_per_second: dict[str, float] = Field(default_factory=lambda: {"en": 2.5, "fr": 2.7, "es": 2.8})
    length_low: float = Field(0.8, gt=0, le=1)       # mots minimum = cible x length_low
    length_high: float = Field(1.15, ge=1)           # mots maximum = cible x length_high
    hook_max_words: int = Field(20, ge=5)
    max_opinion_segments: int = Field(2, ge=0)       # tons pro/anti : segments d'opinion étiquetés autorisés
    llm_verify: bool = True                          # 2e passe : le LLM traque toute information ajoutée
    auto_attribution: bool = True                    # ajoute « According to <vraie source> » si c'est la seule erreur
    adapt_duration: bool = True                      # réduit la durée si la matière vérifiée est insuffisante
    strict_entities: bool = False                    # True : un nom propre absent des affirmations = rejet
    recent_hooks_to_avoid: int = Field(8, ge=0)
    duplicate_similarity: float = Field(0.75, gt=0, le=1)
    banned_phrases: list[str] = [
        "you won't believe", "will shock you", "shocking truth", "what happens next will", "they don't want you to know",
        "vous n'allez pas croire", "ce qu'ils ne veulent pas que vous sachiez", "no vas a creer",
    ]
    entity_allowlist: list[str] = [
        "Trump", "Donald", "President", "White", "House", "United", "States", "America", "American", "Americans",
        "Republican", "Republicans", "Democrat", "Democrats", "Congress", "Senate", "Monday", "Tuesday", "Wednesday",
        "Thursday", "Friday", "Saturday", "Sunday", "January", "February", "March", "April", "May", "June", "July",
        "August", "September", "October", "November", "December", "US", "USA", "Etats", "États", "Unis", "Maison",
        "Blanche", "Président", "Estados", "Unidos", "Casa", "Blanca", "Presidente",
    ]


class ProviderChoice(_Base):
    provider: str = "local"


VIDEO_TEMPLATES = ("breaking_news", "news_fast", "minimal", "documentary", "political")


class VideoSection(_Base):
    ffmpeg_path: str | None = None   # None = ffmpeg du PATH
    resolution: str = "1080x1920"
    fps: int = Field(30, ge=1, le=120)
    template: Literal["auto", "breaking_news", "news_fast", "minimal", "documentary", "political"] = "auto"
    crf: int = Field(20, ge=14, le=32)               # qualité H.264 : plus bas = meilleur et plus lourd
    preset: Literal["ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow"] = "medium"
    font_file: str | None = None                     # police TTF/OTF (ex. assets/fonts/Anton-Regular.ttf), sinon polices Windows
    subtitles: bool = True
    subtitles_on_cards: bool = False                 # les cartes affichent déjà le texte : sous-titres masqués par défaut
    max_words_per_caption: int | None = Field(None, ge=1, le=6)   # None = valeur du template
    transitions: bool = True
    progress_bar: bool | None = None                 # None = valeur du template
    music: bool = False                              # musique de fond (fichiers fournis dans music_dir, licence déclarée)
    music_dir: Path = Path("assets/music")
    music_track: str | None = None                   # nom de fichier précis, sinon rotation
    music_volume_db: float | None = Field(None, ge=-40, le=-6)   # None = valeur du template
    sfx: bool = False                                # effet sonore sur les transitions (fichier de sfx_dir)
    sfx_dir: Path = Path("assets/sfx")
    sfx_volume_db: float = Field(-12, ge=-40, le=0)
    keep_work: bool = False                          # conserve les fichiers intermédiaires (diagnostic)
    render_timeout_s: int = Field(1200, ge=60)

    @field_validator("resolution")
    @classmethod
    def _res(cls, v: str) -> str:
        if not re.match(r"^\d{3,5}x\d{3,5}$", v):
            raise ValueError("résolution attendue au format LARGEURxHAUTEUR (ex. 1080x1920)")
        return v


class EditorialSection(_Base):
    mode: Literal["semi_auto", "full_auto"] = "semi_auto"
    tone: Literal["neutral", "pro_trump", "anti_trump", "balanced"] = "neutral"
    style: Literal[
        "breaking_news", "neutral_news", "fast_news", "dramatic", "explainer", "timeline", "fact_check"
    ] = "neutral_news"
    duration_s: Literal[20, 30, 45, 60, 90] = 45


class QualitySection(_Base):
    min_score: float = Field(0.7, ge=0, le=1)


class SchedulerSection(_Base):
    """Lancement du pilote automatique par le Planificateur de tâches Windows (run.bat schedule install).
    Les heures sont celles de l'horloge du PC."""
    enabled: bool = True
    times: list[str] = Field(default_factory=lambda: ["08:00", "13:00", "18:00"])
    timezone: str = "Europe/Paris"
    task_name: str = Field("TrumpNews Autopilot", min_length=3, max_length=100)
    start_lmstudio: bool = True          # démarre le serveur LM Studio (lms server start) s'il ne répond pas
    max_run_hours: int = Field(3, ge=1, le=12)   # au-delà, Windows arrête le lancement

    _tz = field_validator("timezone")(_check_tz)

    @field_validator("times", mode="before")
    @classmethod
    def _times_text(cls, v):
        """set-option accepte « 08:00,13:00,18:00 ». YAML lit « 18:00 » sans guillemets comme 1080 (base 60)."""
        if isinstance(v, (str, int)):
            v = [v]
        out = []
        for t in v:
            if isinstance(t, int):
                t = f"{t // 60:02d}:{t % 60:02d}"
            out += [x.strip() for x in str(t).split(",") if x.strip()]
        return out

    @field_validator("times")
    @classmethod
    def _times(cls, v: list[str]) -> list[str]:
        for t in v:
            if not _HHMM.match(t):
                raise ValueError(f"heure invalide '{t}' (format HH:MM)")
        return v


class YouTubeSection(_Base):
    """Identifiants OAuth : secrets YOUTUBE_CLIENT_ID et YOUTUBE_CLIENT_SECRET (run.bat set-secret), puis run.bat connect youtube."""
    enabled: bool = False
    privacy: Literal["private", "unlisted", "public"] = "private"
    category_id: str = "25"            # Actualités et politique
    made_for_kids: bool = False

    @field_validator("category_id", mode="before")
    @classmethod
    def _category(cls, v):
        return str(v) if isinstance(v, int) else v


class TikTokSection(_Base):
    """Secrets TIKTOK_CLIENT_KEY et TIKTOK_CLIENT_SECRET, puis run.bat connect tiktok.
    Adresse de retour à déclarer dans l'application TikTok : http://localhost:<redirect_port>/callback/"""
    enabled: bool = False
    privacy: Literal["SELF_ONLY", "MUTUAL_FOLLOW_FRIENDS", "FOLLOWER_OF_CREATOR", "PUBLIC_TO_EVERYONE"] = "SELF_ONLY"
    redirect_port: int = Field(3455, ge=1024, le=65535)
    disable_comment: bool = False
    disable_duet: bool = False
    disable_stitch: bool = False


class InstagramSection(_Base):
    """Jeton de Page Facebook (Facebook Login for Business) : run.bat connect instagram."""
    enabled: bool = False
    user_id: str = ""                  # vide : identifiant trouvé par « connect instagram »
    api_version: str = "v25.0"
    share_to_feed: bool = True

    @field_validator("user_id", mode="before")
    @classmethod
    def _user_id(cls, v):
        return str(v) if isinstance(v, int) else v   # set-option lit « 1784... » comme un nombre

    @field_validator("api_version")
    @classmethod
    def _version(cls, v: str) -> str:
        if not re.fullmatch(r"v\d+\.\d+", v):
            raise ValueError("version attendue sous la forme v25.0")
        return v


class PublishingSection(_Base):
    require_approval: bool = True      # seul un script approuvé (run.bat approve) est publié
    hashtags: list[str] = Field(default_factory=lambda: ["#Trump", "#news", "#politics"])
    youtube: YouTubeSection = Field(default_factory=YouTubeSection)
    tiktok: TikTokSection = Field(default_factory=TikTokSection)
    instagram: InstagramSection = Field(default_factory=InstagramSection)

    @field_validator("hashtags")
    @classmethod
    def _hashtags(cls, v: list[str]) -> list[str]:
        out = []
        for h in v:
            h = h.strip()
            if not re.fullmatch(r"#?\w+", h):
                raise ValueError(f"hashtag invalide '{h}' (un seul mot, sans espace)")
            out.append(h if h.startswith("#") else "#" + h)
        return out


class AutopilotSection(_Base):
    """Bouton « Tout générer et publier » (run.bat autopilot) : choisit les sujets, monte et publie sur YouTube
    sans relecture humaine. Seules les vidéos READY dont le script n'a aucun avertissement sont publiées."""
    videos_per_run: int = Field(3, ge=1, le=5)       # vidéos publiées au plus par lancement
    max_per_day: int = Field(3, ge=1, le=20)         # publications YouTube au plus sur 24 h glissantes (quota Google)
    max_attempts: int = Field(6, ge=1, le=20)        # sujets essayés au plus par lancement
    hours: int = Field(24, ge=1, le=168)             # ancienneté maximale des sujets
    min_story_score: float = Field(0.0, ge=0, le=1)
    allow_caution: bool = True                       # false : seuls les sujets SAFE sont publiés
    auto_approve_minor: bool = True                  # approuve un script dont les avertissements sont tous mineurs
    youtube_privacy: Literal["private", "unlisted", "public"] = "public"
    fetch_news: bool = True                          # récupère les actualités avant de choisir
    neighbor_hours: int = Field(48, ge=0, le=168)    # écarte un sujet voisin d'une vidéo publiée depuis N heures
    neighbor_similarity: float = Field(0.4, gt=0, le=1)   # similarité des titres (mots) à partir de laquelle c'est « voisin »


class AppConfig(_Base):
    app: AppSection = Field(default_factory=AppSection)
    llm: LLMSection = Field(default_factory=LLMSection)
    storage: StorageSection = Field(default_factory=StorageSection)
    news: NewsSection = Field(default_factory=NewsSection)
    factcheck: FactCheckSection = Field(default_factory=FactCheckSection)
    script: ScriptSection = Field(default_factory=ScriptSection)
    tts: TTSSection = Field(default_factory=TTSSection)
    visuals: VisualsSection = Field(default_factory=VisualsSection)
    video: VideoSection = Field(default_factory=VideoSection)
    editorial: EditorialSection = Field(default_factory=EditorialSection)
    quality: QualitySection = Field(default_factory=QualitySection)
    scheduler: SchedulerSection = Field(default_factory=SchedulerSection)
    publishing: PublishingSection = Field(default_factory=PublishingSection)
    autopilot: AutopilotSection = Field(default_factory=AutopilotSection)

    @model_validator(mode="after")
    def _scheduler_tz_default(self) -> "AppConfig":
        if "timezone" not in self.scheduler.model_fields_set:
            self.scheduler.timezone = self.app.timezone
        return self
