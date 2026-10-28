"""Prints libclang's diagnostics (with locations) for one compile-database unit: argv[1] build dir, argv[2] file suffix."""
import json
import subprocess
import sys

sys.path.insert(0, "D:/gui_forms_w3/dev/template_harness/harness")
import clang_ast
import clang_ast_libclang

entries = json.loads(subprocess.check_output(["ninja", "-C", sys.argv[1], "-t", "compdb"], text=True))
entry = next(e for e in entries if e["file"].replace("\\", "/").endswith(sys.argv[2]))
unit = clang_ast.unit_command(entry)
arguments = clang_ast_libclang.parse_arguments(unit)
print("arguments:", " ".join(arguments)[:1500])

import clang.cindex as cindex
index = cindex.Index.create()
source = clang_ast.absolute(unit.directory, unit.file)
tu = index.parse(source, args=arguments)
for diagnostic in tu.diagnostics:
    if diagnostic.severity >= cindex.Diagnostic.Error:
        print(diagnostic.location.file, diagnostic.location.line, diagnostic.location.column, diagnostic.spelling)
