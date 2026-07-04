# Changelog

Формат — [Keep a Changelog](https://keepachangelog.com/ru/1.1.0/),
версии — [SemVer](https://semver.org/lang/ru/). Записи наполняются из GitHub Releases
(`gh release create vX.Y.Z --generate-notes` собирает их из заголовков squash-PR).

## [Unreleased]

### Added
- CI/CD: автодеплой на push в `main` (сборка образа → GHCR → SSH-деплой на Hetzner),
  версия сборки (git sha) в образе, логах старта и `/version`; релизы по тегам `v*`.
- Командная гигиена GitHub: CODEOWNERS, PR/issue-шаблоны, Dependabot, LICENSE.

<!--
При выпуске версии:
  git tag vX.Y.Z && git push origin vX.Y.Z
release.yml создаст GitHub Release с авто-заметками; перенеси их сюда под ## [X.Y.Z].
-->
