# .sushi: FranSushi project configuration

Rendered by template_harness `new_app`. Each plugin reads `<plugin>.json`
here over the user layer `~/.sushi/<plugin>.json` (the project wins), so
per-machine values (the build host, `clang-query`, `path_map`, a build
wrapper's `build_command`) belong in `~/.sushi/`, never here.

Coordination paths point into the projects clone beside this checkout:
seats in `../projects/desks/seats/`, the SushiComms ticket store in
`../projects/desks/{{app}}/sushicomms.json`. SushiStyle reads the harness's
single house rule pack, `../template_harness/claude/rules/house.json`.
`state/` and `logs/` are per machine and ignored.
