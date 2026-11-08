"""The open-source files, and the beginner's guide (Markdown + PDF): present, honest, and consistent with the code."""
import hashlib
import importlib.util
import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).parent.parent
read = lambda p: (ROOT / p).read_text()


# ============================================================ open-source files

def test_license_is_standard_mit_and_the_trading_disclaimer_is_separate_and_linked():
    lic = read("LICENSE")
    assert lic.startswith("MIT License") and "Permission is hereby granted, free of charge" in lic
    assert "WITHOUT WARRANTY OF ANY KIND" in lic
    assert "DISCLAIMER" not in lic.upper().replace("DISCLAIMS", "")        # keep it pure so GitHub recognises "MIT"
    d = read("DISCLAIMER.md")
    for needle in ("not investment advice", "most retail F&O traders lose money", "not affiliated with", "have **not** been verified"):
        assert needle in d, needle
    assert "DISCLAIMER.md" in read("README.md")


def test_contributing_states_every_safety_rule_a_change_must_keep():
    c = read("CONTRIBUTING.md")
    for needle in ("Paper by default", "LIVE_TRADING=true", "types the confirmation phrase", "RiskEngine.evaluate()", "require_approved",
                   "No secrets", "No naked option selling", "No profitability claims", "Tests"):
        assert needle in c or needle.lower() in c.lower(), needle
    assert "SECURITY.md" in c and "CODE_OF_CONDUCT.md" in c


def test_security_policy_says_report_privately_and_never_include_credentials():
    s = read("SECURITY.md")
    assert "do not open a public issue" in s.lower() and "Report a vulnerability" in s
    assert "TOTP secrets" in s and "revoke" in s.lower()


def test_github_templates_and_ci_exist_and_are_valid():
    for f in (".github/pull_request_template.md", ".github/ISSUE_TEMPLATE/bug_report.md",
              ".github/ISSUE_TEMPLATE/feature_request.md", "CODE_OF_CONDUCT.md"):
        assert (ROOT / f).exists(), f
    assert "NOT paste secrets" in read(".github/ISSUE_TEMPLATE/bug_report.md")
    assert "Paper stays the default" in read(".github/pull_request_template.md")
    assert yaml.safe_load(read(".github/ISSUE_TEMPLATE/config.yml"))["blank_issues_enabled"] is False
    wf = yaml.safe_load(read(".github/workflows/tests.yml"))
    steps = wf["jobs"]["test"]["steps"]
    uses = [s["uses"] for s in steps if "uses" in s]
    assert all(re.search(r"@v\d+$", u) for u in uses)                       # pinned to a major version, not @main
    assert any("pytest" in s.get("run", "") for s in steps)
    assert set(wf["jobs"]["test"]["strategy"]["matrix"]["python-version"]) == {"3.11", "3.12", "3.13"}
    assert "continue-on-error" not in wf["jobs"]["test"]                    # every supported version must pass


def test_package_metadata_describes_an_open_source_educational_project():
    import tomllib
    meta = tomllib.loads(read("pyproject.toml"))["project"]
    assert meta["license"]["text"] == "MIT" and meta["readme"] == "README.md" and "paper-first" in meta["description"]
    assert any("Education" in c for c in meta["classifiers"]) and "Alpha" in " ".join(meta["classifiers"])


def test_readme_is_honest_about_independence_status_and_links_the_guide():
    r = read("README.md")
    for needle in ("open-source", "not affiliated with, endorsed by, or supported by Angel One", "Status: early (alpha)",
                   "never been run against the real servers", "MIT", "Beginner's Guide"):
        assert needle in r, needle
    assert "docs/Beginners-Guide.pdf" in r and "docs/user-guide.md" in r
    assert (ROOT / "docs/Beginners-Guide.pdf").exists() and (ROOT / "docs/user-guide.md").exists()
    lowered = r.lower()
    for promise in ("guaranteed return", "guaranteed profit", "risk-free", "risk free", "beat the market", "easy money",
                    "get rich", "passive income", "proven strategy", "consistent profit"):
        assert promise not in lowered, promise                              # no promotional promises, anywhere


# ================================================================= the guide

GUIDE = read("docs/user-guide.md")


def test_guide_covers_the_whole_journey_in_order():
    order = ["docker --version", "git clone", "cp .env.example .env", "TRADER_FEED=simulated", "scripts/start.sh",
             "http://127.0.0.1:8000/", "Create App", "Enable TOTP", "TRADER_FEED=live", "ANGEL_API_KEY=",
             "ANGEL_TOTP_SECRET=", "trader.auth", "trader.alerts whoami", "scripts/review.sh", "scripts/kill.sh",
             "docs/go-live-checklist.md"]
    pos = [GUIDE.find(x) for x in order]
    assert all(p >= 0 for p in pos), [o for o, p in zip(order, pos) if p < 0]
    assert pos == sorted(pos), "steps are out of order"


def test_guide_keeps_the_user_on_paper_and_warns_about_secrets_and_risk():
    assert GUIDE.count("LIVE_TRADING=false") >= 2
    assert "Most retail F&O traders lose money" in GUIDE and "not investment advice" in GUIDE.lower()
    assert "Never share" in GUIDE and "Secret Key" in GUIDE and "does **not** use it" in GUIDE
    assert "static IP" in GUIDE and "may differ" in GUIDE                  # admits Angel One's site can change
    assert "LIVE_TRADING=true" not in GUIDE.replace("`LIVE_TRADING=true` in `.env`, **and**", "")


def test_guide_only_names_settings_scripts_and_commands_that_exist():
    from trader.config import Settings
    fields = {f.upper() for f in Settings.model_fields}
    for var in set(re.findall(r"\b([A-Z][A-Z_]{3,})=", GUIDE)):
        assert var in fields, f"the guide tells users to set {var}, which the program does not read"
    for s in set(re.findall(r"scripts/(\w+\.sh)", GUIDE)):
        assert (ROOT / "scripts" / s).exists(), s
    for mod in set(re.findall(r"python -m (trader[\w.]*)", GUIDE)):
        assert importlib.util.find_spec(mod) is not None, mod
    for img in re.findall(r"!\[[^\]]*\]\(([^)]+)\)", GUIDE):
        assert (ROOT / "docs" / img).exists(), img


def test_every_env_value_shown_in_the_guide_is_obviously_fake():
    for line in re.findall(r"^(ANGEL_\w+|WEBHOOK_SECRET|TELEGRAM_\w+)=(.*)$", GUIDE, re.M):
        assert re.search(r"your|here|paste|example|A123456|1234|YOURLONG|the-long", line[1], re.I) or line[1] == "", line


# ============================================================ the PDF builder

@pytest.fixture(scope="module")
def builder():
    pytest.importorskip("reportlab")
    spec = importlib.util.spec_from_file_location("build_guide_pdf", ROOT / "tools" / "build_guide_pdf.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_markdown_subset_parses_lists_with_code_nested_lists_tables_and_quotes(builder):
    md = ("# T\n\n**sub**\n\n> quote line\n>\n> second\n\n1. first\n   ```\n   code here\n   ```\n2. second\n   - nested a\n   - nested b\n\n"
          "| a | b |\n|---|---|\n| 1 | 2 |\n\n- bullet\n\n![cap](x.png)\n")
    kinds = [b[0] for b in builder.parse(md)]
    assert kinds == ["h1", "p", "quote", "ol", "table", "ul", "image"]
    ol = next(b for b in builder.parse(md) if b[0] == "ol")
    assert [x[0] for x in ol[2][0]] == ["p", "code"] and [x[0] for x in ol[2][1]] == ["p", "ul"]
    table = next(b for b in builder.parse(md) if b[0] == "table")
    assert table[1] == [["a", "b"], ["1", "2"]]


def test_inline_markup_is_safe_and_correct(builder):
    out = builder.inline("**bold** and `a<b & c` and [link](http://x.y) & <script>")
    assert "<b>bold</b>" in out and "a&lt;b &amp; c" in out and 'href="http://x.y"' in out and "&lt;script&gt;" in out
    assert "<script>" not in out
    assert builder.inline("\u20b95 \u2192 ok") == "Rs 5 -&gt; ok"          # characters the PDF font lacks are replaced


def test_the_pdf_builds_with_every_part_bookmarked(builder, tmp_path):
    pypdf = pytest.importorskip("pypdf")
    out = builder.build(builder.SRC, tmp_path / "g.pdf")
    r = pypdf.PdfReader(str(out))
    parts = [p.replace("`", "") for p in re.findall(r"^## (.+)$", GUIDE, re.M)]
    assert [o.title for o in r.outline] == parts and len(r.pages) >= 8
    text = " ".join(p.extract_text() for p in r.pages)
    for needle in ("Most retail F&O traders lose money", "ANGEL_TOTP_SECRET", "KILL SWITCH", "Contents"):
        assert needle in text, needle


def test_the_committed_pdf_matches_the_current_guide():
    """If you edit docs/user-guide.md you must rebuild the PDF:  python tools/build_guide_pdf.py"""
    pypdf = pytest.importorskip("pypdf")
    meta = pypdf.PdfReader(str(ROOT / "docs" / "Beginners-Guide.pdf")).metadata
    want = hashlib.sha256(GUIDE.encode("utf-8")).hexdigest()[:16]
    assert f"[source {want}]" in (meta.subject or ""), "docs/Beginners-Guide.pdf is out of date; run python tools/build_guide_pdf.py"
