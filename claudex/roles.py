"""Dynamic expert role builder — assigns roles based on task requirements."""

import yaml
from pathlib import Path


# Expertise domains and their associated role descriptions
EXPERTISE_MAP = {
    "web_backend": "backend web development (APIs, servers, databases, authentication)",
    "web_frontend": "frontend development (React, HTML/CSS, UI/UX, accessibility)",
    "database": "database design (schema, queries, migrations, optimization, indexing)",
    "security": "application security (auth, encryption, input validation, OWASP)",
    "devops": "DevOps and infrastructure (Docker, CI/CD, deployment, monitoring)",
    "api_design": "API design (REST, GraphQL, versioning, documentation)",
    "testing": "testing strategy (unit, integration, e2e, mocking, coverage)",
    "python": "Python development (idioms, packaging, async, type hints)",
    "javascript": "JavaScript/TypeScript development (Node.js, npm, ES modules)",
    "data": "data engineering (ETL, pipelines, streaming, transformation)",
    "ml": "machine learning (models, training, inference, evaluation)",
    "mobile": "mobile development (iOS, Android, React Native, Flutter)",
    "systems": "systems programming (performance, concurrency, memory management)",
}

# Keywords that map to expertise domains
KEYWORD_TRIGGERS = {
    "web_backend": ["api", "rest", "server", "flask", "django", "express", "fastapi", "endpoint", "route", "middleware"],
    "web_frontend": ["react", "vue", "angular", "html", "css", "component", "ui", "frontend", "dom", "browser"],
    "database": ["sql", "database", "postgres", "sqlite", "mongo", "schema", "migration", "query", "table", "index"],
    "security": ["auth", "login", "password", "token", "jwt", "oauth", "encrypt", "secure", "permission", "role"],
    "devops": ["docker", "deploy", "ci", "cd", "pipeline", "kubernetes", "nginx", "caddy", "container"],
    "api_design": ["api", "endpoint", "graphql", "rest", "swagger", "openapi", "versioning"],
    "testing": ["test", "unittest", "pytest", "jest", "coverage", "mock", "assert", "spec"],
    "python": ["python", "pip", "virtualenv", "flask", "django", "fastapi", "pydantic"],
    "javascript": ["javascript", "typescript", "node", "npm", "react", "express", "webpack", "vite"],
    "data": ["etl", "pipeline", "csv", "json", "transform", "stream", "batch", "data"],
    "ml": ["model", "train", "inference", "neural", "embedding", "vector", "llm", "ai"],
    "mobile": ["ios", "android", "react native", "flutter", "mobile", "app"],
    "systems": ["performance", "concurrency", "thread", "memory", "rust", "go", "c++"],
}


def detect_expertise(task: str) -> list[str]:
    """Detect required expertise domains from a task description."""
    task_lower = task.lower()
    scores: dict[str, int] = {}

    for domain, keywords in KEYWORD_TRIGGERS.items():
        score = sum(1 for kw in keywords if kw in task_lower)
        if score > 0:
            scores[domain] = score

    # Sort by relevance score, take top 3
    sorted_domains = sorted(scores, key=scores.get, reverse=True)[:3]

    # Always include at least one general domain
    if not sorted_domains:
        sorted_domains = ["python"]  # safe default

    return sorted_domains


def build_expert_prompt(base_role_path: Path, expertise_domains: list[str], task: str) -> str:
    """Build an expert system prompt by combining base role + dynamic expertise.

    Args:
        base_role_path: Path to the base role YAML (architect.yaml or developer.yaml)
        expertise_domains: Detected expertise domains for this task
        task: The task description for context
    """
    # Load base role
    with open(base_role_path) as f:
        role = yaml.safe_load(f)

    base_prompt = role.get("base_prompt", "")

    # Build expertise section
    expertise_descriptions = []
    for domain in expertise_domains:
        if domain in EXPERTISE_MAP:
            expertise_descriptions.append(f"  - {EXPERTISE_MAP[domain]}")

    expertise_section = "\n".join(expertise_descriptions)

    expert_prompt = f"""{base_prompt}

FOR THIS TASK, you are an expert in:
{expertise_section}

Apply this expertise to evaluate, plan, and (if you are the developer) implement the following task:
{task}

RESPONSE FORMAT:
After your analysis/response, you MUST include a consensus block at the END of your response:
```json
{{"consensus_block": true, "agreed": true/false, "concerns": ["list specific concerns, or empty if none"], "position": "one-line summary of your position"}}
```
IMPORTANT: agreed=true means you support the approach. You can still list concerns as informational notes — concerns don't block agreement. Only set agreed=false if you genuinely believe the approach should change.
"""
    return expert_prompt


def get_role_description(expertise_domains: list[str]) -> str:
    """Get a human-readable description of the assigned expert roles."""
    descriptions = [EXPERTISE_MAP[d] for d in expertise_domains if d in EXPERTISE_MAP]
    return ", ".join(descriptions) if descriptions else "general software engineering"
