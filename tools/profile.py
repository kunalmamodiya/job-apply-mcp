"""
Candidate profile definition and job relevance matching logic.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher


@dataclass(frozen=True)
class CandidateProfile:
    title: str = "DevOps and AI/ML Engineer"
    experience_years: int = 4
    location: str = "Jaipur, India"
    preferred_locations: tuple[str, ...] = (
        "Remote",
        "Jaipur",
        "Noida",
        "Gurgaon",
        "Gurugram",
        "Bangalore",
        "Bengaluru",
        "Hyderabad",
        "Pune",
        "Remote",
    )
    skills: tuple[str, ...] = (
        "GCP",
        "Azure",
        "Terraform",
        "Kubernetes",
        "Docker",
        "GitLab CI/CD",
        "Jenkins",
        "Azure DevOps",
        "Prometheus",
        "Grafana",
        "Python",
        "Bash",
        "PowerShell",
        "Ollama",
        "RAG",
        "LangChain",
        "QLoRA",
        "LoRA",
        "MCP Tools",
        "FAISS",
        "ChromaDB",
        "LLMs",
        "NLP",
        "Generative AI",
        "SonarQube",
        "Trivy",
        "SBOM",
        "Cloud SQL",
        "BigQuery",
        "Secret Manager",
        "IAM",
        "AKS",
        "GKE",
        "Fargate",
    )
    target_roles: tuple[str, ...] = (
        "MLOps Engineer",
        "LLMOps Engineer",
        "Platform Engineer GenAI",
        "DevOps Engineer",
        "Site Reliability Engineer",
        "AI Platform Engineer",
        "Cloud DevOps Engineer",
        "GenAI Infrastructure Engineer",
    )
    default_search_keywords: tuple[str, ...] = (
        "DevOps Engineer",
        "MLOps Engineer",
        "LLMOps Engineer",
        "Platform Engineer GenAI",
        "Cloud DevOps AI",
        "GenAI Infrastructure Engineer",
        "Site Reliability Engineer",
    )
    avoid_keywords: tuple[str, ...] = (
        "frontend",
        "react developer",
        "angular developer",
        "vue developer",
        "android",
        "ios",
        "swift",
        "kotlin",
        "flutter",
        "data scientist",
        "pytorch training",
        "tensorflow training",
        "deep learning researcher",
    )


PROFILE = CandidateProfile()


def _normalize(text: str) -> str:
    """Lower-case, collapse whitespace, strip punctuation."""
    return re.sub(r"[^a-z0-9 /+#]", " ", text.lower()).strip()


def _fuzzy_ratio(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio()


def _token_overlap(tokens_a: set[str], tokens_b: set[str]) -> float:
    if not tokens_a or not tokens_b:
        return 0.0
    return len(tokens_a & tokens_b) / len(tokens_a)


def compute_match_score(
    job_title: str,
    job_description: str,
    job_location: str,
    required_skills: list[str] | None = None,
    profile: CandidateProfile = PROFILE,
) -> float:
    """
    Return a 0.0-1.0 relevance score for a job against the candidate profile.

    Scoring weights:
        - Role match    : 35%
        - Skill overlap : 40%
        - Location match: 15%
        - Avoid penalty : 10% (subtracted)
    """
    norm_title = _normalize(job_title)
    norm_desc = _normalize(job_description)
    combined_text = f"{norm_title} {norm_desc}"

    # --- Role match (35%) ---
    role_scores: list[float] = []
    for role in profile.target_roles:
        norm_role = _normalize(role)
        # direct substring check
        if norm_role in norm_title:
            role_scores.append(1.0)
        else:
            role_scores.append(_fuzzy_ratio(norm_role, norm_title))
    role_score = max(role_scores) if role_scores else 0.0

    # --- Skill overlap (40%) ---
    profile_skills_norm = {_normalize(s) for s in profile.skills}
    if required_skills:
        job_skills_norm = {_normalize(s) for s in required_skills}
        skill_score = _token_overlap(job_skills_norm, profile_skills_norm)
    else:
        # fall back to checking how many profile skills appear in description
        matched = sum(1 for s in profile_skills_norm if s in combined_text)
        skill_score = min(matched / max(len(profile_skills_norm) * 0.3, 1), 1.0)

    # --- Location match (15%) ---
    norm_location = _normalize(job_location)
    location_score = 0.0
    if "remote" in norm_location:
        location_score = 1.0
    else:
        for loc in profile.preferred_locations:
            if _normalize(loc) in norm_location:
                location_score = 1.0
                break
        if location_score == 0.0 and "india" in norm_location:
            location_score = 0.5

    # --- Avoid penalty (10%) ---
    avoid_penalty = 0.0
    for kw in profile.avoid_keywords:
        if _normalize(kw) in combined_text:
            avoid_penalty = 1.0
            break

    score = (
        0.35 * role_score
        + 0.40 * skill_score
        + 0.15 * location_score
        - 0.10 * avoid_penalty
    )
    return round(max(0.0, min(score, 1.0)), 3)


def should_exclude(
    job_title: str,
    job_description: str,
    required_skills: list[str] | None = None,
    profile: CandidateProfile = PROFILE,
) -> bool:
    """Return True if the job is in an excluded category."""
    combined = _normalize(f"{job_title} {job_description}")
    for kw in profile.avoid_keywords:
        if _normalize(kw) in combined:
            return True
    return False
