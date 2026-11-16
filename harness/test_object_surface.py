"""Unit tests for check_object_surface.py: the lists, whole runs, the allowance and the exit codes.

Carried over from STXlib's tests/check_stx_minmallocpp_surface_tests.py
(properties S1 to S4) against the worked example
testdata/object_surface/stx_minmallocpp.toml, and extended to the generic
parts: the component file, the shrink-only allowance, the stable output
and exit codes, and nm -P parsing (through a stand-in nm, so no toolchain
is needed). Symbol tables stand in for objects.
"""
from __future__ import annotations

import io
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import textwrap
import unittest
from contextlib import redirect_stdout

import harness_testing
import check_object_surface as surface

EXAMPLE: Path = Path(__file__).resolve().parent / 'testdata' / 'object_surface' / 'stx_minmallocpp.toml'
COMPONENT: surface.Component = surface.load_component(EXAMPLE)

# One object table per run: object name -> (defined, undefined).
WEB_OBJECTS: dict[str, tuple[set[str], set[str]]] = {
    'minmallocpp_os_emscripten.cpp.o': ({'stx_os_reserve', 'stx_os_release'},
                                        {'emscripten_builtin_memalign', 'emscripten_builtin_free',
                                         '__memory_base', '__stack_pointer'}),
    'minmallocpp_os_posix.cpp.o': ({'stx_os_yield', 'stx_os_page'},
                                   {'sched_yield', 'sysconf', 'pthread_key_create',
                                    'pthread_setspecific', 'pthread_getspecific', 'abort'}),
    'region.cpp.o': ({'stx_region_take'}, {'stx_os_reserve', 'stx_os_release', 'memset', 'memcpy'}),
}
POSIX_OBJECTS: dict[str, tuple[set[str], set[str]]] = {
    'os.cpp.o': ({'stx_os_reserve'}, {'_mmap', 'mprotect', 'munmap@GLIBC_2.2.5', 'sysconf'}),
    'heap.cpp.o': ({'stx_heap'}, {'stx_os_reserve', 'malloc', 'getenv'}),
    'page.cpp.o': ({'stx_page'}, {'malloc'}),
}


def table_reader(table: dict[str, tuple[set[str], set[str]]]) -> surface.SymbolReader:
    def read(nm: str, path: Path) -> tuple[set[str], set[str]]:
        return table[path.name]
    return read


def table_finder(table: dict[str, tuple[set[str], set[str]]]):
    def find(directory: Path, prefix: str) -> list[Path]:
        return [directory / name for name in sorted(table) if name.startswith(prefix)]
    return find


def run(table: dict[str, tuple[set[str], set[str]]], *arguments: str,
        config: Path = EXAMPLE) -> tuple[int, str]:
    output: io.StringIO = io.StringIO()
    with redirect_stdout(output):
        status: int = surface.main(['--config', str(config), '--objects', 'objs', *arguments],
                                   table_reader(table), table_finder(table))
    return status, output.getvalue()


class Lists(unittest.TestCase):
    """S1 to S3: the family lists, the wasm globals and the toolchain and sanitizer allowances."""

    def test_s1_family_lists(self) -> None:
        self.assertTrue(surface.is_allowed(COMPONENT, 'posix', 'mmap', False))
        self.assertTrue(surface.is_allowed(COMPONENT, 'windows', '__imp_VirtualAlloc', False))
        self.assertTrue(surface.is_allowed(COMPONENT, 'windows', '_tls_used', False))
        self.assertTrue(surface.is_allowed(COMPONENT, 'web', 'emscripten_builtin_memalign', False))
        for name in ('mmap', 'mprotect', 'munmap'):
            self.assertFalse(surface.is_allowed(COMPONENT, 'web', name, False), name)
        self.assertFalse(surface.is_allowed(COMPONENT, 'posix', 'VirtualAlloc', False))
        self.assertFalse(surface.is_allowed(COMPONENT, 'posix', 'malloc', False))
        self.assertFalse(surface.is_allowed(COMPONENT, 'posix', 'emscripten_builtin_memalign', False))
        self.assertFalse(surface.is_allowed(COMPONENT, 'web', 'emscripten_builtin_malloc', False))
        self.assertFalse(surface.is_allowed(COMPONENT, 'web', 'VirtualAlloc', False))

    def test_s2_wasm_globals_on_web_only(self) -> None:
        for name in ('__memory_base', '__table_base', '__stack_pointer', '__tls_base',
                     '__indirect_function_table'):
            self.assertTrue(surface.is_allowed(COMPONENT, 'web', name, False), name)
            self.assertFalse(surface.is_allowed(COMPONENT, 'posix', name, False), name)
            self.assertFalse(surface.is_allowed(COMPONENT, 'windows', name, False), name)

    def test_s3_helpers_everywhere_sanitizer_only_when_sanitized(self) -> None:
        for family in ('posix', 'windows', 'web'):
            for name in ('memset', '__tls_get_addr', '__emutls_v.slot', '___chkstk_ms', '_GLOBAL_OFFSET_TABLE_'):
                self.assertTrue(surface.is_allowed(COMPONENT, family, name, False), (family, name))
            for name in ('__asan_report_load8', '__ubsan_handle_add_overflow', '__cxa_atexit'):
                self.assertFalse(surface.is_allowed(COMPONENT, family, name, False), (family, name))
                self.assertTrue(surface.is_allowed(COMPONENT, family, name, True), (family, name))
            self.assertFalse(surface.is_allowed(COMPONENT, family, 'getenv', True), family)

    def test_normalised(self) -> None:
        self.assertEqual(surface.normalised('___chkstk_ms'), 'chkstk_ms')
        self.assertEqual(surface.normalised('__imp_VirtualAlloc'), 'VirtualAlloc')
        self.assertEqual(surface.normalised('munmap@GLIBC_2.2.5'), 'munmap')


class WholeRuns(unittest.TestCase):
    """S4 and the stable output: whole runs through main()."""

    def test_s4_web_leaves_pass(self) -> None:
        status, output = run(WEB_OBJECTS, '--family', 'web')
        self.assertEqual(status, surface.EXIT_PASS, output)
        self.assertIn('SUMMARY stx_minmallocpp web: PASS (3 objects, ', output)
        self.assertNotIn('stx_os_reserve', output.split('leaves:')[1].splitlines()[0])

    def test_s4_stray_leaf_fails(self) -> None:
        table: dict[str, tuple[set[str], set[str]]] = dict(WEB_OBJECTS)
        table['heap.cpp.o'] = ({'stx_heap'}, {'malloc'})
        status, output = run(table, '--family', 'web')
        self.assertEqual(status, surface.EXIT_FINDINGS)
        self.assertIn('LEAF objs/heap.cpp.o: malloc', output)
        self.assertIn(': FAIL (', output)

    def test_s4_blind_scan_fails(self) -> None:
        table: dict[str, tuple[set[str], set[str]]] = {
            'region.cpp.o': ({'stx_region_take'}, {'memset'})}
        status, output = run(table, '--family', 'web')
        self.assertEqual(status, surface.EXIT_FINDINGS)
        self.assertIn('BLIND web emscripten_builtin_memalign', output)

    def test_s4_posix_reservation_fails_on_web(self) -> None:
        table: dict[str, tuple[set[str], set[str]]] = dict(WEB_OBJECTS)
        table['os.cpp.o'] = ({'stx_os_map'}, {'mmap', 'mprotect'})
        status, output = run(table, '--family', 'web')
        self.assertEqual(status, surface.EXIT_FINDINGS)
        self.assertIn('LEAF objs/os.cpp.o: mmap', output)
        self.assertIn('LEAF objs/os.cpp.o: mprotect', output)

    def test_sanitized_flag(self) -> None:
        table: dict[str, tuple[set[str], set[str]]] = dict(WEB_OBJECTS)
        table['asan.cpp.o'] = ({'x'}, {'__asan_report_load8'})
        self.assertEqual(run(table, '--family', 'web')[0], surface.EXIT_FINDINGS)
        self.assertEqual(run(table, '--family', 'web', '--sanitized')[0], surface.EXIT_PASS)

    def test_under_filters_objects(self) -> None:
        status, output = run(WEB_OBJECTS, '--family', 'web', '--under', 'minmallocpp_os_')
        self.assertEqual(status, surface.EXIT_PASS, output)
        self.assertIn('(2 objects, ', output)

    def test_json_report(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            path: Path = Path(scratch) / 'report.json'
            status, _ = run(POSIX_OBJECTS, '--family', 'posix', '--json', str(path))
            report: dict = json.loads(path.read_text(encoding='utf-8'))
        self.assertEqual(status, surface.EXIT_FINDINGS)
        self.assertFalse(report['passed'])
        self.assertIn('LEAF objs/heap.cpp.o: getenv', report['findings'])
        self.assertIn('mmap', report['leaves'])


class Allowance(unittest.TestCase):
    """The shrink-only allowance: within passes, over fails, stale fails, writing only lowers."""

    def allowance(self, scratch: str, text: str) -> Path:
        path: Path = Path(scratch) / 'allowance.txt'
        path.write_text(text, encoding='utf-8')
        return path

    def test_within_allowance_passes(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            path: Path = self.allowance(scratch, 'posix malloc 2  # being removed\nposix getenv 1\n')
            status, output = run(POSIX_OBJECTS, '--family', 'posix', '--allowance', str(path))
        self.assertEqual(status, surface.EXIT_PASS, output)

    def test_over_allowance_fails(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            path: Path = self.allowance(scratch, 'posix malloc 1\nposix getenv 1\n')
            status, output = run(POSIX_OBJECTS, '--family', 'posix', '--allowance', str(path))
        self.assertEqual(status, surface.EXIT_FINDINGS)
        self.assertIn('OVER posix malloc 2 1', output)

    def test_stale_line_fails_and_write_only_lowers(self) -> None:
        table: dict[str, tuple[set[str], set[str]]] = dict(POSIX_OBJECTS)
        del table['page.cpp.o']
        with tempfile.TemporaryDirectory() as scratch:
            path: Path = self.allowance(scratch, 'posix malloc 2\nposix getenv 1\nposix write 3\nweb malloc 4\n')
            status, output = run(table, '--family', 'posix', '--allowance', str(path))
            self.assertEqual(status, surface.EXIT_FINDINGS)
            self.assertIn('STALE posix malloc 2 1', output)
            self.assertIn('STALE posix write 3 0', output)
            run(table, '--family', 'posix', '--allowance', str(path), '--write-allowance')
            lines: list[str] = [line for line in path.read_text(encoding='utf-8').splitlines()
                                if not line.startswith('#')]
            self.assertEqual(lines, ['posix getenv 1', 'posix malloc 1', 'web malloc 4'])
            status, output = run(table, '--family', 'posix', '--allowance', str(path))
            self.assertEqual(status, surface.EXIT_PASS, output)

    def test_write_never_adds(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            path: Path = self.allowance(scratch, '')
            status, _ = run(POSIX_OBJECTS, '--family', 'posix', '--allowance', str(path), '--write-allowance')
            self.assertEqual(status, surface.EXIT_FINDINGS)
            self.assertEqual([line for line in path.read_text(encoding='utf-8').splitlines()
                              if not line.startswith('#')], [])

    def test_missing_file_is_empty(self) -> None:
        status, output = run(POSIX_OBJECTS, '--family', 'posix', '--allowance', '/nonexistent/allowance.txt')
        self.assertEqual(status, surface.EXIT_FINDINGS)
        self.assertIn('LEAF objs/page.cpp.o: malloc', output)

    def test_malformed_line_is_an_error(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            path: Path = self.allowance(scratch, 'posix malloc\n')
            status, output = run(POSIX_OBJECTS, '--family', 'posix', '--allowance', str(path))
        self.assertEqual(status, surface.EXIT_ERROR)
        self.assertIn('ERROR ', output)


class Configuration(unittest.TestCase):
    """The component file, usage errors and object discovery."""

    def write(self, scratch: str, text: str) -> Path:
        path: Path = Path(scratch) / 'component.toml'
        path.write_text(textwrap.dedent(text), encoding='utf-8')
        return path

    def test_every_leaf_needs_a_reason(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            path: Path = self.write(scratch, '''
                [component]
                name = "c"
                [families.posix]
                must_see = "mmap"
                allowed = [{ symbol = "mmap" }]
            ''')
            with self.assertRaises(surface.ConfigError):
                surface.load_component(path)
            status, output = run(POSIX_OBJECTS, '--family', 'posix', config=path)
        self.assertEqual(status, surface.EXIT_ERROR)
        self.assertIn('reason', output)

    def test_must_see_required(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            path: Path = self.write(scratch, '''
                [component]
                name = "c"
                [families.posix]
                allowed = [{ symbol = "mmap", reason = "r" }]
            ''')
            with self.assertRaises(surface.ConfigError):
                surface.load_component(path)

    def test_symbol_or_pattern_exactly_one(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            path: Path = self.write(scratch, '''
                [component]
                name = "c"
                [families.posix]
                must_see = "mmap"
                allowed = [{ symbol = "mmap", pattern = "m*", reason = "r" }]
            ''')
            with self.assertRaises(surface.ConfigError):
                surface.load_component(path)

    def test_unknown_family_is_a_usage_error(self) -> None:
        status, output = run(WEB_OBJECTS, '--family', 'plan9')
        self.assertEqual(status, surface.EXIT_ERROR)
        self.assertIn('--family must be one of: posix, web, windows', output)

    def test_no_objects_is_an_error(self) -> None:
        status, output = run({}, '--family', 'web')
        self.assertEqual(status, surface.EXIT_ERROR)
        self.assertIn('no objects found', output)

    def test_targets_resolve_to_cmake_directories(self) -> None:
        directories: list[Path] = surface.object_directories(COMPONENT, Path('build'), None)
        self.assertEqual(directories, [Path('build/CMakeFiles/stxlib_stx_minmallocpp_checked.dir')])
        with self.assertRaises(surface.ConfigError):
            surface.object_directories(COMPONENT, None, None)

    def test_list_names_every_reason(self) -> None:
        output: io.StringIO = io.StringIO()
        with redirect_stdout(output):
            status: int = surface.main(['--config', str(EXAMPLE), '--list'])
        self.assertEqual(status, surface.EXIT_PASS)
        self.assertIn('family web (must see emscripten_builtin_memalign)', output.getvalue())
        self.assertIn('  mmap: reserves and commits address space (os.hpp)', output.getvalue())

    def test_objects_under_finds_object_files(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            root: Path = Path(scratch)
            (root / 'src' / 'state').mkdir(parents=True)
            for name in ('src/state/a.cpp.o', 'src/state/b.obj', 'src/other.cpp.o', 'src/state/a.d'):
                (root / name).write_text('', encoding='utf-8')
            found: list[str] = [path.relative_to(root).as_posix() for path in surface.objects_under(root, 'src/state')]
        self.assertEqual(found, ['src/state/a.cpp.o', 'src/state/b.obj'])


@unittest.skipIf(os.name == 'nt', 'the stand-in nm is a POSIX script')
class NmParsing(unittest.TestCase):
    """symbols_of on nm -P output, through a stand-in nm that prints a fixed listing."""

    def test_portable_format(self) -> None:
        listing: str = textwrap.dedent('''\
            archive.a[os.o]:
            _mmap U
            stx_os_reserve T 0000000000000000 0000000000000010
            weak_hook w
            weak_object v
            counter D 0000000000000000 0000000000000008
        ''')
        with tempfile.TemporaryDirectory() as scratch:
            nm: Path = Path(scratch) / 'nm'
            nm.write_text(f'#!{sys.executable}\nimport sys\nsys.stdout.write({listing!r})\n', encoding='utf-8')
            nm.chmod(nm.stat().st_mode | stat.S_IXUSR)
            defined, undefined = surface.symbols_of(str(nm), Path('os.o'))
            failing: Path = Path(scratch) / 'nm_fails'
            failing.write_text(f'#!{sys.executable}\nimport sys\nsys.exit(3)\n', encoding='utf-8')
            failing.chmod(failing.stat().st_mode | stat.S_IXUSR)
            with self.assertRaises(RuntimeError):
                surface.symbols_of(str(failing), Path('os.o'))
        self.assertEqual(defined, {'stx_os_reserve', 'counter'})
        self.assertEqual(undefined, {'_mmap', 'weak_hook', 'weak_object'})


if __name__ == '__main__':
    unittest.main()
