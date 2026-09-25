#!/usr/bin/env bash
# Deploy frontend and restart backend (no Docker required)
set -e

REPO=/opt/sheet-music-web

echo "==> Building frontend..."
cd "$REPO/frontend"
npm ci --silent
npm run build

echo "==> Deploying static files..."
rsync -a --delete "$REPO/frontend/dist/" /var/www/sheet-music-web/
chown -R www-data:www-data /var/www/sheet-music-web

echo "==> Restarting backend..."
systemctl restart sheet-music-backend

echo "==> Done. Running services:"
systemctl is-active sheet-music-xvfb sheet-music-backend
