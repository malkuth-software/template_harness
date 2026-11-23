"""Unit tests for ic_declarations.py: the clause language, the declaration, the outputs and the exit codes.

Properties:
  C1  a clause parses into the grammar of docs/ic_declarations.md section 4 and
      is written as parenthesised C++ (intervals, powers, predicates, short
      circuit kept in order); a value where a condition belongs, or a
      condition where a value belongs, is refused, and so is nesting past 32
      and a clause past 256 tokens;
  C2  a declaration with an unknown key, an unknown name in a clause, an
      `outside` that does not cover exactly the states left out, a refusal
      that is not one of the four forms, or a requirement without its message
      is refused with exit 2;
  C3  generate writes the header's contract lines above each declaration
      (above a template line where there is one), the checks header, the
      manifest column, the block stubs and the obligations; a second run
      changes nothing; check passes on what generate wrote;
  C4  check reports STALE (exit 1) for a hand-edited contract line or output,
      and UNUSED for a predicate no listed source calls;
  C5  a member is looked for inside its component's class, so two classes of
      one header may declare the same line; a declaration line that is not
      unique in its scope is an error;
  C6  a predicate's parameters follow the call's parameters in declared
      order, then the terms; constants are values, not parameters;
  C7  a C function table (`typedef struct name {` ... `} name;`) is a
      component's scope as a class is: its members are looked for between
      the two lines, so two tables may hold a slot of one name;
  C8  a parameter may not take the name of a term or a constant (the
      predicate would read the wrong one), and `**` in a declarations glob
      matches nested directories;
  C9  a declared pointer-to-const parameter keeps one const;
  C10 a call's refusal_order says which refusal wins when several apply: the
      contract line, the legal blocks and a refusal-order obligation carry
      it; an order that names an unknown refusal, names one twice or leaves
      one out is refused, and a declaration without it is unchanged;
  C11 the additive format of schema 1 parses: a hand-stated obligation as a
      lemma (given, shows, sources) or as a structural obligation
      (untouched, paths, metadata, missing), each carried into the
      obligations file; a lemma that is also structural, or untouched
      without paths, is refused; an invariant (from, covers) needs given
      and shows, and covers needs from;
  C12 a represents table gives every state's condition (one missing is
      refused, as is a succeeds for an unknown call and an identity.state
      that is not a state); an open transition or typestate refusal names
      the blocks that cover it, and identity.tested is carried as the
      identity's coverage.
"""
from __future__ import annotations

import io
import json
from pathlib import Path
import tempfile
import textwrap
import unittest
from contextlib import redirect_stdout

import ic_declarations as ic

HEADER: str = textwrap.dedent('''\
    #pragma once

    namespace demo {

    // A counter that can be opened and closed.
    template <typename Element>
    class Counter final {
    public:
        // The value at `index`.
        [[nodiscard]] Element at(const usize index) const noexcept;
        // Takes `amount` away.
        void take(const usize amount, const usize scale) noexcept;
        void close() noexcept;
        [[nodiscard]] bool is_open() const noexcept;
    };

    class Other final {
    public:
        void close() noexcept;
    };

    } // namespace demo
    ''')

DECLARATION: str = textwrap.dedent('''\
    schema = 1
    component = "Counter"
    namespace = "demo"
    header = "include/counter.hpp"
    anchor = "class Counter final {"
    thread = "owner"
    states = ["open", "closed"]
    initial = "open"
    state_query = { call = "is_open", observes = { open = "true", closed = "false" } }

    [terms]
    count = "usize"

    [constants]
    limit = { value = "2^40", type = "u64" }

    [checks]
    path = "include/counter_checks.hpp"
    namespace = "demo::checks"
    includes = ["types.hpp"]
    used_in = ["src/counter.cpp"]

    [[call]]
    name = "at"
    declarations = ["[[nodiscard]] Element at(const usize index) const noexcept;"]
    in = ["open"]
    outside = "failure:disposed:closed_handle"
    parameters = { index = "usize" }
    requires = [{ holds = "index < count", refuses = "requirement", message = "Counter: index out of range" }]

    [[call]]
    name = "take"
    declarations = ["void take(const usize amount, const usize scale) noexcept;"]
    parameters = { amount = "usize", scale = "usize" }
    requires = [
      { holds = "scale in [1, limit] and amount <= count", refuses = "failure:invalid_argument", check = "take_amount" },
    ]
    fails = [{ failure = "out_of_memory", when = "never, here", leaves = "partial" }]

    [[call]]
    name = "close"
    declarations = ["void close() noexcept;"]
    in = ["open"]
    outside = "failure:disposed"
    moves = "closed"
    ''')

PROJECT: str = textwrap.dedent('''\
    schema = 1
    declarations = ["include/**/*.ic.toml"]
    manifest_column = "manifest/constraints.json"
    blocks = "blocks"
    obligations = "obligations"
    generator = "ic_declarations.py"
    column_limit = 100
    ''')

SOURCE: str = 'bool a = demo::checks::admits_at(1, 2) && demo::checks::admits_take_amount(1, 2, 3);\n'


class Workspace:
    """A temporary repository with the example header, declaration, project file and source."""

    def __init__(self) -> None:
        self.directory: tempfile.TemporaryDirectory[str] = tempfile.TemporaryDirectory()
        self.root: Path = Path(self.directory.name)
        self.write('include/counter.hpp', HEADER)
        self.write('include/counter.ic.toml', DECLARATION)
        self.write('ic.toml', PROJECT)
        self.write('src/counter.cpp', SOURCE)

    def write(self, relative: str, text: str) -> None:
        path: Path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')

    def read(self, relative: str) -> str:
        text: str = (self.root / relative).read_text(encoding='utf-8')
        return text

    def run(self, command: str) -> tuple[int, str]:
        output: io.StringIO = io.StringIO()
        with redirect_stdout(output):
            status: int = ic.run(command, self.root / 'ic.toml', self.root)
        return status, output.getvalue()

    def close(self) -> None:
        self.directory.cleanup()


def contract_of(header: str) -> str:
    """The generated contract lines joined into one text, so a sentence wrapped over two lines reads whole."""
    pieces: list[str] = [line.split(ic.COMMENT_PREFIX, 1)[1].strip() for line in header.split('\n')
                         if line.lstrip().startswith(ic.COMMENT_PREFIX)]
    text: str = ' '.join(pieces)
    return text


def cpp(text: str) -> str:
    rendered: str = ic.outer_stripped(ic.cpp_of(ic.parse_clause(text)))
    return rendered


class ClauseTests(unittest.TestCase):
    def test_c1_comparisons_and_logic(self) -> None:
        self.assertEqual(cpp('index < count'), 'index < count')
        self.assertEqual(cpp('first <= count and length <= count - first'),
                         '(first <= count) && (length <= (count - first))')
        self.assertEqual(cpp('not (a == b) or c != 0'), '!(a == b) || (c != 0)')

    def test_c1_intervals_powers_and_predicates(self) -> None:
        self.assertEqual(cpp('scale in (0, 8]'), '0 < scale && scale <= 8')
        self.assertEqual(cpp('bounds.width in [1, 32768]'), '1 <= bounds.width && bounds.width <= 32768')
        self.assertEqual(cpp('size <= 2^63'), 'size <= 0x8000000000000000ULL')
        self.assertEqual(cpp('power_of_two(alignment)'), 'alignment != 0 && (alignment & (alignment - 1)) == 0')
        self.assertEqual(cpp('not_null(address)'), 'address != nullptr')
        self.assertEqual(cpp('finite(x)'), '__builtin_isfinite(x)')

    def test_c1_names_in_order(self) -> None:
        found: list[str] = []
        ic.names_of(ic.parse_clause('b.width < a and c in [a, d]'), found)
        self.assertEqual(found, ['b', 'a', 'c', 'd'])

    def test_c1_refusals(self) -> None:
        text: str
        for text in ('index', 'index < count < 3', 'a + (b < c)', 'power_of_two(a < b)', 'a in [1, 2', 'a <', '2^b',
                     'a $ b', 'power_of_two < 3'):
            with self.subTest(text=text):
                with self.assertRaises(ic.DeclarationError):
                    ic.parse_clause(text)

    def test_c1_bounds(self) -> None:
        deep: str = '(' * 40 + 'a < b' + ')' * 40
        with self.assertRaises(ic.DeclarationError):
            ic.parse_clause(deep)
        long_clause: str = ' and '.join(['a < b'] * 100)
        with self.assertRaises(ic.DeclarationError):
            ic.parse_clause(long_clause)
        nested: str = '(' * 30 + 'a < b' + ')' * 30
        self.assertEqual(cpp(nested), 'a < b')


class DeclarationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.workspace: Workspace = Workspace()

    def tearDown(self) -> None:
        self.workspace.close()

    def refused(self, old: str, new: str, words: str) -> None:
        self.workspace.write('include/counter.ic.toml', DECLARATION.replace(old, new, 1))
        status: int
        output: str
        status, output = self.workspace.run('check')
        self.assertEqual(status, 2, output)
        self.assertIn('ERROR include/counter.ic.toml', output)
        self.assertIn(words, output)

    def test_c2_unknown_key(self) -> None:
        self.refused('thread = "owner"', 'thread = "owner"\nthreads = "ui"', 'unknown key threads')

    def test_c2_unknown_name(self) -> None:
        self.refused('index < count', 'index < size', "'size' is not a parameter, term or constant")

    def test_c2_outside_coverage(self) -> None:
        self.refused('outside = "failure:disposed"\nmoves', 'moves', 'outside must say what the call does in closed')

    def test_c2_refusal_form(self) -> None:
        self.refused('refuses = "failure:invalid_argument"', 'refuses = "throws"', 'is not a refusal')

    def test_c2_requirement_message(self) -> None:
        self.refused(', message = "Counter: index out of range"', '', 'a requirement needs its message')

    def test_c8_parameter_shadows_term(self) -> None:
        self.refused('parameters = { index = "usize" }', 'parameters = { count = "usize" }',
                     "parameter 'count' has the name of a term or constant")

    def test_c10_refusal_order_refused(self) -> None:
        call: str = 'parameters = { index = "usize" }'
        self.refused(call, call + '\nrefusal_order = ["outside", "size"]', "refusal_order names 'size'")
        self.refused(call, call + '\nrefusal_order = ["at", "at"]', 'refusal_order names a refusal twice')
        self.refused(call, call + '\nrefusal_order = ["at"]', 'it leaves out outside')

    def test_c11_obligation_forms_refused(self) -> None:
        lemma: str = ('\n[[obligation]]\nid = "both"\nstatement = "s"\ngiven = ["a >= 0"]\nshows = "a >= 0"\n'
                      'untouched = ["address"]\npaths = ["src/counter.cpp: f: false"]\n')
        self.workspace.write('include/counter.ic.toml', DECLARATION + lemma)
        status, output = self.workspace.run('check')
        self.assertEqual(status, 2, output)
        self.assertIn('a lemma (given, shows) or structural', output)
        alone: str = '\n[[obligation]]\nid = "alone"\nstatement = "s"\nuntouched = ["address"]\n'
        self.workspace.write('include/counter.ic.toml', DECLARATION + alone)
        status, output = self.workspace.run('check')
        self.assertEqual(status, 2, output)
        self.assertIn('untouched and paths go together', output)
        bare: str = '\n[[obligation]]\nid = "kept"\nstatement = "s"\nfrom = ["base: lemma"]\n'
        self.workspace.write('include/counter.ic.toml', DECLARATION + bare)
        status, output = self.workspace.run('check')
        self.assertEqual(status, 2, output)
        self.assertIn('an invariant (from) needs given', output)

    def test_c12_represents_refused(self) -> None:
        self.workspace.write('include/counter.ic.toml', DECLARATION + '\n[represents]\nstates = { open = "a_ != 0" }\n')
        status, output = self.workspace.run('check')
        self.assertEqual(status, 2, output)
        self.assertIn('represents.states must give the condition of every state', output)
        table: str = '\n[represents]\nstates = { open = "a_ != 0", closed = "a_ == 0" }\n'
        self.workspace.write('include/counter.ic.toml', DECLARATION + table + 'succeeds = { open = "result" }\n')
        status, output = self.workspace.run('check')
        self.assertEqual(status, 2, output)
        self.assertIn("represents.succeeds names 'open', not a declared call", output)
        self.refused('thread = "owner"', 'thread = "owner"\nidentity = { kind = "k", stale = "requirement", '
                     'state = "gone" }', "identity.state names 'gone', not a declared state")

    def test_c2_states_needed(self) -> None:
        self.refused('states = ["open", "closed"]\ninitial = "open"\n'
                     'state_query = { call = "is_open", observes = { open = "true", closed = "false" } }\n', '',
                     'need the declaration')


class OutputTests(unittest.TestCase):
    def setUp(self) -> None:
        self.workspace: Workspace = Workspace()

    def tearDown(self) -> None:
        self.workspace.close()

    def test_c3_generate_then_check(self) -> None:
        status: int
        output: str
        status, output = self.workspace.run('generate')
        self.assertEqual(status, 0, output)
        header: str = self.workspace.read('include/counter.hpp')
        lines: list[str] = header.split('\n')
        template_index: int = lines.index('template <typename Element>')
        self.assertTrue(lines[template_index - 1].startswith('// ic:'), header)
        contract: str = contract_of(header)
        self.assertIn('// ic: demo::Counter: generated from include/counter.ic.toml', header)
        self.assertIn('States: open (initial), closed. Observed by is_open(): open true, closed false.', contract)
        at_index: int = next(index for index, line in enumerate(lines) if 'Element at(' in line)
        self.assertTrue(lines[at_index - 1].lstrip().startswith('// ic:'))
        self.assertIn('Admitted in open. In closed: failure disposed (closed_handle), unchanged.', contract)
        self.assertIn('Admitted in open; then closed.', contract)
        self.assertIn('Requires index < count, else requirement ("Counter: index out of range").', contract)
        self.assertIn('// The value at `index`.', header)
        checks: str = self.workspace.read('include/counter_checks.hpp')
        self.assertIn('#include "types.hpp"', checks)
        self.assertIn('inline constexpr u64 limit = 0x10000000000ULL;', checks)
        self.assertIn('[[nodiscard]] constexpr bool admits_at(const usize index, const usize count) noexcept {', checks)
        self.assertIn('inline constexpr char at_message[] = "Counter: index out of range";', checks)
        self.assertNotIn('std::', checks)
        column: dict[str, object] = json.loads(self.workspace.read('manifest/constraints.json'))
        self.assertEqual(column['column'], 'constraints')
        rows: list[dict[str, str]] = column['rows']  # type: ignore[assignment]
        self.assertEqual([row['member'] for row in rows], ['at', 'close', 'take'])
        blocks: dict[str, object] = json.loads(self.workspace.read('blocks/counter.blocks.json'))
        names: list[str] = [block['name'] for block in blocks['blocks']]  # type: ignore[index, union-attr]
        self.assertIn('demo.counter.at.open', names)
        self.assertIn('demo.counter.at.misuse.at', names)
        self.assertIn('demo.counter.at.outside.closed', names)
        self.assertIn('demo.counter.take.open', names)
        self.assertIn('demo.counter.take.closed', names)
        obligations: dict[str, object] = json.loads(self.workspace.read('obligations/counter.obligations.json'))
        kinds: list[str] = [item['kind'] for item in obligations['obligations']]  # type: ignore[index, union-attr]
        self.assertEqual(sorted(set(kinds)), ['entry-check', 'refusal-leaves', 'transition', 'typestate-refusal'])
        self.assertEqual(len(obligations['declaration']['sha256']), 64)  # type: ignore[index]
        status, output = self.workspace.run('generate')
        self.assertEqual(status, 0, output)
        self.assertNotIn('WROTE', output)
        status, output = self.workspace.run('check')
        self.assertEqual(status, 0, output)
        self.assertIn('SUMMARY 1 declaration(s)', output)

    def test_c4_stale_and_unused(self) -> None:
        self.workspace.run('generate')
        header: str = self.workspace.read('include/counter.hpp')
        self.workspace.write('include/counter.hpp', header.replace('Admitted in open; then closed.', 'Closes it.'))
        status: int
        output: str
        status, output = self.workspace.run('check')
        self.assertEqual(status, 1, output)
        self.assertIn('STALE include/counter.hpp', output)
        self.workspace.run('generate')
        self.workspace.write('src/counter.cpp', 'bool a = demo::checks::admits_at(1, 2);\n')
        status, output = self.workspace.run('check')
        self.assertEqual(status, 1, output)
        self.assertIn('UNUSED include/counter.ic.toml: admits_take_amount', output)

    def test_c5_member_scope(self) -> None:
        status: int
        output: str
        status, output = self.workspace.run('generate')
        self.assertEqual(status, 0, output)
        header: str = self.workspace.read('include/counter.hpp')
        other: int = header.index('class Other final {')
        self.assertNotIn('// ic:', header[other:])
        self.workspace.write('include/counter.ic.toml', DECLARATION.replace('anchor = "class Counter final {"\n', ''))
        status, output = self.workspace.run('check')
        self.assertEqual(status, 2, output)
        self.assertIn("2 lines of the header start with 'void close() noexcept;'", output)

    def test_c6_parameter_order(self) -> None:
        self.workspace.run('generate')
        checks: str = self.workspace.read('include/counter_checks.hpp')
        self.assertIn('admits_take_amount(const usize amount, const usize scale, const usize count) noexcept', checks)
        self.assertIn('(1 <= scale && scale <= limit) && (amount <= count)', checks)

    def test_c7_c_typedef_scope(self) -> None:
        lines: list[str] = ['typedef struct gf_first_table {', '    gf_status (GF_CALL* create)(void);',
                            '} gf_first_table;', '', 'typedef struct gf_second_table {',
                            '    gf_status (GF_CALL* create)(void);', '} gf_second_table;']
        self.assertEqual(ic.class_end(lines, 0), 3)
        self.assertEqual(ic.class_end(lines, 4), 7)
        self.assertEqual(ic.matching_line(lines, 'gf_status (GF_CALL* create)(', 'probe', 4, ic.class_end(lines, 4)), 5)

    def test_c10_refusal_order_carried(self) -> None:
        self.workspace.run('generate')
        plain_header: str = self.workspace.read('include/counter.hpp')
        self.assertNotIn('When several refusals apply', plain_header)
        call: str = 'parameters = { index = "usize" }'
        self.workspace.write('include/counter.ic.toml',
                             DECLARATION.replace(call, call + '\nrefusal_order = ["at", "outside"]', 1))
        status, output = self.workspace.run('generate')
        self.assertEqual(status, 0, output)
        self.assertIn('When several refusals apply: at, then the state refusal.',
                      contract_of(self.workspace.read('include/counter.hpp')))
        blocks: dict[str, object] = json.loads(self.workspace.read('blocks/counter.blocks.json'))
        orders = [block.get('refusal_order') for block in blocks['blocks'] if block['call'] == 'at'
                  and block['mode'] == 'legal']
        self.assertEqual(orders, [['at', 'outside']])
        obligations: dict[str, object] = json.loads(self.workspace.read('obligations/counter.obligations.json'))
        kinds = {item['id']: item['kind'] for item in obligations['obligations']}
        self.assertEqual(kinds.get('demo.counter.at.order'), 'refusal-order')
        self.assertEqual(self.workspace.run('check')[0], 0)

    def test_c11_obligation_forms_carried(self) -> None:
        stated: str = ('\n[[obligation]]\nid = "lemma"\nstatement = "s"\ngiven = ["a >= 1"]\nshows = "a >= 0"\n'
                       'sources = ["src/counter.cpp: bool a"]\n'
                       '\n[[obligation]]\nid = "structural"\nstatement = "s"\nuntouched = ["address"]\n'
                       'paths = ["src/counter.cpp: release: false"]\nmetadata = ["slots"]\n'
                       'missing = "the slots are not the block"\n')
        self.workspace.write('include/counter.ic.toml', DECLARATION + stated)
        status, output = self.workspace.run('generate')
        self.assertEqual(status, 0, output)
        obligations: dict[str, object] = json.loads(self.workspace.read('obligations/counter.obligations.json'))
        by_id = {item['id']: item for item in obligations['obligations']}
        self.assertEqual(by_id['demo.counter.lemma']['shows'], 'a >= 0')
        self.assertEqual(by_id['demo.counter.structural']['untouched'], ['address'])
        self.assertEqual(by_id['demo.counter.structural']['missing'], 'the slots are not the block')

    def test_c12_coverage_carried(self) -> None:
        declared: str = DECLARATION.replace('thread = "owner"', 'thread = "owner"\nidentity = { kind = "k", stale = '
                                            '"requirement", tested = ["tests/t.cpp: stale"] }', 1)
        self.workspace.write('include/counter.ic.toml', declared + '\n[represents]\nstates = { open = "a_ != 0", '
                             'closed = "a_ == 0" }\nsucceeds = { close = "true" }\n')
        status, output = self.workspace.run('generate')
        self.assertEqual(status, 0, output)
        obligations: dict[str, object] = json.loads(self.workspace.read('obligations/counter.obligations.json'))
        by_id = {item['id']: item for item in obligations['obligations']}
        self.assertEqual(by_id['demo.counter.close.moves']['covered_by']['blocks'], ['demo.counter.close.open'])
        self.assertEqual(by_id['demo.counter.at.outside.closed']['covered_by']['blocks'],
                         ['demo.counter.at.outside.closed'])
        self.assertEqual(by_id['demo.counter.identity']['covered_by'], {'mechanism': 'tests',
                                                                        'tests': ['tests/t.cpp: stale']})

    def test_c9_const_parameter_once(self) -> None:
        # A declared pointer-to-const keeps its one const; any other type gains it.
        self.assertEqual(ic.const_parameter('const gf_spec*'), 'const gf_spec*')
        self.assertEqual(ic.const_parameter('gf_spec*'), 'const gf_spec*')
        self.assertEqual(ic.const_parameter('usize'), 'const usize')


if __name__ == '__main__':
    unittest.main()
