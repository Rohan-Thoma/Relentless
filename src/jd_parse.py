import re

STATIC_SKILLS = [
    "python", "fastapi", "django", "flask", "postgresql", "postgres", "sql", "redis",
    "docker", "kubernetes", "aws", "gcp", "azure", "langchain", "langgraph", "rag",
    "llm", "genai", "machine learning", "pytorch", "tensorflow", "pandas", "numpy",
    "spark", "kafka", "airflow", "git", "linux", "rest", "graphql", "microservices",
    "typescript", "javascript", "react", "node", "java", "go", "rust", "c++",
    "mlops", "ci/cd", "terraform", "helm", "elasticsearch", "mongodb", "mysql",
    "scikit-learn", "huggingface", "openai", "anthropic", "vector database",
    "embeddings", "nlp", "computer vision", "data engineering", "etl",
]


def _extract_salary(text: str):
    patterns = [
        r"(?:₹|Rs\.?|INR|\$)\s?[\d,\.]+\s?(?:LPA|lakh|k|K)?(?:\s?-\s?(?:₹|Rs\.?|INR|\$)?\s?[\d,\.]+\s?(?:LPA|lakh|k|K)?)?",
        r"\d+(?:\.\d+)?\s?(?:LPA|lakh|LPA)\b",
    ]
    hits = []
    for pat in patterns:
        hits.extend(re.findall(pat, text))
    return hits[:5]


def _extract_skills(text: str, profile: dict | None = None):
    low = text.lower()
    found = set()
    if profile:
        for s in profile.get("core_skills", []):
            if s.lower() in low:
                found.add(s)
    for s in STATIC_SKILLS:
        if s in low:
            found.add(s)
    return sorted(found)


def parse_jd(jd_text: str, profile: dict | None = None) -> dict:
    text = jd_text or ""
    # naive section split by headings
    sections = {"responsibilities": "", "requirements": "", "nice_to_have": "", "compensation": ""}
    # compensation: lines mentioning salary/pay
    comp_lines = [ln for ln in text.splitlines() if re.search(r"salary|compensation|ctc|lpa|lakh", ln, re.IGNORECASE)]
    sections["compensation"] = " ".join(comp_lines)[:1000]
    # requirements: text after 'requirements'/'qualifications' heading until next heading
    def grab(heading):
        m = re.search(rf"{heading}[:\s]*(.*?)(?=\n[A-Z][A-Za-z ]{{3,}}:|\Z)", text, re.IGNORECASE | re.DOTALL)
        return m.group(1).strip() if m else ""
    sections["requirements"] = grab(r"(?:requirements|qualifications|must have)")[:2000]
    sections["responsibilities"] = grab(r"(?:responsibilities|what you.ll do|duties)")[:2000]
    sections["nice_to_have"] = grab(r"(?:nice to have|preferred|bonus)")[:1000]

    return {
        "responsibilities": sections["responsibilities"],
        "requirements": sections["requirements"],
        "nice_to_have": sections["nice_to_have"],
        "compensation": sections["compensation"],
        "salary_mentions": _extract_salary(text),
        "skills": _extract_skills(text, profile),
    }