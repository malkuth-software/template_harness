# {{title}}

@AGENTS.md

## House style

The canonical house style is `HOUSE_STYLE.md` in the notes clone beside this
checkout: `../notes/HOUSE_STYLE.md` (the `main` branch of
`{{notes_repository}}`). Read it before writing code. SushiStyle checks the
mechanical part of it after each C/C++ edit, from the harness's single rule
pack (`../template_harness/claude/rules/house.json`), each rule naming its
HOUSE_STYLE section. Where a rule and HOUSE_STYLE.md disagree, HOUSE_STYLE.md
wins: report the rule.

## This app's notes

Planning, decisions and interviews live on the `{{app}}` branch of
`{{notes_repository}}`, checked out at `../notes-{{app}}/`. Coordination
(tracks, seats, desks) lives in `../projects/` (`{{projects_repository}}`).
