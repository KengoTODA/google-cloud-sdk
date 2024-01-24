#!/bin/bash
set -eu

echo "Switch to checkout path"
cd  /tmp/gcloud_history/

echo "Setup push token"
git remote set-url origin https://${GITHUB_USER_AND_PASS}@github.com/${GITHUB_REPO}.git
git config pull.rebase false

echo "Push tags and commits..."
git pull origin master --verbose
git push -u origin master --tags --verbose

git log --graph --oneline --decorate --all
echo "git sync complete"
