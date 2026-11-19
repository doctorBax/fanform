#!/usr/bin/env bash
set -euo pipefail
cd /opt/fanform
[[ -d .git ]] || { echo 'Not installed from git. Auto update unavailable.' >&2;exit 1; }
branch="$(git symbolic-ref --short HEAD)"
old="$(git rev-parse HEAD)"
git diff --quiet && git diff --cached --quiet || { echo 'Local modifications: refusing automatic update' >&2;exit 1; }
git fetch --depth 1 origin "$branch"
new="$(git rev-parse FETCH_HEAD)"
[[ "$old" != "$new" ]] || exit 0
git merge --ff-only FETCH_HEAD
if ! /usr/bin/python3 -m unittest discover -s tests -q; then
  git reset --hard "$old"
  echo 'Tests failed; rolled back' >&2
  exit 1
fi
systemctl restart fanform.service
