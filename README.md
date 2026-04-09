# Jira To Codex

`jira_to_codex` is a small launcher repo for the Topcoder v6 development workspace. It pulls the Jira issues currently assigned to you, builds a focused Codex prompt with Jira context, and asks Codex to make the fix inside a separate target workspace such as `/home/jmgasper/Documents/Git/v6`.

The repo itself does not contain the application code. It contains the runner, the Codex prompt template, and the local configuration used to point Codex at a multi-repo Topcoder workspace.

## What This Repo Expects

The runner works best when related Topcoder repos are checked out into one shared parent folder. The default configuration in `.env` points at:

```text
/home/jmgasper/Documents/Git/v6
```

Inside that folder, clone the apps and services using the folder names referenced by `LABEL_PATH_MAP`. A typical workspace looks like this:

```text
v6/
├── jira_to_codex/
├── platform-ui/
├── community-app/
├── challenge-api-v6/
├── member-api-v6/
├── review-api-v6/
├── projects-api-v6/
├── marathon-match-api-v6/
├── autopilot-v6/
└── tc-finance-api/
```

If your local folder names differ, update `LABEL_PATH_MAP` in `.env` so Jira labels still map to the correct repo paths.

## Prerequisites

- Python 3 with the `requests` package available.
- `codex` CLI installed and authenticated.
- `git` installed.
- `gh` installed and authenticated if you want Codex to open pull requests.
- Access to the Topcoder Jira tenant and a Jira API token.
- `ffmpeg` and `ffprobe` installed if you want screenshot extraction from video attachments.

## Checking Out A Shared Workspace

1. Create a parent workspace folder.
2. Clone the repos you typically work on into that folder.
3. Clone `jira_to_codex` into the same parent folder or any other convenient location.
4. Make sure each repo keeps the folder name that your label mapping expects.

Example:

```bash
mkdir -p ~/Documents/Git/v6
cd ~/Documents/Git/v6

git clone git@github.com:topcoder-platform/platform-ui.git platform-ui
git clone git@github.com:topcoder-platform/community-app.git community-app
git clone git@github.com:topcoder-platform/challenge-api-v6.git challenge-api-v6
git clone git@github.com:topcoder-platform/member-api-v6.git member-api-v6
git clone git@github.com:topcoder-platform/review-api-v6.git review-api-v6
git clone git@github.com:topcoder-platform/projects-api-v6.git projects-api-v6
git clone git@github.com:topcoder-platform/marathon-match-api-v6.git marathon-match-api-v6
git clone git@github.com:topcoder-platform/autopilot-v6.git autopilot-v6
git clone git@github.com:topcoder-platform/tc-finance-api.git tc-finance-api
git clone git@github.com:topcoder-platform/jira_to_codex.git jira_to_codex
```

Replace each placeholder URL with the SSH or HTTPS clone URL for the repo your team uses.

## Configuration

The launchers load configuration from a repo-local `.env` file.

1. Copy `.env.example` to `.env` if you do not already have a local file.
2. Set `PROJECT_ROOT` to the shared workspace folder that contains the application repos.
3. Set `JIRA_EMAIL` and `JIRA_TOKEN` to your Jira credentials.
4. Adjust `LIVE_JQL` and `DRY_RUN_JQL` if your team wants different ticket filters.
5. Update `LABEL_PATH_MAP` if you add repos or use different local folder names.

`.env` is ignored by Git, so local credentials and workspace paths will not be committed.

## Important Environment Variables

- `PROJECT_ROOT`: Workspace that Codex should modify. This is the main setting that lets you run `jira_to_codex` from its own repo while targeting another folder.
- `LIVE_JQL`: Query used by the live runner.
- `DRY_RUN_JQL`: Query used by `jira_dry_run.sh` or any run where `DRY_RUN=1`.
- `LABEL_PATH_MAP`: JSON map from Jira labels to likely repo folders or app areas.
- `STRICT_MULTILINE_FORMATTING`: Keeps Codex from generating commit or PR bodies that contain literal `\n` text.
- `QA_FAILED_PROMPT_TEMPLATE_PATH`: Optional override for the status-specific prompt used when a Jira issue is in `QA Failed`.
- `POST_CHECK_CMD`: Optional command that runs after Codex finishes.

The Python runner also accepts a legacy `REPO_DIR` environment variable, but `PROJECT_ROOT` is now the preferred name.

## Running The Flow

From the `jira_to_codex` repo root:

```bash
./jira_dry_run.sh
```

Use dry-run first when you want to inspect which issues match your JQL, what attachments were downloaded, and which folders will be suggested to Codex.

When the dry-run output looks correct, run the live flow:

```bash
./jira_to_codex.sh
```

The live run will:

1. Query Jira for issues assigned to you.
2. Resolve likely target repos from `LABEL_PATH_MAP` and skip any issue that already has an open PR in one of those repos whose title contains the Jira key.
3. Download attachments and comments.
4. Build a focused prompt using `codex/jira_fix_prompt.txt`.
5. If the Jira status is `QA Failed`, prepend a follow-up prompt that tells Codex to inspect the previous fix branch, investigate the latest QA comments, and create a fresh branch for the follow-up work.
6. Run `codex exec` in `PROJECT_ROOT`.
7. Let Codex create a branch, commit, push, open a PR, and comment back on the Jira ticket.

## Running Python Directly

You can also launch the runner directly from the `jira_to_codex` repo:

```bash
python3 run_jira_codex.py
```

`run_jira_codex.py` loads `.env` from this repo automatically, so it still targets the configured `PROJECT_ROOT`.

You can override the target workspace for a one-off run:

```bash
PROJECT_ROOT=/home/your-user/Documents/Git/another-workspace python3 run_jira_codex.py
```

You can also point at a different env file:

```bash
JIRA_TO_CODEX_ENV_FILE=/path/to/custom.env python3 run_jira_codex.py
```

## Ignore List

If you create `ignore_list.txt` in this repo, the runner will skip any Jira keys listed there. Add one issue key per line. Lines starting with `#` are ignored.

## Recommended Team Workflow

1. Sync the repos you expect Codex to touch.
2. Run `./jira_dry_run.sh`.
3. Confirm the selected Jira issues, label hints, attachments, and comments make sense.
4. Run `./jira_to_codex.sh`.
5. Review the generated branch and pull request in the target repo workspace.
6. Merge or cherry-pick the changes using your normal team workflow.

## Troubleshooting

- If the launcher says `.env` is missing, create it from `.env.example`.
- If Jira authentication fails, regenerate your Jira API token and update `.env`.
- If Codex changes the wrong repo, check `PROJECT_ROOT` and `LABEL_PATH_MAP`.
- If video screenshots are not extracted, install `ffmpeg` and `ffprobe`, or set `EXTRACT_VIDEO_FRAMES='0'`.
- If a repo is not checked out locally, Codex can still search the workspace, but its focus hints will be less accurate.
