"""Static checks on the files an operator relies on, so a careless edit cannot quietly weaken them."""
import re
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).parent.parent
read = lambda p: (ROOT / p).read_text()


# ================================================================== Dockerfile

def test_dockerfile_is_pinned_unprivileged_and_secret_free():
    d = read("Dockerfile")
    assert re.search(r"^FROM python:3\.\d+-slim-\w+\s*$", d, re.M) and "latest" not in d.split("FROM", 1)[1].splitlines()[0]
    assert re.search(r"^USER trader\s*$", d, re.M) and "useradd" in d                      # never root
    assert d.index("requirements.lock") < d.index("COPY src")                              # dependencies cached before code
    assert "pip install --no-deps ." in d and "pip install -r requirements.lock" in d
    assert "HEALTHCHECK" in d and "/health" in d
    assert re.search(r'^CMD \["python", "-m", "trader"\]', d, re.M)
    for forbidden in ("COPY .env", "COPY data", "ADD .env", "LIVE_TRADING", "ANGEL_", "TELEGRAM_", "WEBHOOK_SECRET"):
        assert forbidden not in d, forbidden


def test_dockerignore_keeps_secrets_state_and_history_out_of_the_image():
    ignore = {l.strip() for l in read(".dockerignore").splitlines() if l.strip() and not l.startswith("#")}
    for must in (".env", ".git", "data", "logs", ".venv", "tests", "*.db"):
        assert must in ignore, must
    assert "!.env.example" in ignore                                                       # the template is harmless


# ==================================================================== compose

@pytest.fixture(scope="module")
def compose():
    return yaml.safe_load(read("docker-compose.yml"))["services"]["trader"]


def test_compose_publishes_the_port_to_this_computer_only(compose):
    assert compose["ports"] == ["127.0.0.1:8000:8000"]                                    # never 0.0.0.0 / bare 8000:8000
    assert "network_mode" not in compose


def test_compose_survives_restarts_but_cannot_resume_live_trading_unattended(compose):
    assert compose["restart"] == "unless-stopped"
    flat = yaml.safe_dump(compose)
    assert "LIVE_TRADING" not in flat                                                      # only .env may turn it on
    assert "--feed" not in " ".join(compose["command"]) or "live" in " ".join(compose["command"])
    assert not compose.get("tty") and not compose.get("stdin_open")                        # no way to auto-type the phrase


def test_compose_is_hardened_and_mounts_state_correctly(compose):
    assert compose["read_only"] is True and compose["cap_drop"] == ["ALL"] and "no-new-privileges:true" in compose["security_opt"]
    assert not compose.get("privileged") and not compose.get("cap_add")
    vols = compose["volumes"]
    assert "./config:/app/config:ro" in vols and "./data:/app/data" in vols                # rules read-only, data persistent
    assert not any("docker.sock" in v or v.startswith("/:") or v.startswith("~") for v in vols)
    assert compose["env_file"] == ".env" and compose["healthcheck"]["test"][0] == "CMD"
    cmd = compose["command"]
    assert cmd[:3] == ["python", "-m", "trader"] and "--host" in cmd and "0.0.0.0" in cmd and "--trust-proxy-from-any-peer" in cmd


def test_compose_config_is_valid_when_a_dotenv_exists(tmp_path):
    docker = subprocess.run(["docker", "--version"], capture_output=True)
    if docker.returncode != 0:
        pytest.skip("docker not installed")
    (tmp_path / ".env").write_text("WEBHOOK_SECRET=x\n")
    (tmp_path / "docker-compose.yml").write_text(read("docker-compose.yml"))
    r = subprocess.run(["docker", "compose", "config", "--quiet"], cwd=tmp_path, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


# ================================================================ dependencies

def test_lock_file_pins_every_runtime_dependency_and_no_dev_tools():
    lock = [l.strip() for l in read("requirements.lock").splitlines() if l.strip() and not l.startswith("#")]
    assert lock and all("==" in l for l in lock)
    names = {l.split("==")[0].lower().replace("_", "-") for l in lock}
    assert not names & {"pytest", "httpx", "pluggy", "iniconfig"}
    for l in read("requirements.txt").splitlines():
        m = re.match(r"^([A-Za-z0-9_.\-]+)==([\w.]+)", l)
        if m:
            assert f"{m.group(1)}=={m.group(2)}".lower() in {x.lower() for x in lock}, m.group(0)


# ==================================================================== scripts

@pytest.mark.parametrize("script", sorted(p.name for p in (ROOT / "scripts").glob("*.sh")))
def test_helper_scripts_are_executable_and_valid_shell(script):
    p = ROOT / "scripts" / script
    assert p.stat().st_mode & 0o111, "not executable"
    assert subprocess.run(["bash", "-n", str(p)], capture_output=True).returncode == 0
    assert "LIVE_TRADING" not in p.read_text()


def test_the_scripts_the_readme_promises_exist():
    readme = read("README.md")
    for s in set(re.findall(r"scripts/(\w+\.sh)", readme)):
        assert (ROOT / "scripts" / s).exists(), s


# ================================================================ config + docs

def test_every_shipped_config_file_loads_with_its_own_loader():
    from trader.alerts import load_alerts_config
    from trader.execution import load_execution_config
    from trader.feed import load_feed_config
    from trader.journal import load_journal_config
    from trader.ops import load_ops_config
    from trader.risk import load_risk_config
    from trader.strategy import build_strategies, load_strategy_config
    from trader.webhook import load_webhook_config
    for fn in (load_alerts_config, load_execution_config, load_feed_config, load_journal_config, load_ops_config,
               load_risk_config, load_webhook_config):
        fn()
    assert build_strategies(load_strategy_config())


def test_every_config_version_has_a_changelog_entry():
    log = read("config/CHANGELOG.md")
    for f in sorted((ROOT / "config").glob("*.yaml")):
        v = yaml.safe_load(f.read_text())["version"]
        assert re.search(rf"{re.escape(f.name)} v{v}\b", log), f"config/CHANGELOG.md has no entry for {f.name} v{v}"


def test_changelog_and_experiments_templates_have_the_human_in_the_loop_fields():
    log, exp = read("config/CHANGELOG.md"), read("config/EXPERIMENTS.md")
    for needle in ("**What changed:**", "**Why:**", "**Expected effect:**", "**Result after N days:**", "git add config"):
        assert needle in log, needle
    for needle in ("One change at a time", "BEFORE you look at results", "**Hypothesis:**", "**Success test",
                   "**Out-of-sample check:**", "**Decision:**", "Weekly review"):
        assert needle in exp, needle


def test_env_example_lists_every_setting_the_program_reads_and_no_real_secrets():
    from trader.config import Settings
    example = read(".env.example")
    keys = set(re.findall(r"^([A-Z_]+)=", example, re.M))
    for field in Settings.model_fields:
        assert field.upper() in keys, f"{field.upper()} missing from .env.example"
    assert re.search(r"^LIVE_TRADING=false\s*$", example, re.M)
    for line in example.splitlines():
        if "=" in line and not line.startswith("#"):
            assert line.split("=", 1)[1].strip() in ("", "false", "live"), f"example file must hold no values: {line}"


def test_readme_carries_the_risk_disclaimer_and_the_operator_runbook():
    r = read("README.md")
    assert "most retail f&o traders lose money" in r.lower()                                 # rule 5, in plain words
    for needle in ("Markets are risky", "not tested for profit", "Kill switch", "KILL SWITCH", "Every morning",
                   "Every evening", "evening review", "Market data down", "Going live", "LIVE_TRADING", "go-live-checklist",
                   "What has and has not been verified", "dead man's switch"):
        assert needle in r, needle
    assert "profitable" in r.lower() and "promises or implies" in r                          # explicit no-profit claim


def test_every_relative_link_in_the_docs_points_at_a_file_that_exists():
    for doc in [ROOT / "README.md", *sorted((ROOT / "docs").glob("*.md"))]:
        for target in re.findall(r"\]\((?!https?://|#|mailto:|\.\./)([^)\s#]+)", doc.read_text()):   # ../ = GitHub-only links
            assert (doc.parent / target).exists(), f"{doc.name} links to missing {target}"


def test_go_live_checklist_has_the_gates_the_brief_requires():
    g = read("docs/go-live-checklist.md")
    for needle in ("4 full weeks", "static IP", "One lot", "`max_lots_per_order: 1`", "Kill switch tested", "LIVE_TRADING=true",
                   "dead man's switch", "unattended", "never been run against the real"):
        assert needle in g or needle.lower() in g.lower(), needle
    assert len(re.findall(r"- \[ \]", g)) >= 25
