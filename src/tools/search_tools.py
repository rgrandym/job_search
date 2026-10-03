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


# Words any department's titles use; overlap on these says nothing about the role family.
_GENERIC_TITLE_WORDS = {
    "head", "lead", "leader", "group", "team", "global", "regional", "manager", "management",
    "director", "officer", "specialist", "associate", "assistant", "coordinator", "project",
    "projects", "programme", "program", "development", "research", "data", "operations",
    "business", "uk", "emea", "europe", "remote", "hybrid", "contract", "permanent", "month",
    "months", "ftc", "full", "part", "time", "with", "for", "in", "on", "at", "to", "a",
}  # fmt: skip
_COMPANY_SUFFIXES = re.compile(
    r"\b(plc|ltd|limited|inc|incorporated|llc|gmbh|ag|sa|bv|co|corp|corporation|group|holdings"
    r"|therapeutics|pharmaceuticals?|pharma|biotechnolog(?:y|ies)|biosciences?|uk)\b\.?",
    re.IGNORECASE,
)


def role_words(title: str) -> set[str]:
    """The role-specific words of a title: `title_core` minus words every department uses."""
    return title_core(title) - _GENERIC_TITLE_WORDS


def shares_role_words(title: str, targets: Iterable[str]) -> bool:
    """Whether a title shares a role-specific word with any target title ("Scientist, Genetic
    Assays" vs "Principal Scientist Cell Therapy": yes; "HR Business Lead": no). A target made
    only of words every department uses ("Business Development Manager") matches a title
    containing all of its words instead."""
    targets = list(targets)
    wanted = set().union(*(role_words(t) for t in targets))
    if role_words(title) & wanted:
        return True
    core = title_core(title)
    generic = [title_core(t) for t in targets if not role_words(t)]
    return any(len(g) >= 2 and g <= core for g in generic)


def company_key(name: str) -> str:
    """Company name without legal or sector suffixes, for matching one employer across sources
    ("Moderna Therapeutics" = "Moderna", "GSK plc" = "GSK")."""
    aliases = {
        "ukri": "ukri",
        "research and innovation": "ukri",
        "glaxosmithkline": "gsk",
        "glaxo smith kline": "gsk",
    }
    plain = re.sub(r"[^\w\s&]", " ", name.lower())
    core = " ".join(_COMPANY_SUFFIXES.sub(" ", plain).split())
    return aliases.get(core, core) or " ".join(name.lower().split())


def posting_description(text: str) -> str:
    """Normalize presentation-only differences without discarding job requirements."""
    plain = re.sub(r"^\s*(?:job )?description[ \t]*(?::|[-–]|\n)\s*", "", text, flags=re.I)
    plain = " ".join(plain.split())
    return plain.casefold()


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


_PARENTHETICAL = re.compile(r"\([^)]*\)")
_ALTERNATIVE = re.compile(r"\s*/\s*|\s+or\s+", re.IGNORECASE)


def _alternatives(text: str) -> list[str]:
    """ "Cell Therapy / iPSC" -> ["Cell Therapy", "iPSC"]."""
    return [" ".join(p.split()) for p in _ALTERNATIVE.split(text) if p.strip()]


def board_terms(roles: Iterable[str], keywords: Iterable[str] = (), limit: int = 16) -> list[str]:
    """Short, advertisable job-board search terms from long profile role titles.

    "Principal Scientist, Cell Therapy / iPSC" -> "Principal Scientist Cell Therapy",
    "Principal Scientist iPSC". Boards match every word (Adzuna) or rank loosely (Reed), so
    one long composite title finds little or noise. Order: the first variant of every role,
    then the profile keywords (broad, high-recall terms such as "iPSC"), then the remaining
    variants round-robin, so a limit never drops a whole role family.
    """
    per_role: list[list[str]] = []
    for role in roles:
        head, _, focus = _PARENTHETICAL.sub(" ", role).partition(",")
        variants = [
            " ".join(f"{h} {f}".split())
            for h in _alternatives(head)
            for f in (_alternatives(focus) or [""])
            if len(h.split()) > 1 or f  # a lone word ("Research") is not a title
        ]
        per_role.append(variants or [" ".join(role.split())])
    ranks = [
        [r[i] for r in per_role if i < len(r)] for i in range(max(map(len, per_role), default=0))
    ]
    firsts, later = (ranks[0] if ranks else []), [v for rank in ranks[1:] for v in rank]
    ordered = [*firsts, *(" ".join(k.split()) for k in keywords if k.strip()), *later]
    seen: set[str] = set()
    terms = []
    for term in ordered:
        if term.lower() not in seen and len(term.split()) <= 6:
            seen.add(term.lower())
            terms.append(term)
    return terms[:limit]


# ------------------------------------------------------------------ posting excerpts

# Headings that start the requirements part of a posting (often past the first screenful).
_REQUIREMENT_HEADINGS = re.compile(
    r"(?:essential|desirable)\s+(?:criteria|requirements|skills|experience)|person specification"
    r"|requirements|qualifications|about you|who you are|what you(?:'ll| will) (?:bring|need)"
    r"|you will (?:have|bring|need)|skills (?:and|&) experience|experience (?:and|&) skills"
    r"|the ideal candidate|we(?:'re| are) looking for|what we(?:'re| are) looking for",
    re.IGNORECASE,
)


def posting_excerpt(text: str, limit: int) -> str:
    """At most `limit` characters of a posting, keeping its requirements: when a requirements
    heading falls past the cut, the excerpt is the opening plus the requirements section,
    because essential criteria usually come last."""
    if len(text) <= limit:
        return text
    found = [m for m in _REQUIREMENT_HEADINGS.finditer(text) if m.start() > limit // 3]
    if not found:
        return text[:limit]
    start = found[0].start()
    if start + 200 >= limit:  # most of the requirements already fit after the opening
        head = limit // 3
        return f"{text[:head]} […] {text[start : start + limit - head]}"
    return text[:limit]


# ------------------------------------------------------------------ languages & eligibility

LANGUAGE_NAMES = (
    "english", "german", "french", "spanish", "italian", "dutch", "flemish", "portuguese",
    "swedish", "danish", "norwegian", "finnish", "icelandic", "polish", "czech", "slovak",
    "hungarian", "romanian", "bulgarian", "greek", "turkish", "russian", "ukrainian", "arabic",
    "hebrew", "hindi", "urdu", "bengali", "mandarin", "cantonese", "chinese", "japanese",
    "korean", "vietnamese", "thai", "indonesian", "malay", "welsh", "irish", "catalan",
)  # fmt: skip
# Level cues, strongest first: 4 native · 3 professional · 2 conversational · 1 basic.
_LEVEL_CUES: tuple[tuple[int, str], ...] = (
    (4, r"native|mother[ -]tongue|bilingual|first language|\bc2\b"),
    (3, r"fluen\w*|proficien\w*|business[- ]level|professional|full working|\bc1\b|advanced"),
    (2, r"conversational|intermediate|working knowledge|good command|\bb[12]\b|good\b"),
    (1, r"basic|beginner|elementary|\ba[12]\b"),
)
# A sentence naming a language is a requirement only with one of these cues: "the German
# market" or "our French subsidiary" is not.
_LANGUAGE_CUE = re.compile(
    r"fluen|proficien|native|mother[ -]tongue|bilingual|speak|spoken|written|language|"
    r"business[- ]level|working knowledge|conversational|\b[abc][12]\b",
    re.IGNORECASE,
)
_OPTIONAL_CUE = re.compile(
    r"desirabl|advantage|a plus|bonus|nice to have|preferred|beneficial|ideally|welcome",
    re.IGNORECASE,
)
_SENTENCES = re.compile(r"(?<=[.;!?•\n])\s+|\n+")


def _level(text: str, default: int) -> int:
    low = text.lower()
    return next((lvl for lvl, cue in _LEVEL_CUES if re.search(cue, low)), default)


def parse_language(entry: str) -> tuple[str, int] | None:
    """("Spanish (C1)" -> ("spanish", 3)). A language listed without a level counts as
    conversational (2), so a posting needing fluency raises a flag rather than passing."""
    low = entry.lower()
    name = next((n for n in LANGUAGE_NAMES if re.search(rf"\b{n}\b", low)), None)
    return None if name is None else (name, _level(low, 2))


def language_requirements(text: str) -> list[tuple[str, int, bool, str]]:
    """Explicit language requirements of a posting: (language, level 1-4, essential, the
    sentence that says so). The language a posting is written in is not a requirement; only
    a stated one is. A requirement without a level cue counts as professional (3)."""
    out: dict[str, tuple[str, int, bool, str]] = {}
    for sentence in _SENTENCES.split(text):
        low = sentence.lower()
        if not _LANGUAGE_CUE.search(low):
            continue
        names = [n for n in LANGUAGE_NAMES if re.search(rf"\b{n}\b", low)]
        if not names:
            continue
        essential = not _OPTIONAL_CUE.search(low)
        level = _level(low, 3)
        for name in names:
            known = out.get(name)
            if known is None or (essential, level) > (known[2], known[1]):
                out[name] = (name, level, essential, " ".join(sentence.split())[:200])
    return list(out.values())


ELIGIBILITY_PATTERNS: dict[str, str] = {
    "right to work": r"right to work|work permit|visa sponsorship|sponsorship is not|"
    r"(?:unable|not able) to (?:offer|provide) (?:visa )?sponsorship|eligible to work",
    "security clearance": r"security clearance|\b(?:sc|dv|nv\d?)\b[- ]clear|clearance (?:is )?"
    r"required|vetting",
    "driving licence": r"driving licen[cs]e|driver'?s licen[cs]e",
    "professional registration": r"\b(?:hcpc|gmc|nmc|gphc|ucp|cipd|aca|acca|cima)\b[^.]{0,40}"
    r"regist|registered (?:with|as)|chartered status",
}


def eligibility_requirements(text: str) -> list[tuple[str, str]]:
    """Eligibility conditions a posting states: (kind, the sentence that says so). Flagged for
    the user to confirm, never used to exclude (`ELIGIBILITY_PATTERNS` lists the kinds)."""
    out: dict[str, str] = {}
    for sentence in _SENTENCES.split(text):
        for kind, pattern in ELIGIBILITY_PATTERNS.items():
            if kind not in out and re.search(pattern, sentence, re.IGNORECASE):
                out[kind] = " ".join(sentence.split())[:200]
    return list(out.items())


def confirms(confirmed: Iterable[str], kind: str) -> bool:
    """Whether a confirmed eligibility statement covers `kind` ("Right to work in the UK"
    covers "right to work"; "SC cleared" covers "security clearance")."""
    keys = {
        "right to work": ("right to work", "visa", "citizen", "settled status", "work permit"),
        "security clearance": ("clearance", "cleared", "vetting", "vetted"),
        "driving licence": ("driving", "driver"),
        "professional registration": ("regist", "chartered", "hcpc", "gmc", "nmc", "gphc"),
    }[kind]
    return any(k in c.lower() for c in confirmed for k in keys)
