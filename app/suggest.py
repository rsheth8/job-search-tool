"""Chip suggestions that keep coming.

A fixed row of eight skills is a list of eight skills. What job boards actually
do is refill: you pick React and TypeScript appears, you pick PyTorch and CUDA
does. The pool feels bottomless because it is large *and* because what surfaces
next is related to what you just chose.

Both halves live here. ``CATALOG`` is the vocabulary -- long enough that nobody
reaches the end of it -- and ``_CLUSTERS`` says which terms belong together, so
a pick pulls its neighbours forward instead of returning the next alphabetical
entry. No LLM call: this is a ranking over a static list, so it is instant,
free, and gives the same answer twice.
"""
from __future__ import annotations

import re

DEFAULT_LIMIT = 12
MAX_LIMIT = 40
MAX_CHOSEN = 60
_PICK_WEIGHT = 3
_CONTEXT_WEIGHT = 1


# --- vocabularies ----------------------------------------------------------

_SKILLS = (
    # languages
    "Python", "JavaScript", "TypeScript", "Java", "C++", "C", "C#", "Go",
    "Rust", "Swift", "Kotlin", "Ruby", "PHP", "Scala", "R", "MATLAB", "SQL",
    "Bash", "Perl", "Haskell", "Elixir", "Dart", "Objective-C", "Julia",
    # frontend
    "React", "Next.js", "Vue", "Angular", "Svelte", "Redux", "HTML", "CSS",
    "Tailwind CSS", "SASS", "Webpack", "Vite", "jQuery", "Accessibility",
    "Responsive design", "Storybook",
    # backend / api
    "Node.js", "Express", "Django", "Flask", "FastAPI", "Spring Boot",
    "Rails", ".NET", "GraphQL", "REST APIs", "gRPC", "Microservices",
    "WebSockets", "OAuth", "Celery",
    # data stores
    "PostgreSQL", "MySQL", "MongoDB", "Redis", "SQLite", "DynamoDB",
    "Cassandra", "Elasticsearch", "Snowflake", "BigQuery", "Redshift",
    "Neo4j", "Database design",
    # cloud / infra
    "AWS", "GCP", "Azure", "Docker", "Kubernetes", "Terraform", "Ansible",
    "CI/CD", "Jenkins", "GitHub Actions", "Linux", "Nginx", "Serverless",
    "Lambda", "Observability", "Datadog", "Prometheus", "Grafana",
    # data / ml
    "Machine learning", "Deep learning", "PyTorch", "TensorFlow",
    "scikit-learn", "Pandas", "NumPy", "Keras", "Hugging Face", "LLMs",
    "NLP", "Computer vision", "Reinforcement learning", "MLOps", "CUDA",
    "Spark", "Hadoop", "Airflow", "dbt", "Kafka", "ETL", "Data modeling",
    "Statistics", "A/B testing", "Feature engineering", "Recommender systems",
    "Time series", "Data visualization", "Tableau", "Power BI", "Looker",
    "Jupyter", "Databricks",
    # mobile
    "iOS", "Android", "SwiftUI", "UIKit", "React Native", "Flutter",
    "Jetpack Compose", "Core Data", "App Store deployment",
    # security
    "Cybersecurity", "Penetration testing", "Cryptography", "Threat modeling",
    "SIEM", "Incident response", "Network security",
    # systems / other engineering
    "Distributed systems", "Operating systems", "Compilers", "Embedded systems",
    "Firmware", "Computer networks", "Algorithms", "Data structures",
    "System design", "Performance tuning", "Concurrency", "Game development",
    "Unity", "Unreal Engine", "AR/VR", "Robotics", "ROS", "Signal processing",
    "FPGA", "Verilog", "CAD", "SolidWorks",
    # practice / tooling
    "Git", "Agile", "Scrum", "Code review", "Unit testing", "Pytest", "Jest",
    "Selenium", "Playwright", "Test automation", "Debugging", "Documentation",
    "Technical writing", "Pair programming",
    # product / analysis / business
    "Product management", "User research", "Figma", "UI/UX design",
    "Prototyping", "Wireframing", "Design systems", "Excel", "Financial modeling",
    "Business analysis", "Requirements gathering", "Stakeholder management",
    "Project management", "Jira", "Salesforce", "SEO", "Digital marketing",
    "Content strategy", "Customer support",
    # human
    "Communication", "Leadership", "Mentoring", "Teamwork", "Problem solving",
    "Public speaking", "Cross-functional collaboration", "Time management",
)

_ROLES = (
    "Software engineer", "Software engineer intern", "Backend engineer",
    "Frontend engineer", "Full-stack engineer", "Mobile engineer",
    "iOS engineer", "Android engineer", "Platform engineer",
    "Infrastructure engineer", "DevOps engineer", "Site reliability engineer",
    "Cloud engineer", "Security engineer", "Data engineer", "Data scientist",
    "Data analyst", "Machine learning engineer", "ML infrastructure engineer",
    "Research engineer", "Research scientist", "AI engineer",
    "Business intelligence analyst", "Analytics engineer",
    "Quantitative analyst", "Quantitative developer", "Embedded engineer",
    "Firmware engineer", "Hardware engineer", "Electrical engineer",
    "Mechanical engineer", "Systems engineer", "Network engineer",
    "QA engineer", "Test engineer", "Automation engineer",
    "Solutions engineer", "Sales engineer", "Support engineer",
    "Developer advocate", "Technical program manager", "Product manager",
    "Associate product manager", "Product designer", "UX designer",
    "UX researcher", "Game developer", "Graphics engineer",
    "Robotics engineer", "Computer vision engineer", "NLP engineer",
    "Database administrator", "Technical writer", "IT support specialist",
    "Consultant", "Business analyst", "Operations analyst",
    "Financial analyst", "Investment banking analyst", "Actuarial analyst",
    "Marketing analyst", "Growth analyst", "Program manager",
    "Project manager", "Engineering manager", "New grad software engineer",
    "Summer intern", "Co-op engineer",
)

_LOCATIONS = (
    "Remote", "Hybrid", "Minneapolis", "St. Paul", "Chicago", "Milwaukee",
    "Madison", "Des Moines", "Kansas City", "St. Louis", "Detroit",
    "Columbus", "Cleveland", "Cincinnati", "Indianapolis", "Pittsburgh",
    "New York", "Brooklyn", "Jersey City", "Boston", "Cambridge",
    "Philadelphia", "Washington DC", "Arlington", "Baltimore", "Richmond",
    "Raleigh", "Durham", "Charlotte", "Atlanta", "Nashville", "Miami",
    "Tampa", "Orlando", "Austin", "Dallas", "Houston", "San Antonio",
    "Denver", "Boulder", "Salt Lake City", "Phoenix", "Las Vegas",
    "San Francisco", "South San Francisco", "Palo Alto", "Mountain View",
    "Sunnyvale", "San Jose", "Oakland", "Berkeley", "Los Angeles",
    "Santa Monica", "San Diego", "Irvine", "Seattle", "Bellevue", "Redmond",
    "Portland", "Toronto", "Vancouver", "Montreal", "London", "Dublin",
    "Berlin", "Amsterdam", "Zurich", "Bangalore", "Hyderabad", "Singapore",
    "Sydney", "Tokyo",
)

_DISCIPLINES = (
    "Computer Science", "Computer Engineering", "Software Engineering",
    "Data Science", "Statistics", "Mathematics", "Applied Mathematics",
    "Electrical Engineering", "Mechanical Engineering", "Civil Engineering",
    "Chemical Engineering", "Biomedical Engineering", "Industrial Engineering",
    "Aerospace Engineering", "Materials Science", "Information Systems",
    "Information Technology", "Cybersecurity", "Artificial Intelligence",
    "Machine Learning", "Robotics", "Physics", "Chemistry", "Biology",
    "Neuroscience", "Bioinformatics", "Economics", "Finance", "Accounting",
    "Business Administration", "Marketing", "Management", "Supply Chain",
    "Operations Research", "Psychology", "Cognitive Science", "Linguistics",
    "Philosophy", "Political Science", "Sociology", "Communications",
    "English", "History", "Graphic Design", "Human-Computer Interaction",
    "Environmental Science", "Public Health", "Nursing", "Education",
)

_DEGREES = (
    "B.S.", "B.A.", "B.Eng.", "M.S.", "M.A.", "M.Eng.", "MBA", "Ph.D.",
    "Associate", "Certificate", "Bootcamp", "High school diploma",
)

_HOW_HEARD = (
    "LinkedIn", "Company website", "Indeed", "Handshake", "Glassdoor",
    "Referral from a friend", "Referral from an employee", "University career fair",
    "Career services", "Recruiter outreach", "GitHub", "Hacker News",
    "Twitter/X", "Conference", "Job board", "Google search", "Professor",
    "Student organization", "Alumni network", "Slack community", "Discord",
)

_WORK_ARRANGEMENT = ("Remote", "Hybrid", "On-site", "Flexible", "Open to any")

_LANGUAGES = (
    "English", "Spanish", "Mandarin", "Hindi", "Gujarati", "French", "German",
    "Arabic", "Portuguese", "Russian", "Japanese", "Korean", "Italian",
    "Bengali", "Punjabi", "Tamil", "Telugu", "Urdu", "Vietnamese", "Tagalog",
    "Polish", "Dutch", "Turkish", "Swahili", "Hebrew", "American Sign Language",
)

CATALOG: dict[str, tuple[str, ...]] = {
    "skills": _SKILLS,
    "roles": _ROLES,
    "locations": _LOCATIONS,
    "disciplines": _DISCIPLINES,
    "degrees": _DEGREES,
    "how_heard": _HOW_HEARD,
    "work_arrangement": _WORK_ARRANGEMENT,
    "languages": _LANGUAGES,
}

FIELDS = tuple(CATALOG)


# --- what sits next to what ------------------------------------------------
# Only groupings that make a pick genuinely informative. A term can be in
# several: React is frontend *and* JavaScript, which is why picking it surfaces
# both Next.js and Node.js.

_CLUSTERS: dict[str, tuple[str, ...]] = {
    "frontend": (
        "React", "Next.js", "Vue", "Angular", "Svelte", "Redux", "HTML", "CSS",
        "Tailwind CSS", "SASS", "Webpack", "Vite", "jQuery", "Accessibility",
        "Responsive design", "Storybook", "UI/UX design", "Design systems",
        "Figma", "Frontend engineer", "Product designer", "UX designer",
    ),
    "javascript": (
        "JavaScript", "TypeScript", "React", "Next.js", "Vue", "Angular",
        "Svelte", "Node.js", "Express", "Jest", "Redux", "GraphQL",
    ),
    "backend": (
        "Node.js", "Express", "Django", "Flask", "FastAPI", "Spring Boot",
        "Rails", ".NET", "GraphQL", "REST APIs", "gRPC", "Microservices",
        "WebSockets", "OAuth", "Celery", "Backend engineer", "Database design",
        "System design", "Distributed systems",
    ),
    "python": (
        "Python", "Django", "Flask", "FastAPI", "Pandas", "NumPy", "Pytest",
        "scikit-learn", "Celery", "Jupyter", "Airflow",
    ),
    "java": ("Java", "Spring Boot", "Kotlin", "Scala", "Android"),
    "data": (
        "SQL", "PostgreSQL", "MySQL", "Snowflake", "BigQuery", "Redshift",
        "Spark", "Hadoop", "Airflow", "dbt", "Kafka", "ETL", "Data modeling",
        "Databricks", "Data engineer", "Analytics engineer", "Tableau",
        "Power BI", "Looker", "Data visualization", "Data analyst",
        "Business intelligence analyst", "Excel",
    ),
    "ml": (
        "Machine learning", "Deep learning", "PyTorch", "TensorFlow",
        "scikit-learn", "Keras", "Hugging Face", "LLMs", "NLP",
        "Computer vision", "Reinforcement learning", "MLOps", "CUDA",
        "Feature engineering", "Recommender systems", "Time series",
        "Statistics", "Machine learning engineer", "Data scientist",
        "Research scientist", "AI engineer", "Artificial Intelligence",
        "Data Science", "Jupyter", "NumPy", "Pandas",
    ),
    "cloud": (
        "AWS", "GCP", "Azure", "Docker", "Kubernetes", "Terraform", "Ansible",
        "CI/CD", "Jenkins", "GitHub Actions", "Linux", "Nginx", "Serverless",
        "Lambda", "Observability", "Datadog", "Prometheus", "Grafana",
        "DevOps engineer", "Site reliability engineer", "Platform engineer",
        "Cloud engineer", "Infrastructure engineer",
    ),
    "mobile": (
        "iOS", "Android", "SwiftUI", "UIKit", "React Native", "Flutter",
        "Jetpack Compose", "Core Data", "App Store deployment", "Swift",
        "Kotlin", "Dart", "Mobile engineer", "iOS engineer", "Android engineer",
    ),
    "security": (
        "Cybersecurity", "Penetration testing", "Cryptography",
        "Threat modeling", "SIEM", "Incident response", "Network security",
        "Security engineer", "OAuth", "Linux",
    ),
    "systems": (
        "C", "C++", "Rust", "Go", "Distributed systems", "Operating systems",
        "Compilers", "Embedded systems", "Firmware", "Computer networks",
        "Concurrency", "Performance tuning", "Algorithms", "Data structures",
        "System design", "Embedded engineer", "Firmware engineer",
    ),
    "hardware": (
        "Verilog", "FPGA", "Signal processing", "Embedded systems", "Firmware",
        "CAD", "SolidWorks", "Robotics", "ROS", "Electrical Engineering",
        "Mechanical Engineering", "Hardware engineer", "Robotics engineer",
        "MATLAB",
    ),
    "games": (
        "Game development", "Unity", "Unreal Engine", "AR/VR", "C#", "C++",
        "Game developer", "Graphics engineer",
    ),
    "quality": (
        "Unit testing", "Pytest", "Jest", "Selenium", "Playwright",
        "Test automation", "Debugging", "Code review", "QA engineer",
        "Test engineer", "Automation engineer",
    ),
    "product": (
        "Product management", "User research", "Figma", "UI/UX design",
        "Prototyping", "Wireframing", "Design systems", "Jira", "Agile",
        "Scrum", "Stakeholder management", "Requirements gathering",
        "Product manager", "Associate product manager",
        "Technical program manager", "Business analysis",
    ),
    "business": (
        "Excel", "Financial modeling", "Business analysis", "Salesforce",
        "SEO", "Digital marketing", "Content strategy", "Project management",
        "Economics", "Finance", "Accounting", "Business Administration",
        "Financial analyst", "Business analyst", "Consultant",
        "Marketing analyst",
    ),
    "people": (
        "Communication", "Leadership", "Mentoring", "Teamwork",
        "Problem solving", "Public speaking", "Cross-functional collaboration",
        "Time management", "Technical writing", "Documentation",
        "Pair programming", "Engineering manager",
    ),
    # locations that trade candidates with each other
    "bay_area": (
        "San Francisco", "South San Francisco", "Palo Alto", "Mountain View",
        "Sunnyvale", "San Jose", "Oakland", "Berkeley", "Remote", "Hybrid",
    ),
    "pnw": ("Seattle", "Bellevue", "Redmond", "Portland", "Vancouver", "Remote"),
    "midwest": (
        "Minneapolis", "St. Paul", "Chicago", "Milwaukee", "Madison",
        "Des Moines", "Kansas City", "St. Louis", "Detroit", "Columbus",
        "Cleveland", "Cincinnati", "Indianapolis",
    ),
    "northeast": (
        "New York", "Brooklyn", "Jersey City", "Boston", "Cambridge",
        "Philadelphia", "Pittsburgh", "Washington DC", "Arlington", "Baltimore",
    ),
    "southeast": (
        "Raleigh", "Durham", "Charlotte", "Atlanta", "Nashville", "Miami",
        "Tampa", "Orlando", "Richmond",
    ),
    "texas": ("Austin", "Dallas", "Houston", "San Antonio"),
    "mountain": ("Denver", "Boulder", "Salt Lake City", "Phoenix", "Las Vegas"),
    "socal": ("Los Angeles", "Santa Monica", "San Diego", "Irvine"),
}


def _norm(term: str) -> str:
    return re.sub(r"[^a-z0-9+#.]+", "", (term or "").strip().lower())


# What a stored profile says is rarely a catalog entry. "ML engineer" is not
# "Machine learning engineer", and someone whose locations read "NYC" has still
# told us where they want to work.
_ALIASES: dict[str, tuple[str, ...]] = {
    "ml": ("ml",),
    "ai": ("ml",),
    "llm": ("ml",),
    "genai": ("ml",),
    "ds": ("ml", "data"),
    "devops": ("cloud",),
    "sre": ("cloud",),
    "infra": ("cloud",),
    "cloud": ("cloud",),
    "frontend": ("frontend", "javascript"),
    "fullstack": ("frontend", "backend"),
    "backend": ("backend",),
    "mobile": ("mobile",),
    "cyber": ("security",),
    "infosec": ("security",),
    "appsec": ("security",),
    "qa": ("quality",),
    "sdet": ("quality",),
    "pm": ("product",),
    "apm": ("product",),
    "tpm": ("product",),
    "quant": ("business", "data"),
    "analytics": ("data",),
    "embedded": ("systems", "hardware"),
    "nyc": ("northeast",),
    "dmv": ("northeast",),
    "sf": ("bay_area",),
    "bayarea": ("bay_area",),
    "norcal": ("bay_area",),
    "socal": ("socal",),
    "la": ("socal",),
    "twincities": ("midwest",),
    "pnw": ("pnw",),
}

# Longest phrase we bother assembling out of a free-text term. "Machine
# learning engineer" is three; nothing in the catalog is longer than four.
_MAX_PHRASE = 4


def _build_index() -> dict[str, frozenset[str]]:
    index: dict[str, set[str]] = {}
    for name, members in _CLUSTERS.items():
        for member in members:
            index.setdefault(_norm(member), set()).add(name)
    return {k: frozenset(v) for k, v in index.items()}


_CLUSTER_OF = _build_index()


def clusters_of(term: str) -> frozenset[str]:
    """Clusters for one term, matching it exactly."""
    return _CLUSTER_OF.get(_norm(term), frozenset())


def clusters_in(phrase: str) -> frozenset[str]:
    """Clusters anywhere inside a free-text phrase.

    Catalog terms arrive clean; profile fields do not. This walks the word
    n-grams of a phrase so "Senior ML engineer, backend" finds both ``ml`` and
    ``backend`` -- neither of which is a catalog entry.
    """
    words = [w for w in re.split(r"[^a-z0-9+#.]+", (phrase or "").lower()) if w]
    found: set[str] = set()
    for start in range(len(words)):
        for size in range(1, _MAX_PHRASE + 1):
            key = "".join(words[start:start + size])
            if not key:
                break
            found |= _CLUSTER_OF.get(key, frozenset())
            found.update(_ALIASES.get(key, ()))
    return frozenset(found)


def next_batch(field: str, chosen=(), *, context=(), query: str = "",
               limit: int = DEFAULT_LIMIT) -> dict:
    """The next chips to show, given what has been picked already.

    ``chosen`` is what the user has selected -- excluded from the result and
    used to rank it. ``context`` is anything else known about them (their
    roles, their major) which seeds a useful first batch before they have
    picked anything at all, so the opening row is not the same generic eight
    for a mechanical engineer as for an ML researcher.
    """
    catalog = CATALOG.get(field)
    if catalog is None:
        return {"field": field, "suggestions": [], "remaining": 0, "known": False}

    limit = max(1, min(int(limit or DEFAULT_LIMIT), MAX_LIMIT))
    taken = {_norm(c) for c in list(chosen)[:MAX_CHOSEN] if _norm(c)}
    pool = [t for t in catalog if _norm(t) not in taken]

    if query:
        needle = _norm(query)
        pool = [t for t in pool if needle in _norm(t)]

    # Weight the clusters the user has pointed at. A tap is evidence and adds
    # up -- Docker then Kubernetes pulls harder on infra than Docker alone. The
    # stored profile is a guess, so it contributes once per cluster no matter
    # how many profile terms land in it; three ML-flavoured profile strings
    # must not outweigh the thing they just tapped.
    weights: dict[str, int] = {}
    for term in list(chosen)[:MAX_CHOSEN]:
        for name in clusters_in(term):
            weights[name] = weights.get(name, 0) + _PICK_WEIGHT
    for name in {c for term in list(context)[:MAX_CHOSEN] for c in clusters_in(term)}:
        weights[name] = weights.get(name, 0) + _CONTEXT_WEIGHT

    def rank(index_term):
        index, term = index_term
        score = sum(weights.get(name, 0) for name in clusters_of(term))
        # Catalog order breaks ties, so an unranked list is still the sensible
        # "most people want these first" order rather than alphabetical noise.
        return (-score, index)

    ordered = [t for _, t in sorted(enumerate(pool), key=rank)]
    return {
        "field": field,
        "suggestions": ordered[:limit],
        "remaining": max(0, len(ordered) - limit),
        "known": True,
    }
