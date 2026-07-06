---
name: github-commit-guidelines
description: Apply project-safe GitHub commit and PR preparation discipline. Use before staging, committing, amending, creating PRs, responding to code review with commits, or when a user asks to commit/push/open a PR; also use when a repo has many dirty files or generated artifacts and Codex must avoid staging unrelated user changes.
---

# Github Commit Guidelines

Use this skill to keep commits small, reviewable, verified, and free of unrelated user work.

## Preflight

1. Inspect `git status --short` from the repo root.
2. Inspect relevant unstaged and staged diffs before staging or committing.
3. Identify which files belong to the requested change and which are unrelated existing user changes.
4. Do not stage unrelated dirty files, temporary debug scripts, logs, backups, secrets, or generated artifacts unless the user explicitly requests them.
5. If the requested commit would mix unrelated concerns, split it or ask before combining.

## Verification Gate

Before committing or saying work is ready:

- Run the narrowest relevant tests, lint, build, smoke test, or file inspection that proves the change.
- If checks fail, stop and report the failure unless the user explicitly asks to commit known-failing work.
- If checks cannot be run, state exactly what was not verified and why.
- Never claim tests pass from old output or memory.

## Secret and Artifact Safety

Before staging:

- Search staged diff for tokens, API keys, passwords, private keys, cookies, `.env` values, and provider credentials.
- Exclude `.env`, local caches, logs, screenshots with secrets, ad-hoc replay dumps, and temporary one-off scripts unless intentionally part of the change.
- If a secret appears in a diff, do not commit it; warn the user and recommend rotation if it was exposed.

## Commit Message

Write concise imperative subjects.

Prefer the repository's existing style. If no stronger convention exists, use Conventional Commits for code changes:

- `fix: ...` for bug fixes
- `feat: ...` for user-visible features
- `test: ...` for tests only
- `docs: ...` for documentation only
- `chore: ...` for maintenance

Use a body when the reason, risk, migration, rollback, or verification is not obvious.

## Stop Conditions

Do not commit when:

- the staged diff contains unrelated user changes;
- verification fails unexpectedly;
- a secret-like value is present;
- the repo state is ambiguous and committing would risk losing or misattributing work;
- the user requested review only, not a commit.

## Reporting

After a successful commit, report:

- commit hash and subject;
- files or change groups included;
- verification command(s) and result;
- known uncommitted unrelated files, if any.

Emit Codex git directives only after the corresponding git action succeeds.
