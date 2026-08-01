# Changelog

> **Источник правды — не этот файл.** Полная история изменений живёт в
> **[GitHub Releases](https://github.com/yennned/NovaPostBot/releases)** (заметки
> генерируются из заголовков squash-PR) и в **[PROGRESS.md](PROGRESS.md)** — журнале
> после каждого коммита. Вести третий список вручную бессмысленно: он отстаёт и
> начинает врать (что и произошло — ниже висели записи июля при десятках более
> поздних PR).

Формат релизов — [SemVer](https://semver.org/lang/ru/). Веха выпускается тегом:

```bash
git tag vX.Y.Z && git push origin vX.Y.Z   # release.yml создаст Release с заметками
```

## Где что искать

| Вопрос | Где смотреть |
|--------|--------------|
| Что вошло в версию `vX.Y.Z` | GitHub Releases, тег `vX.Y.Z` |
| Что изменилось на этой неделе | [PROGRESS.md](PROGRESS.md) (обратная хронология) |
| Что сейчас в проде | лог старта `bot.start version=…` или `/version` (dev) |
| Что осознанно не сделано | [docs/ROADMAP.md](docs/ROADMAP.md) |
