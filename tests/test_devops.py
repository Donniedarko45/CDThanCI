"""Validation tests for DevOps, CI/CD, Docker, and packaging configuration."""

from __future__ import annotations

import fnmatch
from pathlib import Path

import yaml

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    import tomli as tomllib

ROOT = Path(__file__).resolve().parent.parent


def _load_yaml(path: Path) -> dict:
    assert path.is_file(), f"Missing YAML file: {path}"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict), f"Expected YAML mapping in {path}"
    return data


def test_ci_workflow_valid_and_complete():
    ci = _load_yaml(ROOT / ".github" / "workflows" / "ci.yml")
    triggers = ci.get("on") if "on" in ci else ci.get(True)
    assert isinstance(triggers, dict)
    assert {"push", "pull_request", "workflow_dispatch"}.issubset(triggers.keys())
    assert ci.get("concurrency", {}).get("cancel-in-progress") is True

    jobs = ci.get("jobs", {})
    assert {"lint-and-package", "test-matrix", "e2e-and-ui", "docker-smoke"}.issubset(jobs.keys())
    matrix = jobs["test-matrix"]["strategy"]["matrix"]["python-version"]
    assert "3.10" in matrix and "3.12" in matrix

    lint_text = "\n".join(str(step.get("run", "")) for step in jobs["lint-and-package"]["steps"])
    assert "ruff check" in lint_text
    assert "ruff format --check" in lint_text
    assert "node --check" in lint_text
    assert "python -m build" in lint_text
    for asset in (
        "report_template.html",
        "static/index.html",
        "static/app.js",
        "static/charts.js",
        "static/styles.css",
    ):
        assert asset in lint_text

    e2e_text = "\n".join(str(step.get("run", "")) for step in jobs["e2e-and-ui"]["steps"])
    for endpoint in (
        "/static/app.js",
        "/report",
        "/health",
        "/dashboard/data",
        "/api/config",
        "/api/analytics",
        "/predict",
        "/predict/batch",
    ):
        assert endpoint in e2e_text
    assert "#themeToggleBtn" in e2e_text


def test_cd_and_dependabot_workflows_valid():
    cd = _load_yaml(ROOT / ".github" / "workflows" / "cd.yml")
    triggers = cd.get("on") if "on" in cd else cd.get(True)
    assert isinstance(triggers, dict)
    assert "push" in triggers and "workflow_dispatch" in triggers

    jobs = cd.get("jobs", {})
    assert {"build-artifacts", "docker-publish", "release"}.issubset(jobs.keys())
    cd_raw = (ROOT / ".github" / "workflows" / "cd.yml").read_text(encoding="utf-8")
    assert "ghcr.io" in cd_raw
    assert "attest-build-provenance" in cd_raw

    dep = _load_yaml(ROOT / ".github" / "dependabot.yml")
    assert dep.get("version") == 2
    ecosystems = {entry["package-ecosystem"]: entry for entry in dep.get("updates", [])}
    assert {"pip", "github-actions", "docker"}.issubset(ecosystems.keys())
    for entry in ecosystems.values():
        assert entry["schedule"]["interval"] == "weekly"


def test_docker_compose_valid_and_has_demo_profile():
    compose = _load_yaml(ROOT / "docker-compose.yml")
    services = compose.get("services", {})
    assert {"scheduler", "api", "demo-seed", "demo-api"}.issubset(services.keys())

    assert "healthcheck" in services["api"]
    assert "/health" in str(services["api"]["healthcheck"]["test"])
    assert "healthcheck" in services["demo-api"]
    assert "/health" in str(services["demo-api"]["healthcheck"]["test"])

    assert "demo" in services["demo-seed"].get("profiles", [])
    assert "demo" in services["demo-api"].get("profiles", [])
    assert any("8000" in str(port) for port in services["demo-api"].get("ports", []))


def test_dockerfile_and_dockerignore_hardened():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "FROM python:3.12-slim" in dockerfile
    assert "PYTHONDONTWRITEBYTECODE=1" in dockerfile
    assert "PYTHONUNBUFFERED=1" in dockerfile
    assert "USER aqi" in dockerfile
    assert "USER root" not in dockerfile.splitlines()[-1]
    assert "EXPOSE 8000" in dockerfile
    assert "HEALTHCHECK" in dockerfile
    assert "127.0.0.1:8000/health" in dockerfile
    assert "/data" in dockerfile

    dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8")
    lines = {line.strip() for line in dockerignore.splitlines() if line.strip() and not line.strip().startswith("#")}
    expected = {
        ".git",
        ".github",
        ".venv",
        ".uv-cache",
        "runs",
        "data",
        "tests",
        "__pycache__",
        "*.pyc",
        "*.db*",
        "*.zip",
        ".pytest_cache",
        ".coverage",
    }
    missing = expected - lines
    assert not missing, f"Missing .dockerignore entries: {sorted(missing)}"


def test_pyproject_packaging_and_static_package_data():
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    dev_deps = " ".join(data["project"]["optional-dependencies"]["dev"])
    for pkg in ("ruff", "pytest-cov", "build"):
        assert pkg in dev_deps

    assert data["tool"]["ruff"]["target-version"] == "py310"
    assert data["tool"]["ruff"]["line-length"] == 120
    assert set(data["tool"]["ruff"]["lint"]["select"]) >= {"E", "F", "I", "W"}
    assert data["tool"]["coverage"]["run"]["source"] == ["src/aqi_pipeline"]

    patterns = data["tool"]["setuptools"]["package-data"]["aqi_pipeline"]
    pkg_dir = ROOT / "src" / "aqi_pipeline"
    assert (pkg_dir / "report_template.html").is_file()
    assert any(fnmatch.fnmatch("report_template.html", pat) for pat in patterns)

    static_dir = pkg_dir / "static"
    assert static_dir.is_dir(), "src/aqi_pipeline/static directory must exist"
    static_files = [p.relative_to(pkg_dir).as_posix() for p in static_dir.rglob("*") if p.is_file()]
    assert static_files, "src/aqi_pipeline/static must contain frontend assets"
    for rel in static_files:
        assert any(fnmatch.fnmatch(rel, pat) for pat in patterns), f"{rel} not covered by package-data {patterns}"
