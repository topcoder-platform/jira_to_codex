#!/usr/bin/env python3
"""
Sequential Jira -> Codex fixer (labels + attachments + optional video frames + comments + dry-run + ignore list)

UPDATED FOR LATEST JIRA CLOUD SEARCH API:
- Uses POST /rest/api/3/search/jql (enhanced search)
- Pagination uses nextPageToken + isLast (NOT startAt)

Flow (normal mode):
1) Query Jira REST API via JQL
2) Load an optional ignore list of Jira issue keys to skip
3) For each non-ignored issue, sequentially: create a branch and run `codex exec` with a template prompt
4) Codex fixes and creates exactly one commit (per prompt template)
5) You review each branch and merge/cherry-pick as desired

Dry-run mode:
- Lists issues matched by JQL
- Prints labels + mapped focus folders
- Downloads attachments locally
- Optionally extracts video frames (requires ffmpeg)
- Fetches all comments and prints a truncated preview
- Does NOT run codex
- Does NOT run any git commands (no checkout/branch creation)

Key features:
- Label -> folder mapping (LABEL_PATH_MAP), supports label->path or label->[paths]
- Multiple component labels: all mapped paths are passed in a "focus area" hint to Codex
- Attachments:
  - Download all attachments (with safety caps)
  - Pass image attachments to codex via `--image`
  - List all attachments (including videos) in the prompt
- Video frames:
  - Optional ffmpeg extraction of 1..N frames per video attachment
  - Extracted frames are passed to codex as images
- Comments:
  - Fetch all issue comments (paginated)
  - Flatten ADF to plain text
  - Include (truncated) in prompt

Environment variables:
Required:
  JIRA_BASE        e.g. https://yourcompany.atlassian.net
  JIRA_EMAIL       Jira account email
  JIRA_TOKEN       Jira API token
  One of:
    JQL            Jira Query Language string used for both live and dry-run mode
    LIVE_JQL       Jira Query Language string used for live mode

Optional:
  DRY_RUN_JQL      Jira Query Language string used when DRY_RUN=1
  PROJECT_ROOT     target repo/workspace root where Codex should run
                   default "{script_dir}/.."
  REPO_DIR         legacy alias for PROJECT_ROOT
  BASE_BRANCH      default "main"
  PROMPT_TEMPLATE_PATH default "{script_dir}/codex/jira_fix_prompt.txt"
  QA_FAILED_PROMPT_TEMPLATE_PATH default "{script_dir}/codex/jira_qa_failed_prompt.txt"
  IGNORE_LIST_PATH default "{script_dir}/ignore_list.txt"; one Jira issue key per line to skip
  JIRA_TO_CODEX_ENV_FILE path to a .env file to load before reading variables
  DRY_RUN          "1" or "0" (default "0")
  LIMIT_ISSUES     integer; if set, only process first N issues from JQL (default: no limit)
  STRICT_MULTILINE_FORMATTING "1" or "0" (default "1"); adds extra prompt
                   guardrails so commit bodies and PR descriptions use real
                   newlines instead of escaped sequences like "\n"

Label mapping env vars:
  LABEL_PATH_MAP   JSON string mapping label->path or label->[paths]
  COMPONENT_LABEL_PREFIXES comma-separated prefixes to consider "component" labels
                  default "svc-,service-,app-,ui-,web-,platform-"
  INCLUDE_ALL_LABELS_IF_NO_COMPONENT "1" or "0" (default "1")
  USE_LABEL_AS_TOPLEVEL_DIR "1" or "0" (default "1") treat labels that match a top-level folder as a focus path

Post-check env var:
  POST_CHECK_CMD   shell command to run after Codex finishes (debug/sanity),
                  e.g. "git status --porcelain && git log -1 --oneline"

Attachment handling env vars:
  ATTACHMENTS_DIR          where to store downloaded attachments (relative to PROJECT_ROOT unless absolute)
                          default ".codex_jira_attachments"
  MAX_ATTACHMENTS_PER_ISSUE default "25"
  MAX_ATTACHMENT_BYTES      default "50000000" (50MB) per attachment
  TOTAL_MAX_ATTACHMENT_BYTES default "200000000" (200MB) per issue total
  INCLUDE_ATTACHMENTS_IN_PROMPT "1" or "0" (default "1")
  ATTACH_IMAGES_TO_CODEX    "1" or "0" (default "1")

Video frame extraction env vars:
  EXTRACT_VIDEO_FRAMES      "1" or "0" (default "0") - requires ffmpeg
  FRAMES_PER_VIDEO          default "3"
  FRAME_TIME_MODE           "uniform" or "middle" or "start" (default "uniform")
  MAX_TOTAL_VIDEO_FRAMES    default "12" (per issue)
  VIDEO_FRAME_WIDTH         default "1280"

Comments handling env vars:
  INCLUDE_COMMENTS_IN_PROMPT "1" or "0" (default "1")
  MAX_COMMENTS_PER_ISSUE     default "100"
  MAX_TOTAL_COMMENT_CHARS    default "20000"

Notes:
- Codex will also use repo guidance files like AGENTS.md if present in the repo tree.
"""

import json
import os
import pathlib
import re
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import requests

SCRIPT_DIR = pathlib.Path(__file__).resolve().parent


def resolve_env_path() -> pathlib.Path:
    """
    Return the .env file path that should be loaded for this launcher.

    The default is a repo-local `.env`, but callers can override it with
    JIRA_TO_CODEX_ENV_FILE when they want to keep secrets elsewhere.
    """
    override = os.environ.get("JIRA_TO_CODEX_ENV_FILE")
    if override:
        return pathlib.Path(override).expanduser().resolve()
    return SCRIPT_DIR / ".env"


def load_dotenv_file(env_path: pathlib.Path) -> None:
    """
    Load a simple shell-compatible .env file without external dependencies.

    Existing process environment variables win over file values so users can
    override settings per invocation.
    """
    if not env_path.exists():
        return

    with env_path.open("r", encoding="utf-8") as env_file:
        for line_number, raw_line in enumerate(env_file, start=1):
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue

            if line.startswith("export "):
                line = line[7:].lstrip()

            if "=" not in line:
                raise SystemExit(f"Invalid .env entry at {env_path}:{line_number}: {raw_line.rstrip()}")

            key, raw_value = line.split("=", 1)
            key = key.strip()
            raw_value = raw_value.strip()

            if not key:
                raise SystemExit(f"Invalid .env key at {env_path}:{line_number}")

            if key in os.environ:
                continue

            if not raw_value:
                os.environ[key] = ""
                continue

            try:
                parts = shlex.split(raw_value, posix=True)
            except ValueError as exc:
                raise SystemExit(f"Failed to parse {env_path}:{line_number}: {exc}") from exc

            os.environ[key] = "" if not parts else (" ".join(parts) if len(parts) > 1 else parts[0])


def require_env(name: str, env_path: pathlib.Path) -> str:
    """
    Return a required environment value or terminate with a setup hint.

    Parameters:
      name: Environment variable name to read.
      env_path: The resolved .env path used for error reporting.

    Returns:
      The configured environment value.

    Raises:
      SystemExit: If the variable is missing or empty.
    """
    value = os.environ.get(name)
    if value:
        return value
    raise SystemExit(
        f"Required environment variable {name} is not set. "
        f"Create {env_path} or export {name} before running this tool."
    )


ENV_PATH = resolve_env_path()
load_dotenv_file(ENV_PATH)

# --- Required env ---
JIRA_BASE = require_env("JIRA_BASE", ENV_PATH).rstrip("/")
JIRA_EMAIL = require_env("JIRA_EMAIL", ENV_PATH)
JIRA_TOKEN = require_env("JIRA_TOKEN", ENV_PATH)

# --- Optional env ---
DRY_RUN = os.environ.get("DRY_RUN", "0") == "1"
LIVE_JQL = os.environ.get("LIVE_JQL")
DRY_RUN_JQL = os.environ.get("DRY_RUN_JQL")
JQL = os.environ.get("JQL") or (DRY_RUN_JQL if DRY_RUN and DRY_RUN_JQL else LIVE_JQL)
if not JQL:
    raise SystemExit(
        f"Set JQL directly or configure LIVE_JQL in {ENV_PATH}. "
        "You can also set DRY_RUN_JQL for dry-run mode."
    )

PROJECT_ROOT = pathlib.Path(
    os.environ.get("PROJECT_ROOT", os.environ.get("REPO_DIR", str(SCRIPT_DIR.parent)))
).expanduser().resolve()
REPO_DIR = str(PROJECT_ROOT)
BASE_BRANCH = os.environ.get("BASE_BRANCH", "main")
PROMPT_TEMPLATE_PATH = os.environ.get(
    "PROMPT_TEMPLATE_PATH",
    str(SCRIPT_DIR / "codex" / "jira_fix_prompt.txt"),
)
QA_FAILED_PROMPT_TEMPLATE_PATH = os.environ.get(
    "QA_FAILED_PROMPT_TEMPLATE_PATH",
    str(SCRIPT_DIR / "codex" / "jira_qa_failed_prompt.txt"),
)
IGNORE_LIST_PATH = os.environ.get(
    "IGNORE_LIST_PATH",
    str(SCRIPT_DIR / "ignore_list.txt"),
)
POST_CHECK_CMD = os.environ.get("POST_CHECK_CMD")
LIMIT_ISSUES = int(os.environ["LIMIT_ISSUES"]) if "LIMIT_ISSUES" in os.environ else None
STRICT_MULTILINE_FORMATTING = os.environ.get("STRICT_MULTILINE_FORMATTING", "1") == "1"

# Label -> path mapping (supports string or list-of-strings values)
LABEL_PATH_MAP: Dict[str, Any] = json.loads(os.environ.get("LABEL_PATH_MAP", "{}"))

USE_LABEL_AS_TOPLEVEL_DIR = os.environ.get("USE_LABEL_AS_TOPLEVEL_DIR", "1") == "1"
INCLUDE_ALL_LABELS_IF_NO_COMPONENT = os.environ.get("INCLUDE_ALL_LABELS_IF_NO_COMPONENT", "1") == "1"

_COMPONENT_PREFIXES = os.environ.get(
    "COMPONENT_LABEL_PREFIXES",
    "svc-,service-,app-,ui-,web-,platform-",
)
COMPONENT_LABEL_PREFIXES = [p.strip() for p in _COMPONENT_PREFIXES.split(",") if p.strip()]

# Attachment handling
ATTACHMENTS_DIR = os.environ.get("ATTACHMENTS_DIR", ".codex_jira_attachments")
MAX_ATTACHMENTS_PER_ISSUE = int(os.environ.get("MAX_ATTACHMENTS_PER_ISSUE", "25"))
MAX_ATTACHMENT_BYTES = int(os.environ.get("MAX_ATTACHMENT_BYTES", "50000000"))  # 50MB
TOTAL_MAX_ATTACHMENT_BYTES = int(os.environ.get("TOTAL_MAX_ATTACHMENT_BYTES", "200000000"))  # 200MB
INCLUDE_ATTACHMENTS_IN_PROMPT = os.environ.get("INCLUDE_ATTACHMENTS_IN_PROMPT", "1") == "1"
ATTACH_IMAGES_TO_CODEX = os.environ.get("ATTACH_IMAGES_TO_CODEX", "1") == "1"

# Video frame extraction
EXTRACT_VIDEO_FRAMES = os.environ.get("EXTRACT_VIDEO_FRAMES", "0") == "1"
FRAMES_PER_VIDEO = int(os.environ.get("FRAMES_PER_VIDEO", "3"))
FRAME_TIME_MODE = os.environ.get("FRAME_TIME_MODE", "uniform").strip().lower()
MAX_TOTAL_VIDEO_FRAMES = int(os.environ.get("MAX_TOTAL_VIDEO_FRAMES", "12"))
VIDEO_FRAME_WIDTH = int(os.environ.get("VIDEO_FRAME_WIDTH", "1280"))

# Comments handling
INCLUDE_COMMENTS_IN_PROMPT = os.environ.get("INCLUDE_COMMENTS_IN_PROMPT", "1") == "1"
MAX_COMMENTS_PER_ISSUE = int(os.environ.get("MAX_COMMENTS_PER_ISSUE", "100"))
MAX_TOTAL_COMMENT_CHARS = int(os.environ.get("MAX_TOTAL_COMMENT_CHARS", "20000"))

# Common formats
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff"}
VIDEO_EXTS = {".mp4", ".mov", ".webm", ".mkv", ".avi", ".m4v"}


@dataclass
class DownloadedAttachment:
    filename: str
    mime_type: str
    size_bytes: int
    local_path: str
    is_image: bool
    is_video: bool
    source: str  # e.g. "jira" or "video-frame:<video filename>"


_WORKSPACE_REPO_CACHE: Optional[List[pathlib.Path]] = None
_GH_PR_CHECK_WARNING_EMITTED = False


# ----------------------------
# Jira helpers
# ----------------------------

def _jira_auth() -> tuple[str, str]:
    return (JIRA_EMAIL, JIRA_TOKEN)


def _jira_headers_json() -> Dict[str, str]:
    return {"Accept": "application/json", "Content-Type": "application/json"}


def _jira_headers_accept() -> Dict[str, str]:
    return {"Accept": "application/json"}


def jira_search(jql: str, next_page_token: Optional[str] = None, max_results: int = 50) -> Dict[str, Any]:
    """
    Jira Cloud Issue Search (enhanced search):
      POST /rest/api/3/search/jql

    Pagination:
      - request: nextPageToken (optional), maxResults
      - response: issues[], isLast, nextPageToken (when not last)
    """
    url = f"{JIRA_BASE}/rest/api/3/search/jql"
    payload: Dict[str, Any] = {
        "jql": jql,
        "maxResults": max_results,
        "fields": ["summary", "description", "labels", "attachment", "status"],
    }
    if next_page_token:
        payload["nextPageToken"] = next_page_token

    r = requests.post(url, headers=_jira_headers_json(), auth=_jira_auth(), json=payload, timeout=60)
    if not r.ok:
        raise SystemExit(f"Jira search failed: {r.status_code} {r.reason}\n{r.text}")
    return r.json()


def jira_get_all_comments(issue_key: str, max_results: int = 100) -> List[Dict[str, Any]]:
    """
    Fetch all comments for an issue (paginated).
    GET /rest/api/3/issue/{issueIdOrKey}/comment
    """
    start_at = 0
    comments: List[Dict[str, Any]] = []

    while True:
        url = f"{JIRA_BASE}/rest/api/3/issue/{issue_key}/comment"
        params = {"startAt": start_at, "maxResults": max_results}

        r = requests.get(url, headers=_jira_headers_accept(), auth=_jira_auth(), params=params, timeout=60)
        if not r.ok:
            raise SystemExit(f"Jira comments failed for {issue_key}: {r.status_code} {r.reason}\n{r.text}")
        data = r.json()

        page_comments = data.get("comments", []) or []
        comments.extend(page_comments)

        start_at += int(data.get("maxResults", max_results) or max_results)
        total = int(data.get("total", len(comments)) or len(comments))

        if start_at >= total:
            break

    return comments


def extract_adf_text(adf_or_text: Any) -> str:
    """Best-effort ADF -> plain text flatten (works for description and comment bodies)."""
    if adf_or_text is None:
        return ""
    if isinstance(adf_or_text, str):
        return adf_or_text

    chunks: List[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            t = node.get("type")
            if t == "text" and "text" in node:
                chunks.append(node["text"])
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for it in node:
                walk(it)

    walk(adf_or_text)
    return "".join(chunks).strip()


def build_comments_block(
    issue_key: str,
    comments: List[Dict[str, Any]],
    *,
    prioritize_recent: bool = False,
) -> str:
    """
    Format Jira comments for the Codex prompt.

    Parameters:
      issue_key: Jira issue key used in the heading.
      comments: Raw Jira comment payloads for the issue.
      prioritize_recent: When True, newer comments are emitted first so QA
        follow-up feedback is not truncated behind older discussion.

    Returns:
      A prompt-ready comment block, or an empty string when there is no usable
      comment text within the configured limits.
    """
    if not comments:
        return ""

    ordered_comments = list(reversed(comments)) if prioritize_recent else comments
    header_suffix = ", newest first" if prioritize_recent else ""
    out_lines = [f"Comments (Jira issue {issue_key}{header_suffix}):"]
    total_chars = 0
    included = 0

    for c in ordered_comments[:MAX_COMMENTS_PER_ISSUE]:
        author = ((c.get("author") or {}).get("displayName")) or "Unknown"
        created = c.get("created") or ""
        body = extract_adf_text(c.get("body"))

        if not body.strip():
            continue

        entry = f"\n---\nAuthor: {author}\nCreated: {created}\n\n{body}\n"
        if total_chars + len(entry) > MAX_TOTAL_COMMENT_CHARS:
            out_lines.append("\n---\n(Additional comments omitted due to size limits.)\n")
            break

        out_lines.append(entry)
        total_chars += len(entry)
        included += 1

    if included == 0:
        return ""

    return "\n".join(out_lines).strip() + "\n\n"


# ----------------------------
# Repo / git helpers
# ----------------------------

def run(cmd: List[str], cwd: Optional[str] = None, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, check=check, text=True, capture_output=True)


def load_template(template_path: str = PROMPT_TEMPLATE_PATH) -> str:
    """
    Read a text prompt template from disk.

    Parameters:
      template_path: Absolute or relative path to the template file.

    Returns:
      The template contents as UTF-8 text.

    Raises:
      OSError: If the template cannot be opened.
    """
    with open(template_path, "r", encoding="utf-8") as f:
        return f.read()


def fill_template(tpl: str, key: str, summary: str, description: str, status: str) -> str:
    """
    Replace Jira placeholders in a prompt template.

    Parameters:
      tpl: Raw template text containing Jira placeholder tokens.
      key: Jira issue key.
      summary: Jira summary text.
      description: Jira description text flattened to plain text.
      status: Jira workflow status name.

    Returns:
      Template text with placeholders populated.
    """
    return (
        tpl.replace("{{KEY}}", key)
        .replace("{{SUMMARY}}", summary or "")
        .replace("{{DESCRIPTION}}", description or "")
        .replace("{{STATUS}}", status or "Unknown")
    )


def build_output_format_block() -> str:
    """
    Return prompt guardrails that force real multiline commit/PR text.

    Codex sometimes serializes body text as escaped newline sequences when it
    builds CLI arguments inline. This block pushes it toward heredocs/body-file
    flows and explicit verification of the rendered result.
    """
    return (
        "Critical output formatting requirements:\n"
        "- The git commit message body and GitHub PR description must use real newline characters and paragraph breaks.\n"
        "- Do NOT include the literal escape sequences `\\n` or `\\r\\n` in the final commit message or PR body unless you are explicitly referring to those characters.\n"
        "- Do NOT pass multiline content as a single quoted shell argument that contains escaped newlines.\n"
        "- Prefer a temporary file created with a quoted heredoc, then use `git commit -F <file>` and `gh pr create --body-file <file>`.\n"
        "- Before finishing, verify the commit body with `git log -1 --format=%B`.\n"
        "- After creating the PR, verify the PR body with `gh pr view --json body`.\n\n"
    )


def extract_issue_status_name(fields: Dict[str, Any]) -> str:
    """
    Return the Jira status name from an issue fields payload.

    Parameters:
      fields: Jira issue fields returned by the search API.

    Returns:
      The human-readable status name, or an empty string when unavailable.
    """
    status = fields.get("status")
    if isinstance(status, dict):
        return str(status.get("name") or "").strip()
    if isinstance(status, str):
        return status.strip()
    return ""


def is_qa_failed_status(status_name: str) -> bool:
    """
    Determine whether a Jira status should trigger the QA-failed prompt path.

    Parameters:
      status_name: Human-readable Jira workflow status.

    Returns:
      True when the status is the QA follow-up state, otherwise False.
    """
    return status_name.casefold() == "qa failed"


def build_status_guidance_block(issue_key: str, status_name: str) -> str:
    """
    Build any status-specific prompt guidance for the current Jira issue.

    Parameters:
      issue_key: Jira issue key used to fill prompt placeholders.
      status_name: Human-readable Jira workflow status.

    Returns:
      A status-specific guidance block, or an empty string when the issue does
      not require special handling.
    """
    if not is_qa_failed_status(status_name):
        return ""
    qa_template = load_template(QA_FAILED_PROMPT_TEMPLATE_PATH)
    return fill_template(qa_template, issue_key, "", "", status_name).strip() + "\n\n"


def load_ignore_list(ignore_list_path: str) -> set[str]:
    """
    Load Jira issue keys that should be skipped before any per-issue processing.

    Blank lines and comment lines starting with "#" are ignored. A missing file is
    treated as an empty ignore list.
    """
    path = pathlib.Path(ignore_list_path)
    if not path.exists():
        return set()

    ignored: set[str] = set()
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            ticket_id = line.strip()
            if not ticket_id or ticket_id.startswith("#"):
                continue
            ignored.add(ticket_id.upper())

    return ignored


def ensure_clean_git() -> None:
    st = run(["git", "status", "--porcelain"], cwd=REPO_DIR).stdout.strip()
    if st:
        raise SystemExit(
            "Working tree is not clean. Commit/stash your changes before running this.\n"
            f"git status --porcelain:\n{st}\n"
        )


def checkout_base() -> None:
    run(["git", "fetch", "--all", "--prune"], cwd=REPO_DIR)
    run(["git", "checkout", BASE_BRANCH], cwd=REPO_DIR)
    run(["git", "pull", "--ff-only"], cwd=REPO_DIR)


def new_branch(issue_key: str) -> None:
    branch = f"codex/{issue_key}"
    existing = run(["git", "branch", "--list", branch], cwd=REPO_DIR).stdout.strip()
    if not existing:
        run(["git", "checkout", "-b", branch], cwd=REPO_DIR)
    else:
        run(["git", "checkout", branch], cwd=REPO_DIR)


def is_git_repo_root(path: pathlib.Path) -> bool:
    """
    Check whether a path looks like the root of a git repository.

    Parameters:
      path: Directory path to inspect.

    Returns:
      True when the directory contains a `.git` entry, otherwise False.
    """
    return (path / ".git").exists()


def workspace_repo_roots() -> List[pathlib.Path]:
    """
    Return the git repositories available under the configured project root.

    Parameters:
      None.

    Returns:
      Absolute repository root paths. If PROJECT_ROOT itself is a git repo, it
      is returned as the only entry. Otherwise, immediate child repos are
      returned in name order.
    """
    global _WORKSPACE_REPO_CACHE

    if _WORKSPACE_REPO_CACHE is None:
        if is_git_repo_root(PROJECT_ROOT):
            _WORKSPACE_REPO_CACHE = [PROJECT_ROOT]
        else:
            repos: List[pathlib.Path] = []
            try:
                for child in sorted(PROJECT_ROOT.iterdir(), key=lambda item: item.name):
                    if child.is_dir() and is_git_repo_root(child):
                        repos.append(child)
            except OSError:
                repos = []
            _WORKSPACE_REPO_CACHE = repos

    return list(_WORKSPACE_REPO_CACHE)


def resolve_repo_name_from_hint(hint: str, repo_names: List[str]) -> Optional[str]:
    """
    Match a focus hint or Jira label to a workspace repository name.

    Parameters:
      hint: Focus hint text derived from labels or folder mappings.
      repo_names: Known repository folder names under PROJECT_ROOT.

    Returns:
      The matched repository folder name, or None when the hint does not map to
      any known repo.
    """
    normalized = hint.strip()
    if not normalized:
        return None

    normalized_path = normalized.replace("\\", "/")
    first_segment = normalized_path.split("/", 1)[0]
    if first_segment in repo_names:
        return first_segment

    for repo_name in sorted(repo_names, key=len, reverse=True):
        if normalized == repo_name or normalized.startswith(f"{repo_name} "):
            return repo_name

    return None


def resolve_target_repo_paths(labels: List[str], focus_paths: List[str]) -> List[pathlib.Path]:
    """
    Resolve candidate target repositories for a Jira issue.

    Parameters:
      labels: Jira labels on the current issue.
      focus_paths: Focus hints already derived from LABEL_PATH_MAP.

    Returns:
      Candidate repository roots in priority order with duplicates removed.
      When PROJECT_ROOT points directly at a single git repo, that repo is
      returned as a fallback even if the hints do not resolve cleanly.
    """
    repo_roots = workspace_repo_roots()
    if not repo_roots:
        return []

    repo_by_name = {repo_path.name: repo_path for repo_path in repo_roots}
    repo_names = list(repo_by_name)
    resolved: List[pathlib.Path] = []
    seen: set[str] = set()

    for hint in [*focus_paths, *labels]:
        repo_name = resolve_repo_name_from_hint(hint, repo_names)
        if not repo_name or repo_name in seen:
            continue
        resolved.append(repo_by_name[repo_name])
        seen.add(repo_name)

    if not resolved and len(repo_roots) == 1:
        return list(repo_roots)

    return resolved


def search_open_pull_requests_for_issue(issue_key: str, repo_path: pathlib.Path) -> List[Dict[str, Any]]:
    """
    Search open pull requests in a repository for a Jira issue key.

    Parameters:
      issue_key: Jira issue key to search for.
      repo_path: Absolute repository root to query with the GitHub CLI.

    Returns:
      Open pull request metadata dictionaries containing the JSON fields
      returned by `gh pr list`. An empty list is returned when `gh` is missing,
      the query fails, or there are no matching pull requests.
    """
    global _GH_PR_CHECK_WARNING_EMITTED

    if shutil.which("gh") is None:
        if not _GH_PR_CHECK_WARNING_EMITTED:
            print("WARNING: gh is not installed; skipping open pull request pre-checks.")
            _GH_PR_CHECK_WARNING_EMITTED = True
        return []

    result = run(
        [
            "gh",
            "pr",
            "list",
            "--state",
            "open",
            "--search",
            f'"{issue_key}" in:title',
            "--limit",
            "30",
            "--json",
            "number,title,url",
        ],
        cwd=str(repo_path),
        check=False,
    )
    if result.returncode != 0:
        stderr = (result.stderr or result.stdout or "").strip()
        print(f"WARNING: Failed to query open pull requests in {repo_path.name}: {stderr}")
        return []

    try:
        open_prs = json.loads(result.stdout or "[]")
    except json.JSONDecodeError as exc:
        print(f"WARNING: Failed to parse open pull requests in {repo_path.name}: {exc}")
        return []

    return open_prs if isinstance(open_prs, list) else []


def issue_key_matches_pr_title(issue_key: str, title: str) -> bool:
    """
    Check whether a pull request title references the Jira issue key.

    Parameters:
      issue_key: Jira issue key such as `PM-4665`.
      title: Pull request title to inspect.

    Returns:
      True when the title contains the issue key as a standalone token,
      otherwise False.
    """
    pattern = re.compile(rf"(?<![A-Z0-9]){re.escape(issue_key)}(?![A-Z0-9])", re.IGNORECASE)
    return bool(pattern.search(title or ""))


def find_open_pull_requests_for_issue(issue_key: str, repo_paths: List[pathlib.Path]) -> List[Dict[str, Any]]:
    """
    Find open pull requests whose titles already reference a Jira issue key.

    Parameters:
      issue_key: Jira issue key to match in PR titles.
      repo_paths: Candidate target repositories for the current issue.

    Returns:
      Matching pull request records including repo name, number, title, and URL.
    """
    matches: List[Dict[str, Any]] = []

    for repo_path in repo_paths:
        for pr in search_open_pull_requests_for_issue(issue_key, repo_path):
            title = str(pr.get("title") or "")
            if not issue_key_matches_pr_title(issue_key, title):
                continue

            matches.append(
                {
                    "repo_name": repo_path.name,
                    "number": pr.get("number"),
                    "title": title,
                    "url": pr.get("url") or "",
                }
            )

    return matches


# ----------------------------
# Label -> focus paths
# ----------------------------

def is_component_label(label: str) -> bool:
    return any(label.startswith(prefix) for prefix in COMPONENT_LABEL_PREFIXES)


def normalize_paths(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, list):
        return [v for v in value if isinstance(v, str) and v.strip()]
    return []


def label_to_paths(labels: List[str]) -> List[str]:
    considered = [lab for lab in labels if is_component_label(lab)]
    if not considered and INCLUDE_ALL_LABELS_IF_NO_COMPONENT:
        considered = list(labels)

    repo = pathlib.Path(REPO_DIR)
    paths: List[str] = []

    for lab in considered:
        paths.extend(normalize_paths(LABEL_PATH_MAP.get(lab)))

        if USE_LABEL_AS_TOPLEVEL_DIR:
            p = repo / lab
            if p.is_dir():
                paths.append(lab)

    seen = set()
    out: List[str] = []
    for p in paths:
        if p not in seen:
            out.append(p)
            seen.add(p)
    return out


def build_focus_block(labels: List[str], focus_paths: List[str]) -> str:
    if not labels and not focus_paths:
        return ""

    lines = ["Focus area hint:"]
    if labels:
        lines.append(f"- Jira labels: {', '.join(labels)}")
    if focus_paths:
        lines.append(f"- Likely relevant folders: {', '.join(focus_paths)}")
        lines.append("- Start investigation in those folders, but feel free to change other areas if needed for a correct fix.")
    else:
        lines.append("- No folder mapping found for the labels above. Use repo structure to locate the relevant code.")
    return "\n".join(lines).strip() + "\n\n"


# ----------------------------
# Attachments + video frames
# ----------------------------

def _safe_filename(name: str) -> str:
    base = os.path.basename(name)
    base = base.strip().replace("\x00", "")
    base = re.sub(r"[^\w.\- ()\[\]@+~,=]+", "_", base)
    return base or "attachment"


def _attachments_root() -> pathlib.Path:
    p = pathlib.Path(ATTACHMENTS_DIR)
    if not p.is_absolute():
        p = pathlib.Path(REPO_DIR) / p
    return p


def _is_image(path: pathlib.Path, mime_type: str) -> bool:
    ext = path.suffix.lower()
    return ext in IMAGE_EXTS or mime_type.lower().startswith("image/")


def _is_video(path: pathlib.Path, mime_type: str) -> bool:
    ext = path.suffix.lower()
    return ext in VIDEO_EXTS or mime_type.lower().startswith("video/")


def download_issue_attachments(issue_key: str, attachments: List[Dict[str, Any]]) -> List[DownloadedAttachment]:
    if not attachments:
        return []

    root = _attachments_root() / issue_key / "raw"
    root.mkdir(parents=True, exist_ok=True)

    downloaded: List[DownloadedAttachment] = []
    total_bytes = 0
    count = 0

    for att in attachments:
        if count >= MAX_ATTACHMENTS_PER_ISSUE:
            break

        filename = _safe_filename(att.get("filename", "attachment"))
        mime_type = att.get("mimeType", "") or ""
        size_bytes = int(att.get("size", 0) or 0)
        content_url = att.get("content")
        if not content_url:
            continue

        # Safety limits
        if size_bytes and size_bytes > MAX_ATTACHMENT_BYTES:
            continue
        if total_bytes + (size_bytes or 0) > TOTAL_MAX_ATTACHMENT_BYTES:
            break

        dest = root / filename
        if dest.exists():
            stem, suffix = dest.stem, dest.suffix
            i = 2
            while True:
                cand = root / f"{stem} ({i}){suffix}"
                if not cand.exists():
                    dest = cand
                    break
                i += 1

        with requests.get(content_url, auth=_jira_auth(), stream=True, timeout=120, allow_redirects=True) as r:
            if not r.ok:
                raise SystemExit(f"Attachment download failed for {issue_key}: {r.status_code} {r.reason}\n{r.text}")

            bytes_written = 0
            with open(dest, "wb") as f:
                for chunk in r.iter_content(chunk_size=1024 * 256):
                    if not chunk:
                        continue
                    bytes_written += len(chunk)
                    if bytes_written > MAX_ATTACHMENT_BYTES:
                        try:
                            f.close()
                            dest.unlink(missing_ok=True)
                        except Exception:
                            pass
                        bytes_written = 0
                        break
                    f.write(chunk)

        if bytes_written == 0:
            continue

        total_bytes += bytes_written
        count += 1

        downloaded.append(
            DownloadedAttachment(
                filename=dest.name,
                mime_type=mime_type,
                size_bytes=bytes_written,
                local_path=str(dest),
                is_image=_is_image(dest, mime_type),
                is_video=_is_video(dest, mime_type),
                source="jira",
            )
        )

    return downloaded


def _ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def _ffprobe_available() -> bool:
    return shutil.which("ffprobe") is not None


def _ffprobe_duration_seconds(video_path: str) -> Optional[float]:
    if not _ffprobe_available():
        return None
    try:
        p = subprocess.run(
            [
                "ffprobe",
                "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                video_path,
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        out = (p.stdout or "").strip()
        if p.returncode != 0 or not out:
            return None
        return float(out)
    except Exception:
        return None


def _frame_timestamps(duration: Optional[float], n: int, mode: str) -> List[float]:
    n = max(1, n)

    if duration is None or duration <= 0:
        return [float(i) for i in range(1, n + 1)]

    eps = min(0.25, duration / 100.0)

    if mode == "start":
        return [min(duration - eps, float(i)) for i in range(1, n + 1)]

    if mode == "middle":
        mid = duration / 2.0
        if n == 1:
            return [max(0.0, min(duration - eps, mid))]
        window = max(2.0, duration * 0.10)
        start = max(0.0, mid - window / 2.0)
        end = min(duration - eps, mid + window / 2.0)
        if end <= start:
            return [max(0.0, min(duration - eps, mid))]
        step = (end - start) / (n - 1)
        return [start + i * step for i in range(n)]

    # uniform
    if n == 1:
        return [max(0.0, min(duration - eps, duration * 0.25))]
    start = max(0.0, eps)
    end = max(start, duration - eps)
    step = (end - start) / (n - 1)
    return [start + i * step for i in range(n)]


def extract_frames_from_videos(issue_key: str, downloaded: List[DownloadedAttachment]) -> List[DownloadedAttachment]:
    if not EXTRACT_VIDEO_FRAMES:
        return []
    if not _ffmpeg_available():
        print("WARNING: EXTRACT_VIDEO_FRAMES=1 but ffmpeg not found on PATH. Skipping video frame extraction.")
        return []

    videos = [a for a in downloaded if a.is_video]
    if not videos:
        return []

    frames_root = _attachments_root() / issue_key / "frames"
    frames_root.mkdir(parents=True, exist_ok=True)

    created: List[DownloadedAttachment] = []
    remaining_budget = max(0, MAX_TOTAL_VIDEO_FRAMES)

    for v in videos:
        if remaining_budget <= 0:
            break

        duration = _ffprobe_duration_seconds(v.local_path)
        per_video = min(FRAMES_PER_VIDEO, remaining_budget)
        times = _frame_timestamps(duration, per_video, FRAME_TIME_MODE)

        v_path = pathlib.Path(v.local_path)
        v_stem = _safe_filename(v_path.stem)

        for idx, t in enumerate(times, start=1):
            if remaining_budget <= 0:
                break

            out_name = f"{v_stem}__frame_{idx:02d}.png"
            out_path = frames_root / out_name
            vf = f"scale='min(iw,{VIDEO_FRAME_WIDTH})':-2"

            cmd = [
                "ffmpeg",
                "-hide_banner",
                "-loglevel", "error",
                "-y",
                "-ss", f"{t:.3f}",
                "-i", v.local_path,
                "-frames:v", "1",
                "-vf", vf,
                out_path.as_posix(),
            ]

            p = subprocess.run(cmd, text=True, capture_output=True)
            if p.returncode != 0 or not out_path.exists():
                # fallback
                cmd2 = [
                    "ffmpeg",
                    "-hide_banner",
                    "-loglevel", "error",
                    "-y",
                    "-i", v.local_path,
                    "-ss", f"{t:.3f}",
                    "-frames:v", "1",
                    "-vf", vf,
                    out_path.as_posix(),
                ]
                p2 = subprocess.run(cmd2, text=True, capture_output=True)
                if p2.returncode != 0 or not out_path.exists():
                    continue

            size_bytes = out_path.stat().st_size if out_path.exists() else 0
            created.append(
                DownloadedAttachment(
                    filename=out_path.name,
                    mime_type="image/png",
                    size_bytes=size_bytes,
                    local_path=str(out_path),
                    is_image=True,
                    is_video=False,
                    source=f"video-frame:{pathlib.Path(v.local_path).name}",
                )
            )
            remaining_budget -= 1

    return created


def build_attachments_block(downloaded: List[DownloadedAttachment]) -> str:
    if not downloaded:
        return ""

    lines = ["Attachments (downloaded locally):"]
    for a in downloaded:
        if a.is_image:
            tag = "image"
        elif a.is_video:
            tag = "video"
        else:
            tag = "file"
        src = f" [{a.source}]" if a.source and a.source != "jira" else ""
        lines.append(f"- [{tag}]{src} {a.filename} ({a.mime_type or 'unknown'}, {a.size_bytes} bytes) -> {a.local_path}")

    lines.append(
        "Use these attachments for context (e.g., UI screenshots/videos/logs). "
        "Images (including extracted video frames, if present) may be attached directly to the prompt; "
        "other files are referenced by path."
    )
    return "\n".join(lines).strip() + "\n\n"


# ----------------------------
# Codex invocation
# ----------------------------

_CODEX_HELP_CACHE: Optional[str] = None

def _codex_help_text() -> str:
    global _CODEX_HELP_CACHE
    if _CODEX_HELP_CACHE is None:
        p = subprocess.run(["codex", "exec", "--help"], text=True, capture_output=True)
        _CODEX_HELP_CACHE = (p.stdout or "") + "\n" + (p.stderr or "")
    return _CODEX_HELP_CACHE

def codex_fix(prompt: str, image_paths: Optional[List[str]] = None) -> None:
    cmd = ["codex", "exec", "--dangerously-bypass-approvals-and-sandbox", "--skip-git-repo-check"]

    help_txt = _codex_help_text()

    # Only pass this flag if the installed codex supports it
    if "--ask-for-approval" in help_txt:
        cmd.extend(["--ask-for-approval", "never"])

    # Attach images if supported and requested (flag has been stable as --image in your earlier runs;
    # if your codex doesn't support it, you'll see an error and can add similar detection)
    if image_paths and ATTACH_IMAGES_TO_CODEX:
        for pth in image_paths:
            cmd.extend(["--image", pth])

    cmd.append("-")

    p = subprocess.run(cmd, cwd=REPO_DIR, input=prompt, text=True)
    if p.returncode != 0:
        raise SystemExit(f"Codex returned non-zero exit code: {p.returncode}")

# ----------------------------
# Main
# ----------------------------

def fetch_all_issues() -> List[Dict[str, Any]]:
    """
    Fetch all issues for the given JQL using token-based pagination (enhanced search).
    """
    issues: List[Dict[str, Any]] = []
    token: Optional[str] = None

    while True:
        page = jira_search(JQL, next_page_token=token, max_results=50)
        issues.extend(page.get("issues", []) or [])

        token = page.get("nextPageToken")
        is_last = bool(page.get("isLast", False))

        if is_last or not token:
            break

    return issues


def main() -> None:
    tpl = load_template()
    if not DRY_RUN:
        pass
        #ensure_clean_git()
        #checkout_base()

    ignored_issue_keys = load_ignore_list(IGNORE_LIST_PATH)
    issues = fetch_all_issues()
    if not issues:
        print("No issues found for JQL.")
        return

    if LIMIT_ISSUES is not None:
        issues = issues[: max(0, LIMIT_ISSUES)]

    mode = "DRY RUN" if DRY_RUN else "LIVE"
    print(f"{mode}: Found {len(issues)} issues. Processing sequentially...")
    print(f"Target project root: {REPO_DIR}")
    if ignored_issue_keys:
        print(f"Ignore list loaded: {len(ignored_issue_keys)} ticket(s) from {IGNORE_LIST_PATH}")

    for idx, issue in enumerate(issues, start=1):
        key = issue["key"]
        fields = issue.get("fields", {}) or {}
        summary = fields.get("summary", "") or ""

        if key.upper() in ignored_issue_keys:
            print("\n" + "=" * 80)
            print(f"[{idx}/{len(issues)}] {key} - {summary}")
            print(f"IGNORED {key} DUE TO IGNORE LIST.")
            print("=" * 80)
            continue

        description = extract_adf_text(fields.get("description"))
        status_name = extract_issue_status_name(fields) or "Unknown"
        qa_failed = is_qa_failed_status(status_name)
        labels = fields.get("labels", []) or []
        attachments = fields.get("attachment", []) or []

        focus_paths = label_to_paths(labels)
        target_repo_paths = resolve_target_repo_paths(labels, focus_paths)
        existing_prs = find_open_pull_requests_for_issue(key, target_repo_paths)
        focus_block = build_focus_block(labels, focus_paths)
        status_guidance_block = build_status_guidance_block(key, status_name)

        print("\n" + "=" * 80)
        print(f"[{idx}/{len(issues)}] {key} - {summary}")
        print(f"Status: {status_name}")
        if labels:
            print(f"Labels: {', '.join(labels)}")
        if focus_paths:
            print(f"Focus paths: {', '.join(focus_paths)}")
        if target_repo_paths:
            print(f"Target repos: {', '.join(repo_path.name for repo_path in target_repo_paths)}")
        if qa_failed:
            print("Prompt mode: QA Failed follow-up")
        print(f"Jira attachments: {len(attachments)}")
        if existing_prs:
            print("Open pull requests already exist for this ticket:")
            for pr in existing_prs:
                print(f"- {pr['repo_name']} #{pr['number']}: {pr['title']} -> {pr['url']}")
            print(f"SKIPPING {key} DUE TO EXISTING OPEN PULL REQUEST.")
            print("=" * 80)
            continue
        print("=" * 80)

        # Attachments (download + optional video frame extraction)
        downloaded: List[DownloadedAttachment] = []
        frame_attachments: List[DownloadedAttachment] = []

        if INCLUDE_ATTACHMENTS_IN_PROMPT and attachments:
            try:
                downloaded = download_issue_attachments(key, attachments)
                if downloaded:
                    print(f"Downloaded attachments: {len(downloaded)} -> {_attachments_root() / key}")
            except Exception as e:
                print(f"WARNING: Failed to download attachments for {key}: {e}")

        if downloaded and EXTRACT_VIDEO_FRAMES:
            try:
                frame_attachments = extract_frames_from_videos(key, downloaded)
                if frame_attachments:
                    print(f"Extracted video frames: {len(frame_attachments)}")
            except Exception as e:
                print(f"WARNING: Failed to extract video frames for {key}: {e}")

        all_attachments = downloaded + frame_attachments
        attachments_block = build_attachments_block(all_attachments) if INCLUDE_ATTACHMENTS_IN_PROMPT else ""

        # Comments (fetch + prompt block)
        comments_block = ""
        if INCLUDE_COMMENTS_IN_PROMPT:
            try:
                comments = jira_get_all_comments(key, max_results=100)
                comments_block = build_comments_block(key, comments, prioritize_recent=qa_failed)
                print(f"Fetched comments: {len(comments)}")
            except Exception as e:
                print(f"WARNING: Failed to fetch comments for {key}: {e}")

        # Images to attach to Codex (original + extracted frames)
        image_paths = [a.local_path for a in all_attachments if a.is_image]

        if DRY_RUN:
            print("\nPrompt additions preview (truncated by limits):")
            print((focus_block or "(no focus block)").rstrip())
            print((status_guidance_block or "(no status guidance block)").rstrip())
            if INCLUDE_ATTACHMENTS_IN_PROMPT:
                print((attachments_block or "(no attachments block)").rstrip())
            if INCLUDE_COMMENTS_IN_PROMPT:
                print((comments_block or "(no comments block)").rstrip())
            continue

        # LIVE MODE: git + codex
        #checkout_base()
        #new_branch(key)

        prompt = fill_template(tpl, key, summary, description, status_name)
        format_block = build_output_format_block() if STRICT_MULTILINE_FORMATTING else ""
        prompt = format_block + focus_block + status_guidance_block + attachments_block + comments_block + prompt

        codex_fix(prompt, image_paths=image_paths)

        if POST_CHECK_CMD:
            print("\nPost-check:")
            out = run(["bash", "-lc", POST_CHECK_CMD], cwd=REPO_DIR, check=False)
            if out.stdout:
                print(out.stdout)
            if out.stderr:
                print(out.stderr)

        #run(["git", "checkout", BASE_BRANCH], cwd=REPO_DIR)

    if DRY_RUN:
        print("\nDRY RUN complete. No Codex or git operations were performed.")
    else:
        print("\nDone. Review each codex/<TICKET> branch and merge/cherry-pick as desired.")


if __name__ == "__main__":
    main()
