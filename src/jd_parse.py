import re

STATIC_SKILLS = [
    "python", "fastapi", "django", "flask", "postgresql", "postgres", "pgvector", "sql", "redis",
    "docker", "kubernetes", "aws", "gcp", "azure", "bedrock", "sagemaker", "vertex ai",
    "langchain", "langgraph", "langsmith", "langfuse", "rag", "llm", "genai",
    "machine learning", "deep learning", "pytorch", "tensorflow", "keras", "scikit-learn",
    "pandas", "numpy", "spark", "kafka", "airflow", "mlflow", "dvc", "ray",
    "tensorrt", "onnx", "yolo", "yolov8", "opencv", "deepstream", "vlm", "vllm", "triton",
    "git", "linux", "bash", "rest", "graphql", "microservices", "asyncio", "pytest",
    "typescript", "javascript", "react", "node.js", "java", "go", "rust", "c++",
    "mlops", "ci/cd", "terraform", "helm", "elasticsearch", "mongodb", "mysql",
    "huggingface", "openai", "anthropic", "vector database", "vector databases",
    "embeddings", "nlp", "computer vision", "ocr", "data engineering", "etl",
    "streamlit", "gradio", "grafana", "prometheus", "nginx", "gunicorn",
]


def skill_in_text(skill: str, text_lower: str) -> bool:
    """Strict token-boundary skill match (prevents 'go' matching 'good' or 'rag' matching 'storage',
    while still matching skills followed by sentence punctuation like 'Kubernetes.' or 'Python,').
    """
    s = (skill or "").strip().lower()
    if not s or not text_lower:
        return False
    pattern = r"(?:^|(?<![a-z0-9+#]))" + re.escape(s) + r"(?=$|[^a-z0-9+#])"
    return bool(re.search(pattern, text_lower))


def _extract_salary(text: str):
    patterns = [
        r"(?:₹|Rs\.?|INR|\$)\s?[\d,\.]+\s?(?:LPA|lakh|k|K)?(?:\s?-\s?(?:₹|Rs\.?|INR|\$)?\s?[\d,\.]+\s?(?:LPA|lakh|k|K)?)?",
        r"\d+(?:\.\d+)?\s?(?:LPA|lakh)\b",
    ]
    hits = []
    for pat in patterns:
        hits.extend(re.findall(pat, text))
    return hits[:5]


def _extract_skills(text: str, profile: dict | None = None):
    low = (text or "").lower()
    found = set()
    if profile:
        for s in profile.get("core_skills", []):
            if skill_in_text(s, low):
                found.add(s)
    for s in STATIC_SKILLS:
        if skill_in_text(s, low):
            found.add(s)
    return sorted(found)


def parse_jd(jd_text: str, profile: dict | None = None) -> dict:
    text = jd_text or ""
    sections = {"responsibilities": "", "requirements": "", "nice_to_have": "", "compensation": ""}
    comp_lines = [ln for ln in text.splitlines() if re.search(r"salary|compensation|ctc|lpa|lakh", ln, re.IGNORECASE)]
    sections["compensation"] = " ".join(comp_lines)[:1000]

    def grab(heading):
        m = re.search(rf"{heading}[:\s]*(.*?)(?=\n[A-Z][A-Za-z ]{{3,}}:|\Z)", text, re.IGNORECASE | re.DOTALL)
        return m.group(1).strip() if m else ""

    sections["requirements"] = grab(r"(?:requirements|qualifications|must have)")[:4000]
    sections["responsibilities"] = grab(r"(?:responsibilities|what you.ll do|duties)")[:4000]
    sections["nice_to_have"] = grab(r"(?:nice to have|preferred|bonus)")[:2000]

    return {
        "responsibilities": sections["responsibilities"],
        "requirements": sections["requirements"],
        "nice_to_have": sections["nice_to_have"],
        "compensation": sections["compensation"],
        "salary_mentions": _extract_salary(text),
        "skills": _extract_skills(text, profile),
    }
