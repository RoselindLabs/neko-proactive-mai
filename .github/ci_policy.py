"""Standalone CI policy, vendored unchanged as .github/ci_policy.py in plugins.

Only test routing: unknown diffs run tests; documentation success cannot publish.
No production configuration, third-party actions, deployment or model requests.
"""

import argparse
import itertools
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import urllib.parse
import urllib.request

PRODUCTION_PYTHON = "3.14.4"
PRODUCTION_SDK = "==2.23.0"


def is_documentation(name):
    path = PurePosixPath(name)
    return (
        not path.is_absolute()
        and ".." not in path.parts
        and path.suffix.lower() == ".md"
        and (len(path.parts) == 1 or path.parts[0] == "docs")
    )


def classify_paths(paths):
    return {"runtime": paths is None or any(not is_documentation(p) for p in paths)}


def git_paths(base, head, root="."):
    if not all(
        isinstance(v, str) and re.fullmatch("[a-f0-9]{40}", v) and set(v) != {"0"}
        for v in (base, head)
    ):
        return None
    try:
        # No rename inference: both deleted old and added new paths are considered.
        result = subprocess.run(
            ["git", "diff", "--no-renames", "--name-only", "-z", base, head, "--"],
            cwd=root,
            capture_output=True,
            timeout=20,
        )
        if result.returncode or len(result.stdout) > 8 * 1024 * 1024:
            return None
        names = result.stdout.decode("utf-8").split("\0")
        if names[-1] != "":
            return None
        return [name for name in names[:-1] if name]
    except (OSError, UnicodeError, subprocess.TimeoutExpired):
        return None


def api(path):
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        raise ValueError("invalid repository")
    request = urllib.request.Request(
        "https://api.github.com/repos/" + repo + path,
        headers={
            "Authorization": "Bearer " + os.environ.get("GITHUB_TOKEN", ""),
            "Accept": "application/vnd.github+json",
        },
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        raw = response.read(4 * 1024 * 1024 + 1)
    if len(raw) > 4 * 1024 * 1024:
        raise ValueError("response too large")
    return json.loads(raw)


def last_compatibility_head(workflow):
    if workflow not in {"tests.yml", "ci.yml"}:
        return None
    try:
        runs = api(
            "/actions/workflows/" + workflow + "/runs?branch=main&status=completed&per_page=20"
        )["workflow_runs"]
        for run in runs:
            if run.get("head_branch") != "main" or run.get("conclusion") != "success":
                continue
            jobs = api("/actions/runs/" + str(int(run["id"])) + "/jobs?filter=latest&per_page=100")[
                "jobs"
            ]
            if any(
                j.get("name") == "compatibility-proof"
                and j.get("status") == "completed"
                and j.get("conclusion") == "success"
                for j in jobs
            ):
                return run.get("head_sha")
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


def event_changes(event, workflow="tests.yml"):
    name = os.environ.get("GITHUB_EVENT_NAME", "")
    head = os.environ.get("GITHUB_SHA", "")
    if name == "schedule":
        # A failed/inaccessible comparison is conservative: run the full matrix.
        paths = git_paths(last_compatibility_head(workflow), head)
        return paths, classify_paths(paths)["runtime"]
    if name == "workflow_dispatch" or os.environ.get("GITHUB_REF", "").startswith("refs/tags/"):
        return None, name != "workflow_dispatch" or event.get("inputs", {}).get("full") in (
            True,
            "true",
        )
    if name == "push":
        return git_paths(event.get("before"), head), False
    if name == "pull_request":
        try:
            base = event["pull_request"]["base"]["sha"]
            ancestor = (
                subprocess.check_output(["git", "merge-base", base, head], timeout=20)
                .decode()
                .strip()
            )
            return git_paths(ancestor, head), False
        except (OSError, ValueError, KeyError, subprocess.SubprocessError):
            return None, False
    return None, False


def plugin_matrix(plugin, full=False):
    minimum = "==2.10.0" if plugin in {"neko-tts", "neko-server-status"} else "==2.12.0"
    oldest = "3.10" if plugin == "neko-server-status" else "3.12"
    production = {
        "os": "ubuntu-24.04",
        "python": PRODUCTION_PYTHON,
        "sdk": "production",
        "sdk_spec": PRODUCTION_SDK,
        "case": "linux-production",
    }
    windows = {
        "os": "windows-latest",
        "python": oldest,
        "sdk": "minimum",
        "sdk_spec": minimum,
        "case": "windows-minimum",
    }
    if not full:
        return {"include": [production, windows]}
    pythons = ["3.10", "3.12", "3.14"] if plugin == "neko-server-status" else ["3.12", "3.14"]
    sdks = [
        ("minimum", minimum),
        ("latest", ">=2.10,<3" if plugin == "neko-server-status" else ">=2.12,<3"),
    ]
    if plugin == "neko-tts":
        sdks.insert(1, ("baseline", "==2.12.0"))
    rows = [production]
    for system, python, (sdk, spec) in itertools.product(
        ["ubuntu-24.04", "windows-latest"], pythons, sdks
    ):
        case = "compat-" + system.split("-")[0] + "-py" + python + "-" + sdk
        if system == "windows-latest" and python == oldest and sdk == "minimum":
            case = "windows-minimum"
        rows.append({"os": system, "python": python, "sdk": sdk, "sdk_spec": spec, "case": case})
    return {"include": rows}  # All original8/12 cases plus one pinned production anchor.


def emit(values):
    lines = []
    for key, value in values.items():
        encoded = (
            str(value).lower()
            if isinstance(value, bool)
            else json.dumps(value, separators=(",", ":"))
            if isinstance(value, (dict, list))
            else str(value)
        )
        if "\n" in encoded or "\r" in encoded:
            raise ValueError("invalid output")
        lines.append(key + "=" + encoded)
    destination = os.environ.get("GITHUB_OUTPUT")
    if destination:
        with open(destination, "a", encoding="utf-8") as file:
            file.write("\n".join(lines) + "\n")
    print("\n".join(lines))


def check_results(needs, expected):
    for job, required in expected.items():
        result = needs.get(job, {}).get("result")
        if required:
            if result != "success":
                raise ValueError("Required job did not pass: " + job)
        elif result not in {"skipped", "success"}:
            raise ValueError("Unexpected job result: " + job)


def check_docs(root=Path(".")):
    checked = 0
    for path in [*root.glob("*.md"), *(root / "docs").rglob("*.md")]:
        text = re.sub(r"```.*?```", "", path.read_text(encoding="utf-8"), flags=re.S)
        for link in re.findall(r"\[[^\]]+\]\(([^\s)]+)\)", text):
            parsed = urllib.parse.urlsplit(link)
            if parsed.scheme or parsed.netloc or not parsed.path:
                continue
            target = (path.parent / urllib.parse.unquote(parsed.path)).resolve()
            if not target.is_relative_to(root.resolve()) or not target.exists():
                raise ValueError(
                    "Missing or escaping local document link: " + str(path) + ": " + link
                )
        checked += 1
    if not checked:
        raise ValueError("No documentation found")
    print("Local documentation checked:", checked)


def self_test():
    if classify_paths(["README.md", "docs/history.md"])["runtime"]:
        raise ValueError("documentation classification failed")
    for paths in (
        None,
        [".github/workflows/tests.yml"],
        ["_manifest.json"],
        ["webui.json"],
        ["scripts/check.py"],
        ["old.py", "docs/new.md"],
    ):
        if not classify_paths(paths)["runtime"]:
            raise ValueError("code classification failed")
    for result in ("failure", "cancelled", "skipped", None):
        try:
            check_results({"runtime": {"result": result}}, {"runtime": True})
        except ValueError:
            continue
        raise ValueError("failed core job was accepted")
    check_results(
        {"docs": {"result": "success"}, "runtime": {"result": "skipped"}},
        {"docs": True, "runtime": False},
    )
    for plugin in ("neko-life", "neko-tts", "neko-server-status"):
        rows = plugin_matrix(plugin)["include"]
        if len(rows) != 2 or {r["case"] for r in rows} != {"linux-production", "windows-minimum"}:
            raise ValueError("invalid core matrix")
    print("CI policy classification and failure checks passed")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("classify", "docs", "gate", "self-test"))
    parser.add_argument("--plugin", default="neko-life")
    parser.add_argument("--workflow", default="tests.yml", choices=("tests.yml", "ci.yml"))
    args = parser.parse_args()
    if args.command == "self-test":
        self_test()
    elif args.command == "docs":
        check_docs()
    elif args.command == "classify":
        self_test()
        event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text(encoding="utf-8"))
        paths, full = event_changes(event, args.workflow)
        result = classify_paths(paths)
        result.update(full=full, matrix=plugin_matrix(args.plugin, full))
        emit(result)
    else:
        needs = json.loads(os.environ["NEEDS_JSON"])
        runtime = needs.get("changes", {}).get("outputs", {}).get("runtime")
        if runtime not in ("true", "false"):
            raise ValueError("Missing change classification")
        check_results(needs, {"changes": True, "docs": True, "tests": runtime == "true"})
        print("CI required results verified; runtime:", runtime)


if __name__ == "__main__":
    main()
