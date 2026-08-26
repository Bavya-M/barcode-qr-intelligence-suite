# PowerShell Script to stage, commit, and push Flask refactor to GitHub main branch
$ErrorActionPreference = "Stop"

Write-Host "Staging files..." -ForegroundColor Cyan
git add app.py templates/index.html vercel.json requirements.txt README.md

Write-Host "Committing changes..." -ForegroundColor Cyan
git commit -m "Refactor desktop app to web-ready Flask application deployable on Vercel"

Write-Host "Pushing to remote origin/main..." -ForegroundColor Cyan
git push origin main

Write-Host "Deployment push complete!" -ForegroundColor Green
