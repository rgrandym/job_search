"""Deterministic text utilities shared by CV tailoring and job matching.

Pure functions only: no I/O, no LLM calls. Safe to use from any layer.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from functools import lru_cache

# Canonical skill -> aliases. Extend freely; keys are the canonical form.
SKILL_SYNONYMS: dict[str, set[str]] = {
    "javascript": {"js", "ecmascript"},
    "typescript": {"ts"},
    "python": {"py", "python3"},
    "kubernetes": {"k8s"},
    "postgresql": {"postgres", "psql"},
    "machine learning": {"ml"},
    "deep learning": {"dl"},
    "natural language processing": {"nlp"},
    "large language models": {"llm", "llms"},
    "amazon web services": {"aws"},
    "google cloud platform": {"gcp", "google cloud"},
    "microsoft azure": {"azure"},
    "continuous integration": {"ci", "ci/cd", "cicd"},
    "react": {"react.js", "reactjs"},
    "node.js": {"node", "nodejs"},
    "c++": {"cpp"},
    "c#": {"csharp"},
    "golang": {"go lang"},  # bare "go" is too ambiguous ("go to market")
}
_ALIAS_TO_CANON = {a: c for c, aliases in SKILL_SYNONYMS.items() for a in aliases | {c}}

# Ordered seniority ladder. Index = level.
SENIORITY_LEVELS: list[tuple[str, tuple[str, ...]]] = [
    ("intern", ("intern", "internship", "trainee")),
    ("junior", ("junior", "jr", "graduate", "entry", "associate")),
    ("mid", ("mid", "intermediate")),
    ("senior", ("senior", "sr")),
    ("staff", ("staff", "lead", "tech lead")),
    ("principal", ("principal", "distinguished", "architect")),
    ("manager", ("manager", "head of")),
    ("director", ("director",)),
    ("executive", ("vp", "vice president", "chief", "cto", "ceo", "cio")),
]
DEFAULT_LEVEL = 2  # "mid" when a title carries no seniority marker

TITLE_ABBREVIATIONS: dict[str, str] = {
    "sr": "senior",
    "snr": "senior",
    "jr": "junior",
    "jnr": "junior",
    "eng": "engineer",
    "engr": "engineer",
    "dev": "developer",
    "mgr": "manager",
    "ml": "machine learning",
    "ai": "artificial intelligence",
    "nlp": "natural language processing",
    "swe": "software engineer",
    "sde": "software engineer",
    "sre": "site reliability engineer",
    "qa": "quality assurance",
    "pm": "product manager",
    "ux": "user experience",
    "ui": "user interface",
    "fe": "frontend",
    "be": "backend",
    "fullstack": "full stack",
    "devops": "devops",
    "bi": "business intelligence",
    "hr": "human resources",
    "vp": "vice president",
    "cto": "chief technology officer",
}

# Vocabulary for extracting skills from free-text job descriptions. Canonical forms;
# aliases are resolved through SKILL_SYNONYMS. Extend freely.
TECH_SKILLS: tuple[str, ...] = (
    # languages
    "python",
    "java",
    "scala",
    "kotlin",
    "golang",
    "rust",
    "c++",
    "c#",
    "ruby",
    "php",
    "swift",
    "javascript",
    "typescript",
    "sql",
    "matlab",
    "bash",
    # data / ml
    "machine learning",
    "deep learning",
    "natural language processing",
    "computer vision",
    "large language models",
    "pytorch",
    "tensorflow",
    "keras",
    "scikit-learn",
    "xgboost",
    "pandas",
    "numpy",
    "spark",
    "hadoop",
    "airflow",
    "dbt",
    "kafka",
    "flink",
    "snowflake",
    "databricks",
    "bigquery",
    "redshift",
    "mlops",
    "mlflow",
    "kubeflow",
    "hugging face",
    "langchain",
    "rag",
    "recommender systems",
    "statistics",
    "a/b testing",
    "data modeling",
    "etl",
    "tableau",
    "power bi",
    "looker",
    "spacy",
    # cloud / infra
    "amazon web services",
    "google cloud platform",
    "microsoft azure",
    "kubernetes",
    "docker",
    "terraform",
    "ansible",
    "linux",
    "continuous integration",
    "jenkins",
    "github actions",
    "prometheus",
    "grafana",
    "microservices",
    "serverless",
    # data stores
    "postgresql",
    "mysql",
    "mongodb",
    "redis",
    "elasticsearch",
    "dynamodb",
    "cassandra",
    # web
    "react",
    "angular",
    "vue",
    "node.js",
    "django",
    "flask",
    "fastapi",
    "spring",
    "graphql",
    "html",
    "css",
    "next.js",
    # practices / other
    "agile",
    "scrum",
    "tdd",
    "system design",
    "distributed systems",
    "security",
    "gdpr",
    "excel",
    "salesforce",
    "sap",
    "jira",
)

_TITLE_STOPWORDS = set().union(*(set(k) for _, k in SENIORITY_LEVELS)) | {
    "i",
    "ii",
    "iii",
    "iv",
    "of",
    "and",
    "the",
    "&",
    "-",
    "/",
}
_WORD_RE = re.compile(r"[a-z0-9][a-z0-9+#./\-]*")
_NUMBER_RE = re.compile(r"\$?\d[\d,]*(?:\.\d+)?\s*(?:%|[kmb]\b|x\b)?", re.IGNORECASE)


def normalize_skill(skill: str) -> str:
    """Canonical lowercase form of a skill, resolving known aliases."""
    s = re.sub(r"\s+", " ", skill.strip().lower())
    return _ALIAS_TO_CANON.get(s, s)


def normalize_skills(skills: Iterable[str]) -> set[str]:
    """Normalise a collection of skills to a set of canonical forms."""
    return {normalize_skill(s) for s in skills if s.strip()}


def tokenize(text: str) -> list[str]:
    """Lowercase word tokens (keeps symbols common in tech terms: c++, c#, node.js).

    Sentence punctuation is stripped ("Spark." -> "spark").
    """
    return [t.rstrip(".-/") for t in _WORD_RE.findall(text.lower())]


@lru_cache(maxsize=512)
def _padded(text: str) -> str:
    return f" {' '.join(tokenize(text))} "


def _mentions_padded(padded: str, term: str) -> bool:
    canon = normalize_skill(term)
    variants = {canon} | SKILL_SYNONYMS.get(canon, set())
    return any(f" {' '.join(tokenize(v))} " in padded for v in variants if tokenize(v))


def mentions(text: str, term: str) -> bool:
    """True if `term` (or any synonym of it) appears in `text` as whole words."""
    return _mentions_padded(_padded(text), term)


def seniority_level(title: str) -> int:
    """Map a job title to an index in `SENIORITY_LEVELS` (highest marker wins)."""
    low = f" {title.lower()} "
    found = [
        i
        for i, (_, keys) in enumerate(SENIORITY_LEVELS)
        if any(re.search(rf"\b{re.escape(k)}\b", low) for k in keys)
    ]
    return max(found) if found else DEFAULT_LEVEL


def seniority_name(level: int) -> str:
    """Human name of a seniority level index."""
    return SENIORITY_LEVELS[max(0, min(level, len(SENIORITY_LEVELS) - 1))][0]


def expand_title(title: str) -> str:
    """Expand common title abbreviations ("Sr. ML Eng" -> "senior machine learning engineer")."""
    words = [TITLE_ABBREVIATIONS.get(w.rstrip("."), w.rstrip(".")) for w in tokenize(title)]
    return " ".join(words)


def title_core(title: str) -> set[str]:
    """Role-family words of a title with seniority markers removed (abbreviations expanded)."""
    return {t for t in expand_title(title).split() if t not in _TITLE_STOPWORDS}


def extract_skills(text: str, extra_vocab: Iterable[str] = ()) -> list[str]:
    """Canonical skills from `TECH_SKILLS` (+ `extra_vocab`) mentioned in free text.

    Used when a posting has no structured skill lists, which is the common case for
    API and ATS sources.
    """
    vocab = {normalize_skill(v) for v in (*TECH_SKILLS, *SKILL_SYNONYMS, *extra_vocab) if v}
    padded = _padded(text)
    return sorted(v for v in vocab if _mentions_padded(padded, v))


def parse_salary(text: str | None) -> tuple[float | None, float | None]:
    """Best-effort annual salary range from text like "£50,000 - £60k", "$120k+", "45000".

    Values below 1,000 are treated as thousands ("50-60k"). Hourly/daily rates are ignored.
    """
    if not text or re.search(r"per (hour|day)|/h(ou)?r|/day|p/h|daily rate", text, re.I):
        return None, None
    values: list[float] = []
    for num, k in re.findall(r"(\d[\d,]*(?:\.\d+)?)\s*(k)?\b", text, re.I):
        v = float(num.replace(",", ""))
        v = v * 1000 if k or v < 1000 else v
        if 1_000 <= v <= 2_000_000:
            values.append(v)
    if not values:
        return None, None
    return min(values), max(values)


def jaccard(a: set[str], b: set[str]) -> float:
    """Jaccard similarity of two sets (0 when both empty)."""
    return len(a & b) / len(a | b) if a | b else 0.0


def extract_numbers(text: str) -> set[str]:
    """Numeric facts in `text`, normalised (e.g. '40%', '$2m', '12')."""
    return {re.sub(r"[\s,]", "", m.group(0)).lower() for m in _NUMBER_RE.finditer(text)}
