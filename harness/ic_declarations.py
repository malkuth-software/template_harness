#!/usr/bin/env python3
"""Behavioural constraint declarations (IC): parse them, check them, generate from them.

A component states its behavioural constraints once, in a TOML declaration
beside its header: what each call requires and what it does otherwise, the
object's states and the calls each admits, the thread a call runs on, and
who owns what a call returns or takes. This tool reads the declarations and
writes everything else that states those constraints:

- the header's contract lines (`// ic: ...`) above each declared call;
- an entry-check header: one predicate and one message per clause, which
  the code calls, so a stated condition and its check are one text;
- a member-manifest column (`constraints`), one row per call;
- fuzz block stubs (JSON) and, with the project's `engine`, the engine's
  C++ blocks (the IC study's phase IC2);
- proof obligations (JSON), open until a deduction discharges them (a
  deducer's results are merged while their hashes match);
- with the project's `gate`, the gate results file.

The format is docs/ic_declarations.md; the design is the IC study's (notes,
gui_forms branch, planning/stages/IC-integrity-constraints/STUDY.md). It
proves only that the outputs match the declarations and that every
generated predicate is called; whether the code keeps a clause is for tests,
the fuzzer and deductions.

Usage:
  python3 -B harness/ic_declarations.py generate --config P.toml [--root DIR]
  python3 -B harness/ic_declarations.py check    --config P.toml [--root DIR]
  python3 -B harness/ic_declarations.py list     --config P.toml [--root DIR]

Exit status (stable, for a CI gate):
  0  every output is current and every predicate is called;
  1  findings: STALE <path> (an output differs), UNUSED <path>: <predicate>;
  2  a configuration or declaration error: ERROR <path>: <message>.

Standard library only; Python 3.11 or later (tomllib).
"""

from __future__ import annotations

import argparse
import dataclasses
import glob
import hashlib
import json
import re
import sys
import textwrap
import tomllib
from pathlib import Path

SCHEMA: int = 1
COMMENT_PREFIX: str = '// ic:'
THREADS: tuple[str, ...] = ('any', 'owner', 'ui', 'dispatcher')
CALL_KINDS: tuple[str, ...] = ('member', 'static', 'constructor', 'free')
LEAVES: tuple[str, ...] = ('unchanged', 'partial', 'unspecified')
PREDICATES: tuple[str, ...] = ('power_of_two', 'not_null', 'finite')
KEYWORDS: tuple[str, ...] = ('and', 'or', 'not', 'in')
COMPARISONS: tuple[str, ...] = ('<', '<=', '>', '>=', '==', '!=')
MAXIMUM_DEPTH: int = 32
MAXIMUM_TOKENS: int = 256
IDENTIFIER: re.Pattern[str] = re.compile(r'[A-Za-z_][A-Za-z0-9_]*\Z')
TEMPLATE_LINE: re.Pattern[str] = re.compile(r'\s*template\s*<')

PROJECT_KEYS: frozenset[str] = frozenset(
    {'schema', 'declarations', 'manifest_column', 'blocks', 'obligations', 'generator', 'column_limit', 'engine',
     'gate', 'gate_run', 'deductions'})
DECLARATION_KEYS: frozenset[str] = frozenset(
    {'schema', 'component', 'namespace', 'header', 'anchor', 'keys', 'thread', 'states', 'initial',
     'state_query', 'identity', 'terms', 'constants', 'checks', 'call', 'obligation', 'engine'})
KEY_KEYS: frozenset[str] = frozenset({'manifest', 'abi', 'registry', 'surface'})
STATE_QUERY_KEYS: frozenset[str] = frozenset({'call', 'observes'})
IDENTITY_KEYS: frozenset[str] = frozenset({'kind', 'stale'})
CONSTANT_KEYS: frozenset[str] = frozenset({'value', 'cpp', 'type'})
CHECKS_KEYS: frozenset[str] = frozenset({'path', 'namespace', 'includes', 'used_in'})
CALL_KEYS: frozenset[str] = frozenset(
    {'name', 'id', 'declarations', 'kind', 'thread', 'in', 'outside', 'moves', 'parameters', 'requires',
     'fails', 'returns', 'transfers', 'engine', 'refusal_order'})
REQUIRE_KEYS: frozenset[str] = frozenset({'holds', 'refuses', 'message', 'check', 'leaves'})
FAIL_KEYS: frozenset[str] = frozenset({'failure', 'when', 'leaves'})
RETURN_KEYS: frozenset[str] = frozenset({'type', 'owns', 'state', 'borrows', 'until', 'revokes'})
TRANSFER_KEYS: frozenset[str] = frozenset({'what', 'from', 'to'})
OBLIGATION_KEYS: frozenset[str] = frozenset({'id', 'statement', 'method', 'premises', 'given', 'shows', 'sources',
                                            'untouched', 'paths', 'metadata', 'missing'})
# The name refusal_order uses for a call's typestate refusal (its `outside`).
ORDER_OUTSIDE: str = 'outside'


class DeclarationError(Exception):
    """A declaration or project file that does not follow docs/ic_declarations.md."""


# ---- the clause language ---------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class Token:
    kind: str  # 'name', 'number', 'operator', 'keyword', 'end'
    text: str


@dataclasses.dataclass(frozen=True)
class Node:
    """One node of a parsed clause. `kind` says which fields matter:
    name (text), number (value, text), arithmetic (operator, items[2]),
    compare (operator, items[2]), interval (items: subject, low, high; low_open, high_open),
    predicate (text: the predicate, items[1]), logic (operator 'and'/'or', items), negation (items[1])."""
    kind: str
    text: str = ''
    value: int = 0
    operator: str = ''
    items: tuple['Node', ...] = ()
    low_open: bool = False
    high_open: bool = False


BOOLEAN_KINDS: frozenset[str] = frozenset({'compare', 'interval', 'predicate', 'logic', 'negation'})
NAME_PATTERN: re.Pattern[str] = re.compile(r'[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*')
NUMBER_PATTERN: re.Pattern[str] = re.compile(r'0[xX][0-9A-Fa-f]+|[0-9]+')
OPERATOR_PATTERN: re.Pattern[str] = re.compile(r'<=|>=|==|!=|[<>+\-*^()\[\],]')


def tokens_of(text: str) -> list[Token]:
    result: list[Token] = []
    position: int = 0
    while position < len(text):
        character: str = text[position]
        if character.isspace():
            position = position + 1
            continue
        name: re.Match[str] | None = NAME_PATTERN.match(text, position)
        if name is not None:
            word: str = name.group(0)
            kind: str = 'keyword' if word in KEYWORDS else 'name'
            result.append(Token(kind, word))
            position = name.end()
            continue
        number: re.Match[str] | None = NUMBER_PATTERN.match(text, position)
        if number is not None:
            result.append(Token('number', number.group(0)))
            position = number.end()
            continue
        operator: re.Match[str] | None = OPERATOR_PATTERN.match(text, position)
        if operator is not None:
            result.append(Token('operator', operator.group(0)))
            position = operator.end()
            continue
        raise DeclarationError('unexpected character ' + repr(character) + ' in clause ' + repr(text))
    result.append(Token('end', ''))
    if len(result) > MAXIMUM_TOKENS:
        # The parser and the C++ writer recurse over the clause's tree, whose
        # depth is at most its token count: this bound keeps them far inside
        # Python's recursion limit.
        raise DeclarationError('a clause of more than ' + str(MAXIMUM_TOKENS) + ' tokens: ' + repr(text[:60]))
    return result


class ClauseParser:
    """Recursive descent over one clause; nesting is refused beyond MAXIMUM_DEPTH."""

    def __init__(self, text: str) -> None:
        self.text: str = text
        self.tokens: list[Token] = tokens_of(text)
        self.position: int = 0
        self.depth: int = 0

    def fail(self, message: str) -> DeclarationError:
        error: DeclarationError = DeclarationError(message + ' in clause ' + repr(self.text))
        return error

    def peek(self) -> Token:
        token: Token = self.tokens[self.position]
        return token

    def take(self) -> Token:
        token: Token = self.tokens[self.position]
        self.position = self.position + 1
        return token

    def at(self, text: str) -> bool:
        token: Token = self.peek()
        result: bool = token.kind in ('operator', 'keyword') and token.text == text
        return result

    def expect(self, text: str) -> None:
        if not self.at(text):
            raise self.fail('expected ' + repr(text) + ', found ' + repr(self.peek().text or 'the end'))
        self.take()

    def enter(self) -> None:
        self.depth = self.depth + 1
        if self.depth > MAXIMUM_DEPTH:
            raise self.fail('nested more than ' + str(MAXIMUM_DEPTH) + ' deep')

    def leave(self) -> None:
        self.depth = self.depth - 1

    def parse(self) -> Node:
        node: Node = self.condition()
        if self.peek().kind != 'end':
            raise self.fail('unexpected ' + repr(self.peek().text))
        require_boolean(node, self)
        return node

    def condition(self) -> Node:
        items: list[Node] = [self.disjunct()]
        while self.at('or'):
            self.take()
            items.append(self.disjunct())
        if len(items) == 1:
            return items[0]
        item: Node
        for item in items:
            require_boolean(item, self)
        node: Node = Node('logic', operator='or', items=tuple(items))
        return node

    def disjunct(self) -> Node:
        items: list[Node] = [self.negation()]
        while self.at('and'):
            self.take()
            items.append(self.negation())
        if len(items) == 1:
            return items[0]
        item: Node
        for item in items:
            require_boolean(item, self)
        node: Node = Node('logic', operator='and', items=tuple(items))
        return node

    def negation(self) -> Node:
        if self.at('not'):
            self.take()
            self.enter()
            inner: Node = self.negation()
            self.leave()
            require_boolean(inner, self)
            node: Node = Node('negation', items=(inner,))
            return node
        relation: Node = self.relation()
        return relation

    def relation(self) -> Node:
        token: Token = self.peek()
        if token.kind == 'name' and token.text in PREDICATES:
            self.take()
            self.expect('(')
            argument: Node = self.sum()
            self.expect(')')
            require_arithmetic(argument, self)
            node: Node = Node('predicate', text=token.text, items=(argument,))
            return node
        left: Node = self.sum()
        if self.peek().kind == 'operator' and self.peek().text in COMPARISONS:
            operator: str = self.take().text
            right: Node = self.sum()
            require_arithmetic(left, self)
            require_arithmetic(right, self)
            compared: Node = Node('compare', operator=operator, items=(left, right))
            return compared
        if self.at('in'):
            self.take()
            low_open: bool
            if self.at('['):
                low_open = False
            elif self.at('('):
                low_open = True
            else:
                raise self.fail("expected '[' or '(' after 'in'")
            self.take()
            low: Node = self.sum()
            self.expect(',')
            high: Node = self.sum()
            high_open: bool
            if self.at(']'):
                high_open = False
            elif self.at(')'):
                high_open = True
            else:
                raise self.fail("expected ']' or ')' to close the interval")
            self.take()
            require_arithmetic(left, self)
            require_arithmetic(low, self)
            require_arithmetic(high, self)
            interval: Node = Node('interval', items=(left, low, high), low_open=low_open, high_open=high_open)
            return interval
        return left

    def sum(self) -> Node:
        node: Node = self.product()
        while self.at('+') or self.at('-'):
            operator: str = self.take().text
            right: Node = self.product()
            require_arithmetic(node, self)
            require_arithmetic(right, self)
            node = Node('arithmetic', operator=operator, items=(node, right))
        return node

    def product(self) -> Node:
        node: Node = self.factor()
        while self.at('*'):
            self.take()
            right: Node = self.factor()
            require_arithmetic(node, self)
            require_arithmetic(right, self)
            node = Node('arithmetic', operator='*', items=(node, right))
        return node

    def factor(self) -> Node:
        token: Token = self.take()
        if token.kind == 'number':
            value: int = int(token.text, 0)
            if self.at('^'):
                self.take()
                exponent_token: Token = self.take()
                if exponent_token.kind != 'number':
                    raise self.fail("'^' needs a number on both sides")
                exponent: int = int(exponent_token.text, 0)
                if exponent > 64:
                    raise self.fail('a power above 2^64')
                power: Node = Node('number', text=token.text + '^' + exponent_token.text, value=value ** exponent)
                return power
            number: Node = Node('number', text=token.text, value=value)
            return number
        if token.kind == 'name':
            if token.text in PREDICATES:
                raise self.fail(repr(token.text) + ' is a predicate, not a value')
            name: Node = Node('name', text=token.text)
            return name
        if token.kind == 'operator' and token.text == '(':
            self.enter()
            inner: Node = self.condition()
            self.leave()
            self.expect(')')
            return inner
        raise self.fail('unexpected ' + repr(token.text or 'the end'))


def require_boolean(node: Node, parser: ClauseParser) -> None:
    if node.kind not in BOOLEAN_KINDS:
        raise parser.fail('a value where a condition is needed')


def require_arithmetic(node: Node, parser: ClauseParser) -> None:
    if node.kind in BOOLEAN_KINDS:
        raise parser.fail('a condition where a value is needed')


def parse_clause(text: str) -> Node:
    parser: ClauseParser = ClauseParser(text)
    node: Node = parser.parse()
    return node


def names_of(node: Node, found: list[str]) -> None:
    """The clause's names (first part only), in order of first appearance; iterative."""
    pending: list[Node] = [node]
    while pending:
        current: Node = pending.pop()
        if current.kind == 'name':
            root: str = current.text.split('.')[0]
            if root not in found:
                found.append(root)
            continue
        pending.extend(reversed(current.items))


def cpp_literal(value: int, hexadecimal: bool) -> str:
    """A C++ literal: decimal below 2^31 unless asked otherwise, else unsigned long long hexadecimal."""
    if value >= 2 ** 64:
        raise DeclarationError('the number ' + str(value) + ' does not fit 64 bits')
    if value < 2 ** 31 and not hexadecimal:
        return str(value)
    return hex(value) + 'ULL'


def cpp_number(node: Node) -> str:
    literal: str = cpp_literal(node.value, '^' in node.text)
    return literal


def cpp_of(node: Node) -> str:
    """The clause as a C++ expression, every operation parenthesised."""
    if node.kind == 'name':
        return node.text
    if node.kind == 'number':
        return cpp_number(node)
    if node.kind in ('arithmetic', 'compare'):
        left: str = cpp_of(node.items[0])
        right: str = cpp_of(node.items[1])
        return '(' + left + ' ' + node.operator + ' ' + right + ')'
    if node.kind == 'interval':
        subject: str = cpp_of(node.items[0])
        low: str = cpp_of(node.items[1])
        high: str = cpp_of(node.items[2])
        low_operator: str = '<' if node.low_open else '<='
        high_operator: str = '<' if node.high_open else '<='
        return '(' + low + ' ' + low_operator + ' ' + subject + ' && ' + subject + ' ' + high_operator + ' ' + high + ')'
    if node.kind == 'predicate':
        argument: str = cpp_of(node.items[0])
        if node.text == 'power_of_two':
            return '(' + argument + ' != 0 && (' + argument + ' & (' + argument + ' - 1)) == 0)'
        if node.text == 'not_null':
            return '(' + argument + ' != nullptr)'
        return '__builtin_isfinite(' + argument + ')'
    if node.kind == 'logic':
        joiner: str = ' && ' if node.operator == 'and' else ' || '
        parts: list[str] = []
        item: Node
        for item in node.items:
            parts.append(cpp_of(item))
        return '(' + joiner.join(parts) + ')'
    return '!' + cpp_of(node.items[0])


def outer_stripped(expression: str) -> str:
    """An expression without one pair of outer parentheses that encloses all of it."""
    if not (expression.startswith('(') and expression.endswith(')')):
        return expression
    depth: int = 0
    index: int
    for index in range(len(expression)):
        if expression[index] == '(':
            depth = depth + 1
        elif expression[index] == ')':
            depth = depth - 1
            if depth == 0 and index != len(expression) - 1:
                return expression
    return expression[1:-1]


def spelled(text: str) -> str:
    """A clause's text with its whitespace collapsed, as the author wrote it."""
    result: str = ' '.join(text.split())
    return result


# ---- the declaration model -------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class Refusal:
    kind: str  # requirement, failure, null, value
    failure: str = ''
    code: str = ''
    value: str = ''

    def described(self) -> str:
        if self.kind == 'failure':
            if self.code:
                return 'failure ' + self.failure + ' (' + self.code + ')'
            return 'failure ' + self.failure
        if self.kind == 'value':
            return 'returns ' + self.value
        if self.kind == 'null':
            return 'returns null'
        return 'requirement'

    def record(self) -> dict[str, str]:
        result: dict[str, str] = {'kind': self.kind}
        if self.failure:
            result['failure'] = self.failure
        if self.code:
            result['code'] = self.code
        if self.value:
            result['value'] = self.value
        return result


@dataclasses.dataclass(frozen=True)
class Requirement:
    holds: str
    clause: Node
    refuses: Refusal
    message: str
    check: str
    leaves: str
    names: tuple[str, ...]


@dataclasses.dataclass(frozen=True)
class Failure:
    failure: str
    when: str
    leaves: str


@dataclasses.dataclass(frozen=True)
class Call:
    name: str
    identifier: str
    declarations: tuple[str, ...]
    kind: str
    thread: str
    admitting: tuple[str, ...]  # empty: every state
    outside: tuple[tuple[str, Refusal], ...]
    moves: str
    parameters: tuple[tuple[str, str], ...]
    requires: tuple[Requirement, ...]
    fails: tuple[Failure, ...]
    returns: tuple[tuple[str, object], ...]
    transfers: tuple[tuple[str, str, str], ...]
    engine: CallEngine | None = None
    refusal_order: tuple[str, ...] = ()  # empty: undeclared; else every refusal once, the winner first


@dataclasses.dataclass(frozen=True)
class Constant:
    name: str
    type: str
    value: int
    value_text: str
    cpp: str


@dataclasses.dataclass(frozen=True)
class Checks:
    path: str
    namespace: str
    includes: tuple[str, ...]
    used_in: tuple[str, ...]


@dataclasses.dataclass(frozen=True)
class Obligation:
    identifier: str
    statement: str
    method: str
    premises: tuple[str, ...]
    given: tuple[str, ...] = ()
    shows: str = ''
    sources: tuple[str, ...] = ()
    untouched: tuple[str, ...] = ()   # names no refused path forms a pointer from (a structural obligation)
    paths: tuple[str, ...] = ()       # `path: function: refusal` the structural obligation reads
    metadata: tuple[str, ...] = ()    # accessors whose pointers lie outside what `untouched` names
    missing: str = ''                 # the premise a deduction lacks, as its author states it


@dataclasses.dataclass(frozen=True)
class Declaration:
    path: str
    identifier: str
    text: str
    component: str
    namespace: str
    header: str
    anchor: str
    keys: tuple[tuple[str, str], ...]
    thread: str
    states: tuple[str, ...]
    initial: str
    state_query: str
    observes: tuple[tuple[str, str], ...]
    identity_kind: str
    identity_stale: Refusal | None
    terms: tuple[tuple[str, str], ...]
    constants: tuple[Constant, ...]
    checks: Checks | None
    calls: tuple[Call, ...]
    obligations: tuple[Obligation, ...]
    engine: EngineObject | None = None

    def manifest_key(self) -> str:
        key: str = dict(self.keys).get('manifest', '')
        if key:
            return key
        return self.namespace + '::' + self.component


@dataclasses.dataclass(frozen=True)
class Project:
    root: Path
    declarations: tuple[str, ...]
    manifest_column: str
    blocks: str
    obligations: str
    generator: str
    column_limit: int
    engine: EngineProject | None = None
    gate: str = ''
    gate_run: str = ''
    deductions: str = ''


def table_of(value: object, where: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise DeclarationError(where + ' must be a table')
    return value


def text_of(table: dict[str, object], key: str, where: str, default: str | None = None) -> str:
    value: object = table.get(key, default)
    if value is None:
        raise DeclarationError(where + ' needs ' + key)
    if not isinstance(value, str):
        raise DeclarationError(where + ': ' + key + ' must be a string')
    return value


def texts_of(table: dict[str, object], key: str, where: str) -> tuple[str, ...]:
    value: object = table.get(key, [])
    if not isinstance(value, list):
        raise DeclarationError(where + ': ' + key + ' must be a list of strings')
    item: object
    for item in value:
        if not isinstance(item, str):
            raise DeclarationError(where + ': ' + key + ' must be a list of strings')
    result: tuple[str, ...] = tuple(value)
    return result


def known_keys(table: dict[str, object], allowed: frozenset[str], where: str) -> None:
    unknown: list[str] = sorted(set(table) - allowed)
    if unknown:
        raise DeclarationError(where + ': unknown key ' + ', '.join(unknown))


def refusal_of(text: str, where: str) -> Refusal:
    if text == 'requirement':
        return Refusal('requirement')
    if text == 'null':
        return Refusal('null')
    if text.startswith('value:') and len(text) > len('value:'):
        return Refusal('value', value=text[len('value:'):])
    if text.startswith('failure:'):
        parts: list[str] = text.split(':')
        if len(parts) in (2, 3) and all(IDENTIFIER.match(part) for part in parts[1:]):
            code: str = parts[2] if len(parts) == 3 else ''
            return Refusal('failure', failure=parts[1], code=code)
    raise DeclarationError(where + ': ' + repr(text) + ' is not a refusal (requirement, failure:<kind>[:<code>], '
                           'null or value:<value>)')


def leaves_of(table: dict[str, object], where: str) -> str:
    leaves: str = text_of(table, 'leaves', where, 'unchanged')
    if leaves not in LEAVES:
        raise DeclarationError(where + ': leaves must be one of ' + ', '.join(LEAVES))
    return leaves


def identifier_checked(text: str, where: str) -> str:
    if not IDENTIFIER.match(text):
        raise DeclarationError(where + ': ' + repr(text) + ' is not an identifier')
    return text


def constant_of(name: str, value: object, where: str) -> Constant:
    table: dict[str, object] = table_of(value, where)
    known_keys(table, CONSTANT_KEYS, where)
    type_name: str = text_of(table, 'type', where)
    if ('value' in table) == ('cpp' in table):
        raise DeclarationError(where + ' needs exactly one of value and cpp')
    if 'cpp' in table:
        return Constant(name, type_name, 0, '', text_of(table, 'cpp', where))
    value_text: str = text_of(table, 'value', where)
    parser: ClauseParser = ClauseParser(value_text)
    node: Node = parser.sum()
    if parser.peek().kind != 'end':
        raise DeclarationError(where + ': unexpected ' + repr(parser.peek().text) + ' in the value')
    number: int = constant_value(node, where)
    if number < 0 or number >= 2 ** 64:
        raise DeclarationError(where + ': the value is outside [0, 2^64)')
    return Constant(name, type_name, number, spelled(value_text), '')


def constant_value(node: Node, where: str) -> int:
    if node.kind == 'number':
        return node.value
    if node.kind == 'arithmetic':
        left: int = constant_value(node.items[0], where)
        right: int = constant_value(node.items[1], where)
        if node.operator == '+':
            return left + right
        if node.operator == '-':
            return left - right
        return left * right
    raise DeclarationError(where + ': a constant value is numbers, +, - and *')


def call_of(value: object, index: int, declaration: dict[str, object], states: tuple[str, ...],
            known_names: dict[str, str]) -> Call:
    where: str = 'call ' + str(index + 1)
    table: dict[str, object] = table_of(value, where)
    known_keys(table, CALL_KEYS, where)
    name: str = text_of(table, 'name', where)
    where = 'call ' + name
    identifier: str = identifier_checked(text_of(table, 'id', where, name), where)
    declarations: tuple[str, ...] = texts_of(table, 'declarations', where)
    if not declarations:
        raise DeclarationError(where + ' needs declarations')
    kind: str = text_of(table, 'kind', where, 'member')
    if kind not in CALL_KINDS:
        raise DeclarationError(where + ': kind must be one of ' + ', '.join(CALL_KINDS))
    default_thread: str = text_of(declaration, 'thread', 'the declaration', 'any')
    thread: str = text_of(table, 'thread', where, default_thread)
    if thread not in THREADS:
        raise DeclarationError(where + ': thread must be one of ' + ', '.join(THREADS))
    admitting: tuple[str, ...] = texts_of(table, 'in', where)
    moves: str = text_of(table, 'moves', where, '')
    outside: list[tuple[str, Refusal]] = []
    stateful: bool = bool(admitting) or bool(moves) or 'outside' in table
    if stateful and not states:
        raise DeclarationError(where + ': in, outside and moves need the declaration\'s states')
    if stateful and kind == 'free':
        raise DeclarationError(where + ': a free call has no states')
    state: str
    for state in admitting:
        if state not in states:
            raise DeclarationError(where + ': ' + repr(state) + ' is not a declared state')
    if len(set(admitting)) != len(admitting):
        raise DeclarationError(where + ': a state is listed twice in in')
    if moves and moves not in states:
        raise DeclarationError(where + ': moves names ' + repr(moves) + ', not a declared state')
    excluded: list[str] = [state for state in states if admitting and state not in admitting]
    if 'outside' in table:
        given: object = table['outside']
        if isinstance(given, str):
            refusal: Refusal = refusal_of(given, where + ': outside')
            for state in excluded:
                outside.append((state, refusal))
        else:
            outside_table: dict[str, object] = table_of(given, where + ': outside')
            if sorted(outside_table) != sorted(excluded):
                raise DeclarationError(where + ': outside must name exactly the states not in in: '
                                       + ', '.join(excluded))
            for state in excluded:
                outside.append((state, refusal_of(text_of(outside_table, state, where + ': outside'),
                                                  where + ': outside')))
    if excluded and not outside:
        raise DeclarationError(where + ': outside must say what the call does in ' + ', '.join(excluded))
    if not excluded and 'outside' in table:
        raise DeclarationError(where + ': outside needs in to leave a state out')
    parameter_table: dict[str, object] = table_of(table.get('parameters', {}), where + ': parameters')
    parameters: list[tuple[str, str]] = []
    parameter: str
    for parameter in parameter_table:
        identifier_checked(parameter, where + ': parameters')
        if parameter in known_names:
            # A parameter named like a term or a constant would make the clause's text and the predicate's
            # parameters disagree about which one a name means.
            raise DeclarationError(where + ': parameter ' + repr(parameter) + ' has the name of a term or constant')
        parameters.append((parameter, text_of(parameter_table, parameter, where + ': parameters')))
    visible: dict[str, str] = dict(known_names)
    visible.update(dict(parameters))
    requires_value: object = table.get('requires', [])
    if not isinstance(requires_value, list):
        raise DeclarationError(where + ': requires must be a list of tables')
    requirements: list[Requirement] = []
    position: int
    item: object
    for position, item in enumerate(requires_value):
        requirement_where: str = where + ': requires ' + str(position + 1)
        requirement_table: dict[str, object] = table_of(item, requirement_where)
        known_keys(requirement_table, REQUIRE_KEYS, requirement_where)
        holds: str = text_of(requirement_table, 'holds', requirement_where)
        try:
            clause: Node = parse_clause(holds)
        except DeclarationError as error:
            raise DeclarationError(requirement_where + ': ' + str(error)) from None
        found: list[str] = []
        names_of(clause, found)
        found_name: str
        for found_name in found:
            if found_name not in visible:
                raise DeclarationError(requirement_where + ': ' + repr(found_name)
                                       + ' is not a parameter, term or constant')
        refuses: Refusal = refusal_of(text_of(requirement_table, 'refuses', requirement_where), requirement_where)
        message: str = text_of(requirement_table, 'message', requirement_where, '')
        if refuses.kind == 'requirement' and not message:
            raise DeclarationError(requirement_where + ': a requirement needs its message')
        if refuses.kind != 'requirement' and message:
            raise DeclarationError(requirement_where + ': only a requirement carries a message')
        default_check: str = identifier if len(requires_value) == 1 else ''
        check: str = text_of(requirement_table, 'check', requirement_where, default_check)
        if not check:
            raise DeclarationError(requirement_where + ': a call with several clauses names each one\'s check')
        identifier_checked(check, requirement_where + ': check')
        # The predicate's parameters follow the call's own (so its argument
        # order is the C++ signature's), then the object's terms and the
        # constants, each in declared order.
        ordered: list[str] = [name for name, _ in parameters if name in found]
        ordered.extend(name for name in known_names if name in found and name not in ordered)
        requirements.append(Requirement(spelled(holds), clause, refuses, message, check,
                                        leaves_of(requirement_table, requirement_where), tuple(ordered)))
    fails_value: object = table.get('fails', [])
    if not isinstance(fails_value, list):
        raise DeclarationError(where + ': fails must be a list of tables')
    fails: list[Failure] = []
    for position, item in enumerate(fails_value):
        fail_where: str = where + ': fails ' + str(position + 1)
        fail_table: dict[str, object] = table_of(item, fail_where)
        known_keys(fail_table, FAIL_KEYS, fail_where)
        failure: str = identifier_checked(text_of(fail_table, 'failure', fail_where), fail_where)
        fails.append(Failure(failure, text_of(fail_table, 'when', fail_where, ''), leaves_of(fail_table, fail_where)))
    returns_table: dict[str, object] = table_of(table.get('returns', {}), where + ': returns')
    known_keys(returns_table, RETURN_KEYS, where + ': returns')
    if 'state' in returns_table:
        # One state, or several: the result is in one of them, as the arguments decide.
        returned: object = returns_table['state']
        returned_states: list[object] = returned if isinstance(returned, list) else [returned]
        if not returned_states or any(item not in states for item in returned_states):
            raise DeclarationError(where + ': returns.state must name declared states')
    if 'until' in returns_table:
        texts_of(returns_table, 'until', where + ': returns')
    transfers_value: object = table.get('transfers', [])
    if not isinstance(transfers_value, list):
        raise DeclarationError(where + ': transfers must be a list of tables')
    transfers: list[tuple[str, str, str]] = []
    for position, item in enumerate(transfers_value):
        transfer_where: str = where + ': transfers ' + str(position + 1)
        transfer_table: dict[str, object] = table_of(item, transfer_where)
        known_keys(transfer_table, TRANSFER_KEYS, transfer_where)
        transfers.append((text_of(transfer_table, 'what', transfer_where),
                          text_of(transfer_table, 'from', transfer_where),
                          text_of(transfer_table, 'to', transfer_where)))
    engine: CallEngine | None = None
    if 'engine' in table:
        term_table: dict[str, object] = table_of(declaration.get('terms', {}), 'terms')
        terms: dict[str, str] = {key: str(term_table[key]) for key in term_table}
        component_name: str = text_of(declaration, 'component', 'the declaration')
        returns_object: bool = kind == 'static' and declaration_returns_object(declared_return(declarations[0]),
                                                                               component_name)
        engine = call_engine_of(table['engine'], where, terms, visible, states,
                                'live' if kind == 'member' else ('any' if returns_object else 'none'),
                                returns_object)
    order: tuple[str, ...] = refusal_order_of(table, where, requirements, bool(outside))
    call: Call = Call(name, identifier, declarations, kind, thread, admitting, tuple(outside), moves,
                      tuple(parameters), tuple(requirements), tuple(fails), tuple(returns_table.items()),
                      tuple(transfers), engine, order)
    return call


def refusal_order_of(table: dict[str, object], where: str, requirements: list[Requirement],
                     has_outside: bool) -> tuple[str, ...]:
    """A call's refusal_order: which refusal wins when several apply, the first listed winning. It names each
    requirement's check, and `outside` for the typestate refusal, every one exactly once."""
    if 'refusal_order' not in table:
        return ()
    order: tuple[str, ...] = texts_of(table, 'refusal_order', where)
    refusals: list[str] = [requirement.check for requirement in requirements]
    if has_outside:
        refusals.insert(0, ORDER_OUTSIDE)
    if len(refusals) < 2:
        raise DeclarationError(where + ': refusal_order needs at least two refusals to order')
    if len(set(order)) != len(order):
        raise DeclarationError(where + ': refusal_order names a refusal twice')
    unknown: list[str] = [item for item in order if item not in refusals]
    if unknown:
        raise DeclarationError(where + ': refusal_order names ' + ', '.join(repr(item) for item in unknown)
                               + ', not a check of this call' + (' nor outside' if has_outside else ''))
    left_out: list[str] = [item for item in refusals if item not in order]
    if left_out:
        raise DeclarationError(where + ': refusal_order must name every refusal; it leaves out ' + ', '.join(left_out))
    return order


def order_text(call: Call) -> str:
    """The refusal order in words: `the closed state's refusal, then mode, then size`."""
    return ', then '.join('the state refusal' if item == ORDER_OUTSIDE else item for item in call.refusal_order)


def load_declaration(root: Path, relative: str) -> Declaration:
    path: Path = root / relative
    text: str = path.read_text(encoding='utf-8')
    try:
        raw: dict[str, object] = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise DeclarationError(str(error)) from None
    known_keys(raw, DECLARATION_KEYS, 'the declaration')
    if raw.get('schema') != SCHEMA:
        raise DeclarationError('schema must be ' + str(SCHEMA))
    name: str = Path(relative).name
    if not name.endswith('.ic.toml'):
        raise DeclarationError('a declaration is named <id>.ic.toml')
    identifier: str = identifier_checked(name[:-len('.ic.toml')], 'the file name')
    component: str = text_of(raw, 'component', 'the declaration')
    namespace: str = text_of(raw, 'namespace', 'the declaration')
    header: str = text_of(raw, 'header', 'the declaration')
    anchor: str = text_of(raw, 'anchor', 'the declaration', '')
    key_table: dict[str, object] = table_of(raw.get('keys', {}), 'keys')
    known_keys(key_table, KEY_KEYS, 'keys')
    keys: list[tuple[str, str]] = []
    key: str
    for key in sorted(key_table):
        keys.append((key, text_of(key_table, key, 'keys')))
    thread: str = text_of(raw, 'thread', 'the declaration', 'any')
    if thread not in THREADS:
        raise DeclarationError('thread must be one of ' + ', '.join(THREADS))
    states: tuple[str, ...] = texts_of(raw, 'states', 'the declaration')
    state: str
    for state in states:
        identifier_checked(state, 'states')
    if len(set(states)) != len(states):
        raise DeclarationError('a state is declared twice')
    initial: str = text_of(raw, 'initial', 'the declaration', '')
    if states and initial not in states:
        raise DeclarationError('initial must be one of the states')
    if not states and initial:
        raise DeclarationError('initial needs states')
    state_query: str = ''
    observes: list[tuple[str, str]] = []
    if 'state_query' in raw:
        if not states:
            raise DeclarationError('state_query needs states')
        query_table: dict[str, object] = table_of(raw['state_query'], 'state_query')
        known_keys(query_table, STATE_QUERY_KEYS, 'state_query')
        state_query = text_of(query_table, 'call', 'state_query')
        observed: dict[str, object] = table_of(query_table.get('observes', {}), 'state_query.observes')
        if sorted(observed) != sorted(states):
            raise DeclarationError('state_query.observes must give the query\'s answer in every state')
        for state in states:
            observes.append((state, text_of(observed, state, 'state_query.observes')))
    identity_kind: str = ''
    identity_stale: Refusal | None = None
    if 'identity' in raw:
        identity_table: dict[str, object] = table_of(raw['identity'], 'identity')
        known_keys(identity_table, IDENTITY_KEYS, 'identity')
        identity_kind = text_of(identity_table, 'kind', 'identity')
        identity_stale = refusal_of(text_of(identity_table, 'stale', 'identity'), 'identity.stale')
    term_table: dict[str, object] = table_of(raw.get('terms', {}), 'terms')
    terms: list[tuple[str, str]] = []
    for key in term_table:
        identifier_checked(key, 'terms')
        terms.append((key, text_of(term_table, key, 'terms')))
    constant_table: dict[str, object] = table_of(raw.get('constants', {}), 'constants')
    constants: list[Constant] = []
    for key in constant_table:
        identifier_checked(key, 'constants')
        constants.append(constant_of(key, constant_table[key], 'constants.' + key))
    known_names: dict[str, str] = dict(terms)
    constant: Constant
    for constant in constants:
        if constant.name in known_names:
            raise DeclarationError(repr(constant.name) + ' is both a term and a constant')
        known_names[constant.name] = constant.type
    checks: Checks | None = None
    if 'checks' in raw:
        checks_table: dict[str, object] = table_of(raw['checks'], 'checks')
        known_keys(checks_table, CHECKS_KEYS, 'checks')
        used_in: tuple[str, ...] = texts_of(checks_table, 'used_in', 'checks') or (header,)
        checks = Checks(text_of(checks_table, 'path', 'checks'), text_of(checks_table, 'namespace', 'checks'),
                        texts_of(checks_table, 'includes', 'checks'), used_in)
    calls_value: object = raw.get('call', [])
    if not isinstance(calls_value, list):
        raise DeclarationError('call must be an array of tables')
    calls: list[Call] = []
    index: int
    value: object
    for index, value in enumerate(calls_value):
        calls.append(call_of(value, index, raw, states, known_names))
    identifiers: list[str] = [call.identifier for call in calls]
    if len(set(identifiers)) != len(identifiers):
        raise DeclarationError('two calls have one id; give each its own id')
    check_names: list[str] = [requirement.check for call in calls for requirement in call.requires]
    repeated: list[str] = sorted({name for name in check_names if check_names.count(name) > 1})
    differing: list[str] = [name for name in repeated if not same_clause(calls, name)]
    if differing:
        raise DeclarationError('the check ' + ', '.join(differing) + ' names two different clauses')
    if check_names and checks is None:
        raise DeclarationError('clauses need the [checks] table: where their predicates are generated')
    obligations_value: object = raw.get('obligation', [])
    if not isinstance(obligations_value, list):
        raise DeclarationError('obligation must be an array of tables')
    obligations: list[Obligation] = []
    for index, value in enumerate(obligations_value):
        obligation_where: str = 'obligation ' + str(index + 1)
        obligation_table: dict[str, object] = table_of(value, obligation_where)
        known_keys(obligation_table, OBLIGATION_KEYS, obligation_where)
        obligations.append(Obligation(identifier_checked(text_of(obligation_table, 'id', obligation_where),
                                                         obligation_where),
                                      text_of(obligation_table, 'statement', obligation_where),
                                      text_of(obligation_table, 'method', obligation_where, ''),
                                      texts_of(obligation_table, 'premises', obligation_where),
                                      texts_of(obligation_table, 'given', obligation_where),
                                      text_of(obligation_table, 'shows', obligation_where, ''),
                                      texts_of(obligation_table, 'sources', obligation_where),
                                      texts_of(obligation_table, 'untouched', obligation_where),
                                      texts_of(obligation_table, 'paths', obligation_where),
                                      texts_of(obligation_table, 'metadata', obligation_where),
                                      text_of(obligation_table, 'missing', obligation_where, '')))
        last: Obligation = obligations[-1]
        if last.shows and last.untouched:
            raise DeclarationError(obligation_where + ': an obligation is a lemma (given, shows) or structural '
                                   '(untouched, paths), not both')
        if bool(last.untouched) != bool(last.paths):
            raise DeclarationError(obligation_where + ': untouched and paths go together')
        if last.metadata and not last.untouched:
            raise DeclarationError(obligation_where + ': metadata belongs to a structural obligation (untouched)')
    declaration: Declaration = Declaration(relative, identifier, text, component, namespace, header, anchor,
                                           tuple(keys), thread, states, initial, state_query, tuple(observes),
                                           identity_kind, identity_stale, tuple(terms), tuple(constants), checks,
                                           tuple(calls), tuple(obligations),
                                           engine_object_of(raw, dict(terms), states, known_names))
    return declaration


def same_clause(calls: list[Call], check: str) -> bool:
    """Whether every clause named `check` states the same condition and refusal (overloads may share one)."""
    found: list[Requirement] = [requirement for call in calls for requirement in call.requires
                                if requirement.check == check]
    first: Requirement = found[0]
    other: Requirement
    for other in found[1:]:
        if (other.holds, other.refuses, other.message, other.names) != (first.holds, first.refuses, first.message,
                                                                         first.names):
            return False
    return True


def load_project(path: Path, root: Path) -> Project:
    try:
        raw: dict[str, object] = tomllib.loads(path.read_text(encoding='utf-8'))
    except tomllib.TOMLDecodeError as error:
        raise DeclarationError(str(error)) from None
    known_keys(raw, PROJECT_KEYS, 'the project file')
    if raw.get('schema') != SCHEMA:
        raise DeclarationError('the project file\'s schema must be ' + str(SCHEMA))
    limit: object = raw.get('column_limit', 120)
    if not isinstance(limit, int) or limit < 60:
        raise DeclarationError('column_limit must be a whole number of at least 60')
    project: Project = Project(root, texts_of(raw, 'declarations', 'the project file'),
                               text_of(raw, 'manifest_column', 'the project file'),
                               text_of(raw, 'blocks', 'the project file'),
                               text_of(raw, 'obligations', 'the project file'),
                               text_of(raw, 'generator', 'the project file', 'harness/ic_declarations.py'), limit,
                               engine_project_of(raw), text_of(raw, 'gate', 'the project file', ''),
                               text_of(raw, 'gate_run', 'the project file', ''),
                               text_of(raw, 'deductions', 'the project file', ''))
    return project


def declaration_paths(project: Project) -> list[str]:
    found: set[str] = set()
    pattern: str
    for pattern in project.declarations:
        match: str
        # `**` matches any number of directories, as the docs' project file uses it.
        for match in glob.glob(pattern, root_dir=project.root, recursive=True):
            found.add(Path(match).as_posix())
    result: list[str] = sorted(found)
    return result


# ---- the sentences every output shares -------------------------------------------------------

def leaves_text(leaves: str) -> str:
    if leaves == 'unchanged':
        return 'unchanged'
    if leaves == 'partial':
        return 'partly changed'
    return 'in an unspecified state'


def refusal_sentence(refusal: Refusal, message: str, leaves: str) -> str:
    if refusal.kind == 'requirement':
        return 'requirement ("' + message + '")'
    return refusal.described() + ', ' + leaves_text(leaves)


def call_sentences(declaration: Declaration, call: Call) -> list[str]:
    """The call's contract as sentences, in the order docs/ic_declarations.md section 5 gives."""
    sentences: list[str] = []
    if call.admitting:
        moving: str = '; then ' + call.moves if call.moves else ''
        sentences.append('Admitted in ' + ', '.join(call.admitting) + moving + '.')
        groups: dict[Refusal, list[str]] = {}
        state: str
        refusal: Refusal
        for state, refusal in call.outside:
            groups.setdefault(refusal, []).append(state)
        states: list[str]
        for refusal, states in groups.items():
            sentences.append('In ' + ', '.join(states) + ': ' + refusal.described() + ', unchanged.')
    elif call.moves:
        sentences.append('Admitted in any state; then ' + call.moves + '.')
    requirement: Requirement
    for requirement in call.requires:
        sentences.append('Requires ' + requirement.holds + ', else '
                         + refusal_sentence(requirement.refuses, requirement.message, requirement.leaves) + '.')
    if call.refusal_order:
        sentences.append('When several refusals apply: ' + order_text(call) + '.')
    outcomes: list[str] = []
    failure: Failure
    for failure in call.fails:
        when: str = ' (' + failure.when + ')' if failure.when else ''
        outcomes.append(failure.failure + when + ', ' + leaves_text(failure.leaves))
    if outcomes:
        sentences.append('Fails ' + '; '.join(outcomes) + '.')
    if call.thread != declaration.thread:
        sentences.append('Thread: ' + call.thread + '.')
    returns: dict[str, object] = dict(call.returns)
    if returns:
        sentences.append(returns_sentence(returns))
    what: str
    source: str
    target: str
    for what, source, target in call.transfers:
        sentences.append('Transfers ' + what + ' from ' + source + ' to ' + target + '.')
    return sentences


def returns_sentence(returns: dict[str, object]) -> str:
    parts: list[str] = []
    if 'borrows' in returns:
        borrowed: str = 'a borrow of ' + ('the object' if returns['borrows'] == 'self' else str(returns['borrows']))
        until: object = returns.get('until', [])
        if isinstance(until, list) and until:
            ends: list[str] = [str(item) for item in until]
            borrowed = borrowed + ', valid until ' + (', '.join(ends[:-1]) + ' or ' + ends[-1] if len(ends) > 1
                                                      else ends[0])
        parts.append(borrowed)
    elif 'type' in returns:
        typed: str = str(returns['type'])
        if 'state' in returns:
            returned: object = returns['state']
            if isinstance(returned, list):
                named: list[str] = [str(item) for item in returned]
                typed = typed + ' in ' + (', '.join(named[:-1]) + ' or ' + named[-1] if len(named) > 1 else named[0])
            else:
                typed = typed + ' in ' + str(returned)
        parts.append(typed)
    if 'owns' in returns:
        parts.append('owned by the ' + str(returns['owns']))
    if 'revokes' in returns:
        parts.append('revoked ' + str(returns['revokes']))
    sentence: str = 'Returns ' + ', '.join(parts) + '.'
    return sentence


def component_sentences(declaration: Declaration, project: Project) -> list[str]:
    sentences: list[str] = [declaration.manifest_key() + ': generated from ' + declaration.path + ' by '
                            + project.generator + '; edit the declaration, not these lines.']
    if declaration.states:
        listed: list[str] = [state + (' (initial)' if state == declaration.initial else '')
                             for state in declaration.states]
        sentence: str = 'States: ' + ', '.join(listed) + '.'
        if declaration.state_query:
            answers: list[str] = [state + ' ' + answer for state, answer in declaration.observes]
            sentence = sentence + ' Observed by ' + declaration.state_query + '(): ' + ', '.join(answers) + '.'
        sentences.append(sentence)
    sentences.append('Thread: ' + declaration.thread + ', unless a call says otherwise.')
    if declaration.identity_kind and declaration.identity_stale is not None:
        sentences.append('Identity: ' + declaration.identity_kind + '; a stale one: '
                         + declaration.identity_stale.described() + ', unchanged.')
    return sentences


def wrapped(sentences: list[str], indent: str, limit: int) -> list[str]:
    width: int = limit - len(indent) - len(COMMENT_PREFIX) - 1
    lines: list[str] = []
    paragraph: str = ' '.join(sentences)
    piece: str
    for piece in textwrap.wrap(paragraph, width=width, break_long_words=False, break_on_hyphens=False):
        lines.append(indent + COMMENT_PREFIX + ' ' + piece)
    return lines


# ---- generators ------------------------------------------------------------------------------

def stripped_lines(text: str) -> list[str]:
    """The header's lines without generated contract lines."""
    kept: list[str] = [line for line in text.split('\n') if not line.lstrip().startswith(COMMENT_PREFIX)]
    return kept


def matching_line(lines: list[str], start: str, where: str, low: int = 0, high: int = -1) -> int:
    """The one line in [low, high) (high -1: the end) whose text, leading space ignored, starts with `start`."""
    end: int = len(lines) if high < 0 else high
    wanted: str = start.strip()
    found: list[int] = [index for index in range(low, end) if lines[index].strip().startswith(wanted)]
    if len(found) != 1:
        count: str = 'no line' if not found else str(len(found)) + ' lines'
        scope: str = 'of the header' if low == 0 and high < 0 else 'of the component\'s class'
        raise DeclarationError(where + ': ' + count + ' ' + scope + ' start with ' + repr(wanted))
    return found[0]


def class_end(lines: list[str], anchor_index: int) -> int:
    """The line after the anchor's class: its first `};` at the anchor's indentation, or, for a C
    `typedef struct name {`, its first `} name;` there (a C ABI's function table)."""
    line: str = lines[anchor_index]
    indent: str = line[:len(line) - len(line.lstrip())]
    closing: str = indent + '};'
    typedef_closing: re.Pattern[str] = re.compile(re.escape(indent) + r'\}\s*[A-Za-z_][A-Za-z0-9_]*\s*;\Z')
    index: int
    for index in range(anchor_index + 1, len(lines)):
        text: str = lines[index].rstrip()
        if text == closing or typedef_closing.match(text):
            return index + 1
    return len(lines)


def insertion_point(lines: list[str], index: int) -> int:
    """Above a `template <...>` line directly before the declaration, else the declaration itself."""
    if index > 0 and TEMPLATE_LINE.match(lines[index - 1]):
        return index - 1
    return index


def header_text(project: Project, header: str, declarations: list[Declaration]) -> str:
    """The header as the declarations generate it."""
    original: str = (project.root / header).read_text(encoding='utf-8')
    lines: list[str] = stripped_lines(original)
    insertions: list[tuple[int, list[str]]] = []
    declaration: Declaration
    for declaration in declarations:
        low: int = 0
        high: int = -1
        if declaration.anchor:
            anchor_index: int = matching_line(lines, declaration.anchor, declaration.path + ': anchor')
            low = anchor_index
            high = class_end(lines, anchor_index)
            point: int = insertion_point(lines, anchor_index)
            indent: str = lines[point][:len(lines[point]) - len(lines[point].lstrip())]
            insertions.append((point, wrapped(component_sentences(declaration, project), indent,
                                              project.column_limit)))
        call: Call
        for call in declaration.calls:
            where: str = declaration.path + ': call ' + call.name
            # A member is looked for inside the component's class (two
            # classes of one header may declare the same line); a free call
            # anywhere in the header.
            call_low: int = low if call.kind != 'free' else 0
            call_high: int = high if call.kind != 'free' else -1
            first: int = matching_line(lines, call.declarations[0], where, call_low, call_high)
            other: str
            for other in call.declarations[1:]:
                matching_line(lines, other, where, call_low, call_high)
            point = insertion_point(lines, first)
            indent = lines[point][:len(lines[point]) - len(lines[point].lstrip())]
            insertions.append((point, wrapped(call_sentences(declaration, call), indent, project.column_limit)))
    points: list[int] = [point for point, _ in insertions]
    if len(set(points)) != len(points):
        raise DeclarationError(header + ': two calls or anchors share one declaration line')
    added: list[str]
    point: int
    for point, added in sorted(insertions, key=insertion_order, reverse=True):
        lines[point:point] = added
    result: str = '\n'.join(lines)
    return result


def insertion_order(insertion: tuple[int, list[str]]) -> int:
    return insertion[0]


def checks_text(project: Project, declaration: Declaration) -> str:
    checks: Checks | None = declaration.checks
    assert checks is not None
    lines: list[str] = ['#pragma once', '']
    lines.extend(comment_lines('Entry checks of ' + declaration.manifest_key() + ', generated by ' + project.generator
                               + ' from ' + declaration.path + '; edit the declaration, not this file. Each '
                               'predicate is one `requires` clause of the declaration, so the condition the header '
                               'states and the check the code runs are one text (docs/ic_declarations.md in '
                               'template_harness).', '', project.column_limit))
    lines.append('')
    include: str
    for include in checks.includes:
        lines.append('#include "' + include + '"')
    if checks.includes:
        lines.append('')
    lines.append('namespace ' + checks.namespace + ' {')
    lines.append('')
    constant: Constant
    emitted_constant: bool = False
    for constant in declaration.constants:
        if constant.cpp:
            continue
        lines.append('// ' + constant.value_text)
        lines.append('inline constexpr ' + constant.type + ' ' + constant.name + ' = '
                     + cpp_literal(constant.value, True) + ';')
        emitted_constant = True
    if emitted_constant:
        lines.append('')
    types: dict[str, str] = dict(declaration.terms)
    cpp_names: dict[str, str] = {}
    for constant in declaration.constants:
        types[constant.name] = constant.type
        if constant.cpp:
            cpp_names[constant.name] = constant.cpp
    constant_names: set[str] = {constant.name for constant in declaration.constants}
    written: set[str] = set()
    call: Call
    for call in declaration.calls:
        call_types: dict[str, str] = dict(types)
        call_types.update(dict(call.parameters))
        requirement: Requirement
        for requirement in call.requires:
            if requirement.check in written:
                continue
            written.add(requirement.check)
            parameters: list[str] = [const_parameter(call_types[name]) + ' ' + name for name in requirement.names
                                     if name not in constant_names]
            clause: Node = renamed(requirement.clause, cpp_names)
            lines.extend(comment_lines(declaration.component + '::' + call.name + ': ' + requirement.holds + '; else '
                                       + requirement.refuses.described() + '.', '', project.column_limit))
            lines.append('[[nodiscard]] constexpr bool admits_' + requirement.check + '(' + ', '.join(parameters)
                         + ') noexcept {')
            lines.extend(result_lines(clause, project.column_limit))
            lines.append('    return result;')
            lines.append('}')
            if requirement.refuses.kind == 'requirement':
                lines.append('inline constexpr char ' + requirement.check + '_message[] = "'
                             + requirement.message.replace('\\', '\\\\').replace('"', '\\"') + '";')
            lines.append('')
    lines.append('} // namespace ' + checks.namespace)
    lines.append('')
    result: str = '\n'.join(lines)
    return result


def comment_lines(text: str, indent: str, limit: int) -> list[str]:
    width: int = limit - len(indent) - len('// ')
    lines: list[str] = [indent + '// ' + piece for piece in
                        textwrap.wrap(text, width=width, break_long_words=False, break_on_hyphens=False)]
    return lines


def result_lines(clause: Node, limit: int) -> list[str]:
    """`const bool result = <clause>;`, with a long conjunction or disjunction one operand per line."""
    first: str = '    const bool result = '
    single: str = first + outer_stripped(cpp_of(clause)) + ';'
    if len(single) <= limit:
        return [single]
    if clause.kind != 'logic':
        # A chained comparison renders as a conjunction of two comparisons.
        pieces: list[str] = outer_stripped(cpp_of(clause)).split(' && ')
        if len(pieces) != 2 or any(piece.count('(') != piece.count(')') for piece in pieces):
            return [single]
        return [first + pieces[0], ' ' * (len(first) - 3) + '&& ' + pieces[1] + ';']
    joiner: str = '&& ' if clause.operator == 'and' else '|| '
    operands: list[str] = [cpp_of(item) for item in clause.items]
    lines: list[str] = [first + operands[0]]
    operand: str
    for operand in operands[1:]:
        lines.append(' ' * (len(first) - len(joiner)) + joiner + operand)
    lines[-1] = lines[-1] + ';'
    return lines


def const_parameter(type_name: str) -> str:
    """A predicate parameter's type: const, once (a declared `const T*` already is)."""
    if type_name.startswith('const '):
        return type_name
    return 'const ' + type_name


def renamed(node: Node, cpp_names: dict[str, str]) -> Node:
    """The clause with constants that name existing C++ entities spelled as C++ (iterative rebuild)."""
    if not cpp_names:
        return node
    if node.kind == 'name':
        replacement: str = cpp_names.get(node.text, node.text)
        return dataclasses.replace(node, text=replacement)
    items: tuple[Node, ...] = tuple(renamed(item, cpp_names) for item in node.items)
    return dataclasses.replace(node, items=items)


def predicate_names(declaration: Declaration) -> list[str]:
    names: list[str] = []
    call: Call
    for call in declaration.calls:
        requirement: Requirement
        for requirement in call.requires:
            name: str = 'admits_' + requirement.check
            if name not in names:
                names.append(name)
    return names


def column_rows(declaration: Declaration) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    call: Call
    for call in declaration.calls:
        rows.append({'type': declaration.manifest_key(), 'member': call.name,
                     'constraints': ' '.join(call_sentences(declaration, call)) or 'No declared constraint.',
                     'declaration': declaration.path})
    return rows


def column_text(project: Project, declarations: list[Declaration]) -> str:
    rows: list[dict[str, str]] = []
    declaration: Declaration
    for declaration in declarations:
        rows.extend(column_rows(declaration))
    rows.sort(key=row_order)
    document: dict[str, object] = {'schema': SCHEMA, 'column': 'constraints', 'generated_by': project.generator,
                                   'rows': rows}
    result: str = json.dumps(document, indent=2) + '\n'
    return result


def row_order(row: dict[str, str]) -> tuple[str, str]:
    return (row['type'], row['member'])


def block_inputs(declaration: Declaration, call: Call, state: str) -> list[dict[str, object]]:
    inputs: list[dict[str, object]] = []
    if call.kind == 'member':
        subject: dict[str, object] = {'name': 'self', 'type': declaration.manifest_key()}
        if state:
            subject['state'] = state
        inputs.append(subject)
    name: str
    type_name: str
    for name, type_name in call.parameters:
        bounded: list[str] = [requirement.holds for requirement in call.requires if name in requirement.names]
        inputs.append({'name': name, 'type': type_name, 'bounded_by': bounded})
    return inputs


def blocks_text(project: Project, declaration: Declaration) -> str:
    prefix: str = declaration.manifest_key().replace('::', '.').lower()
    blocks: list[dict[str, object]] = []
    call: Call
    for call in declaration.calls:
        states: tuple[str, ...] = ()
        if call.kind == 'member':
            states = call.admitting or declaration.states
        legal_states: tuple[str, ...] = states or ('',)
        state: str
        for state in legal_states:
            name: str = prefix + '.' + call.identifier + ('.' + state if state else '')
            block: dict[str, object] = {
                'name': name, 'mode': 'legal', 'call': call.name, 'kind': call.kind, 'thread': call.thread,
                'state': state or None, 'moves': call.moves or None,
                'inputs': block_inputs(declaration, call, state),
                'requires': [requirement.holds for requirement in call.requires],
                'fails': [{'failure': failure.failure, 'when': failure.when, 'leaves': failure.leaves}
                          for failure in call.fails],
                'returns': dict(call.returns),
            }
            if call.refusal_order:
                block['refusal_order'] = list(call.refusal_order)
            blocks.append(block)
        requirement: Requirement
        for requirement in call.requires:
            blocks.append({'name': prefix + '.' + call.identifier + '.misuse.' + requirement.check, 'mode': 'misuse',
                           'call': call.name, 'violates': requirement.holds,
                           'expect': requirement_expectation(requirement), 'leaves': requirement.leaves
                           if requirement.refuses.kind != 'requirement' else 'process stops'})
        refusal: Refusal
        for state, refusal in call.outside:
            blocks.append({'name': prefix + '.' + call.identifier + '.outside.' + state, 'mode': 'misuse',
                           'call': call.name, 'state': state, 'expect': refusal.record(), 'leaves': 'unchanged'})
    document: dict[str, object] = {
        'schema': SCHEMA, 'component': declaration.manifest_key(), 'declaration': declaration.path,
        'generated_by': project.generator,
        'status': 'stub: the block bodies are phase IC2 (the engine); each block says what its body must do',
        'states': list(declaration.states), 'initial': declaration.initial or None,
        'state_query': declaration.state_query or None, 'blocks': blocks,
    }
    result: str = json.dumps(document, indent=2) + '\n'
    return result


def requirement_expectation(requirement: Requirement) -> dict[str, str]:
    expectation: dict[str, str] = requirement.refuses.record()
    if requirement.message:
        expectation['message'] = requirement.message
    return expectation


def obligations_text(project: Project, declaration: Declaration) -> str:
    prefix: str = declaration.manifest_key().replace('::', '.').lower()
    obligations: list[dict[str, object]] = []
    call: Call
    for call in declaration.calls:
        base: str = prefix + '.' + call.identifier
        requirement: Requirement
        for requirement in call.requires:
            evidence: str = ''
            if declaration.checks is not None:
                evidence = ('generated check ' + declaration.checks.namespace + '::admits_' + requirement.check
                            + ', called in ' + ', '.join(declaration.checks.used_in))
            obligations.append(open_obligation(base + '.requires.' + requirement.check, 'entry-check',
                                               call.name + ' runs only when ' + requirement.holds + '; otherwise '
                                               + refusal_sentence(requirement.refuses, requirement.message,
                                                                  requirement.leaves) + '.', evidence))
            if any(operator in requirement.holds for operator in ('-', '+', '*')):
                obligations.append(open_obligation(base + '.exact.' + requirement.check, 'predicate-exact',
                                                   'admits_' + requirement.check + ' computes ' + requirement.holds
                                                   + ' exactly: no subtraction below zero, no sum or product past '
                                                   'its type, in C++\'s order of evaluation.', ''))
            if requirement.refuses.kind != 'requirement' and requirement.leaves == 'unchanged':
                obligations.append(open_obligation(base + '.refusal.' + requirement.check, 'refusal-leaves',
                                                   'Unless ' + requirement.holds + ', ' + call.name
                                                   + ' changes nothing observable.', ''))
        if call.refusal_order:
            obligations.append(open_obligation(base + '.order', 'refusal-order',
                                               'When several of ' + call.name + '\'s refusals apply, the first of '
                                               + order_text(call) + ' is the one reported.', ''))
        if call.moves:
            source: str = ', '.join(call.admitting) if call.admitting else 'any state'
            obligations.append(open_obligation(base + '.moves', 'transition',
                                               call.name + ' succeeding in ' + source + ' leaves the object in '
                                               + call.moves + '.', ''))
        state: str
        refusal: Refusal
        for state, refusal in call.outside:
            obligations.append(open_obligation(base + '.outside.' + state, 'typestate-refusal',
                                               call.name + ' in ' + state + ': ' + refusal.described()
                                               + ', changing nothing observable.', ''))
    if declaration.identity_kind and declaration.identity_stale is not None:
        obligations.append(open_obligation(prefix + '.identity', 'identity',
                                           'A stale identity (' + declaration.identity_kind + ') is refused: '
                                           + declaration.identity_stale.described() + ', changing nothing observable.',
                                           ''))
    obligation: Obligation
    for obligation in declaration.obligations:
        record: dict[str, object] = open_obligation(prefix + '.' + obligation.identifier, 'stated',
                                                    obligation.statement, '')
        record['method'] = obligation.method
        record['premises'] = list(obligation.premises)
        if obligation.shows:
            record['given'] = list(obligation.given)
            record['shows'] = obligation.shows
            record['sources'] = list(obligation.sources)
        if obligation.untouched:
            record['untouched'] = list(obligation.untouched)
            record['paths'] = list(obligation.paths)
            record['metadata'] = list(obligation.metadata)
        if obligation.missing:
            record['missing'] = obligation.missing
        obligations.append(record)
    document: dict[str, object] = {
        'schema': SCHEMA, 'component': declaration.manifest_key(), 'generated_by': project.generator,
        'declaration': {'path': declaration.path,
                        'sha256': hashlib.sha256(declaration.text.encode('utf-8')).hexdigest()},
        'record': 'level, tool, premises and status as in STXlib\'s verification programme, section 6; '
                  'a discharged obligation records the source hash it was proved against',
        'obligations': obligations,
    }
    result: str = json.dumps(document, indent=2) + '\n'
    return result


def open_obligation(identifier: str, kind: str, statement: str, evidence: str) -> dict[str, object]:
    record: dict[str, object] = {'id': identifier, 'kind': kind, 'statement': statement, 'level': 'none',
                                 'tool': None, 'premises': [], 'status': 'open'}
    if evidence:
        record['evidence'] = evidence
    return record


def outputs_of(project: Project, declarations: list[Declaration]) -> dict[str, str]:
    """Every generated file's path and text."""
    outputs: dict[str, str] = {}
    by_header: dict[str, list[Declaration]] = {}
    declaration: Declaration
    for declaration in declarations:
        by_header.setdefault(declaration.header, []).append(declaration)
    header: str
    group: list[Declaration]
    for header, group in by_header.items():
        outputs[header] = header_text(project, header, group)
    for declaration in declarations:
        if declaration.checks is not None:
            if declaration.checks.path in outputs:
                raise DeclarationError(declaration.path + ': the checks path is another output')
            outputs[declaration.checks.path] = checks_text(project, declaration)
        outputs[project.blocks + '/' + declaration.identifier + '.blocks.json'] = blocks_text(project, declaration)
        outputs[project.obligations + '/' + declaration.identifier + '.obligations.json'] = \
            deduced(project, declaration, obligations_text(project, declaration))
    outputs[project.manifest_column] = column_text(project, declarations)
    engine_files: dict[str, str]
    summaries: list[EngineSummary]
    engine_files, summaries = engine_outputs(project, declarations)
    path: str
    for path in engine_files:
        if path in outputs:
            raise DeclarationError('the engine path ' + path + ' is another output')
    outputs.update(engine_files)
    if project.gate:
        by_declaration: dict[str, str] = {
            declaration.path: outputs[project.obligations + '/' + declaration.identifier + '.obligations.json']
            for declaration in declarations}
        outputs[project.gate] = gate_text(project, summaries, by_declaration)
    return outputs


def deduced(project: Project, declaration: Declaration, text: str) -> str:
    """The obligations with the deductions merged: an obligation the deducer (tools/ic_deduce.py, the IC
    study's phase IC4) discharged against this declaration's text and the named sources' current text is
    recorded as discharged, with its level, tool, premises and the hashes it was proved against; any other
    stays open. A deduction whose hashes no longer match is ignored, so a changed source reopens it."""
    if not project.deductions:
        return text
    path: Path = project.root / project.deductions
    if not path.is_file():
        return text
    loaded_deductions: object = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(loaded_deductions, dict):
        raise DeclarationError(project.deductions + ' is not a JSON object')
    records: object = loaded_deductions.get('deductions', [])
    if not isinstance(records, list):
        raise DeclarationError(project.deductions + ': deductions must be a list')
    by_id: dict[str, dict[str, object]] = {}
    record: object
    for record in records:
        if isinstance(record, dict) and isinstance(record.get('id'), str):
            by_id[str(record['id'])] = record
    document: dict[str, object] = json.loads(text)
    declaration_hash: str = hashlib.sha256(declaration.text.encode('utf-8')).hexdigest()
    items: object = document.get('obligations', [])
    assert isinstance(items, list)
    item: object
    for item in items:
        assert isinstance(item, dict)
        found: dict[str, object] | None = by_id.get(str(item.get('id')))
        if found is None or found.get('declaration_sha256') != declaration_hash:
            continue
        hashes: object = found.get('sources', {})
        if not isinstance(hashes, dict) or not sources_current(project.root, hashes):
            continue
        item['status'] = 'discharged'
        item['level'] = found.get('level', 'deduced')
        item['tool'] = found.get('tool', 'tools/ic_deduce.py')
        item['premises'] = found.get('premises', [])
        item['method'] = found.get('method', '')
        item['proved_against'] = hashes
    result: str = json.dumps(document, indent=2) + '\n'
    return result


def sources_current(root: Path, hashes: dict[object, object]) -> bool:
    name: object
    for name in hashes:
        file: Path = root / str(name)
        if not file.is_file():
            return False
        if hashlib.sha256(file.read_bytes()).hexdigest() != hashes[name]:
            return False
    return True


def unused_predicates(project: Project, declarations: list[Declaration], outputs: dict[str, str]) -> list[str]:
    """`<path>: <predicate>` for each generated predicate none of its used_in files calls."""
    findings: list[str] = []
    declaration: Declaration
    for declaration in declarations:
        if declaration.checks is None:
            continue
        texts: list[str] = []
        path: str
        for path in declaration.checks.used_in:
            text: str | None = outputs.get(path)
            if text is None:
                file: Path = project.root / path
                if not file.is_file():
                    raise DeclarationError(declaration.path + ': checks.used_in names a missing file ' + path)
                text = file.read_text(encoding='utf-8')
            texts.append(text)
        name: str
        for name in predicate_names(declaration):
            pattern: re.Pattern[str] = re.compile(r'\b' + re.escape(name) + r'\s*\(')
            if not any(pattern.search(text) for text in texts):
                findings.append(declaration.path + ': ' + name)
    return findings


# ---- the engine's blocks (phase IC2) ---------------------------------------------------------
#
# A declaration with an [engine] table also generates C++ blocks for the block
# engine: one legal block per driven call, one misuse block per clause and one
# per state a call refuses. The engine (the project's own C++, named by the
# project file's [engine] table) keeps each object's declared state and terms
# as its model, schedules only blocks whose states admit them, and after each
# block compares what the object reports with what the declaration predicted.
# The format of the tables is docs/ic_declarations.md, section 8.

ENGINE_PROJECT_KEYS: frozenset[str] = frozenset({'path', 'namespace', 'include', 'codes', 'include_roots'})
ENGINE_KEYS: frozenset[str] = frozenset(
    {'object', 'includes', 'make', 'observe', 'state_query', 'state_answers', 'slots', 'codes', 'invariants',
     'teardown', 'requirement_guard', 'skip', 'compound', 'condition'})
CALL_ENGINE_KEYS: frozenset[str] = frozenset(
    {'invoke', 'draws', 'values', 'pointers', 'ranges', 'ensures', 'expect', 'into', 'empties', 'needs', 'state_by',
     'skip', 'result_type', 'refused_when', 'failed_when', 'assume'})
COMPOUND_KEYS: frozenset[str] = frozenset({'name', 'mode', 'needs', 'in', 'invoke', 'result_type', 'expect',
                                           'empties', 'draws', 'values', 'ranges', 'ensures', 'assume',
                                           'failed_when'})
NEEDS: tuple[str, ...] = ('live', 'empty', 'any', 'none')
ERROR_KINDS: tuple[str, ...] = (
    'invalid_argument', 'out_of_range', 'limit_exceeded', 'out_of_memory', 'wrong_thread', 'disposed',
    'stale_identity', 'revoked', 'unsupported', 'not_found', 'permission_denied', 'already_exists', 'interrupted',
    'io_failure', 'foreign_failure', 'foreign_exception', 'internal_failure')
# Draw types: the C++ type, the class of draw and the largest value.
UNSIGNED_TYPES: dict[str, tuple[str, str]] = {
    'bool': ('bool', '1ULL'), 'u8': ('stxlib::u8', '0xffULL'), 'u16': ('stxlib::u16', '0xffffULL'),
    'u32': ('stxlib::u32', '0xffffffffULL'), 'u64': ('stxlib::u64', '0xffffffffffffffffULL'),
    'usize': ('stxlib::usize', 'static_cast<stxlib::u64>(~stxlib::usize{0})'),
    'Byte': ('stxlib::Byte', '0xffULL')}
SIGNED_TYPES: dict[str, tuple[str, str, str]] = {
    'i32': ('stxlib::i32', '-2147483647LL - 1', '2147483647LL'),
    'i64': ('stxlib::i64', '-9223372036854775807LL - 1', '9223372036854775807LL')}
VOCABULARY_TYPES: frozenset[str] = frozenset(
    {'usize', 'u8', 'u16', 'u32', 'u64', 'i32', 'i64', 'Byte', 'TextView', 'Path', 'View', 'MutableView', 'Buffer',
     'Vector', 'ErrorRecord'})
CPP_IDENTIFIER: re.Pattern[str] = re.compile(r'[A-Za-z_][A-Za-z0-9_]*')
CPP_TOKEN: re.Pattern[str] = re.compile(r'[A-Za-z_][A-Za-z0-9_]*|\s+|.')


@dataclasses.dataclass(frozen=True)
class EngineProject:
    path: str
    namespace: str
    include: str
    codes: str
    include_roots: tuple[str, ...]


@dataclasses.dataclass(frozen=True)
class Effect:
    term: str
    observed: bool
    node: Node | None
    text: str


@dataclasses.dataclass(frozen=True)
class CallEngine:
    invoke: str
    draws: tuple[tuple[str, str], ...]
    values: tuple[tuple[str, str], ...]
    pointers: tuple[tuple[str, str, str], ...]  # name, C++ type, legal value
    ranges: tuple[tuple[str, Node], ...]  # name, interval node
    ensures: tuple[Effect, ...]
    expect: tuple[str, ...]
    into: bool
    empties: bool
    needs: str
    state_by: tuple[str, tuple[str, ...]] | None
    skip: str
    result_type: str
    refused_when: str
    failed_when: str
    assume: tuple[str, ...] = ()


@dataclasses.dataclass(frozen=True)
class Compound:
    name: str
    mode: str
    needs: str
    admitting: tuple[str, ...]
    engine: CallEngine


@dataclasses.dataclass(frozen=True)
class EngineObject:
    object: str
    includes: tuple[str, ...]
    make: str
    observe: tuple[tuple[str, str], ...]
    state_query: str
    slots: int
    codes: str
    invariants: tuple[str, ...]
    teardown: tuple[tuple[str, str], ...]
    requirement_guard: str
    skip: str
    compounds: tuple[Compound, ...]
    state_answers: tuple[tuple[str, str], ...] = ()
    condition: str = ''


def engine_project_of(raw: dict[str, object]) -> EngineProject | None:
    if 'engine' not in raw:
        return None
    table: dict[str, object] = table_of(raw['engine'], 'the project file: engine')
    known_keys(table, ENGINE_PROJECT_KEYS, 'the project file: engine')
    roots: tuple[str, ...] = texts_of(table, 'include_roots', 'the project file: engine') or ('include', 'src')
    project: EngineProject = EngineProject(text_of(table, 'path', 'the project file: engine'),
                                           text_of(table, 'namespace', 'the project file: engine'),
                                           text_of(table, 'include', 'the project file: engine'),
                                           text_of(table, 'codes', 'the project file: engine', ''), roots)
    return project


def pairs_of(table: dict[str, object], key: str, where: str) -> tuple[tuple[str, str], ...]:
    inner: dict[str, object] = table_of(table.get(key, {}), where + ': ' + key)
    pairs: list[tuple[str, str]] = []
    name: str
    for name in inner:
        identifier_checked(name, where + ': ' + key)
        pairs.append((name, text_of(inner, name, where + ': ' + key)))
    return tuple(pairs)


def range_node(text: str, name: str, where: str) -> Node:
    """`[a, b]` (either end open with a parenthesis) as the clause `name in [a, b]`."""
    try:
        node: Node = parse_clause(name + ' in ' + text)
    except DeclarationError as error:
        raise DeclarationError(where + ': ranges.' + name + ': ' + str(error)) from None
    if node.kind != 'interval':
        raise DeclarationError(where + ': ranges.' + name + ' must be an interval such as [0, 64]')
    return node


def call_engine_of(value: object, where: str, terms: dict[str, str], visible: dict[str, str],
                   states: tuple[str, ...], default_needs: str, default_into: bool) -> CallEngine:
    table: dict[str, object] = table_of(value, where + ': engine')
    known_keys(table, CALL_ENGINE_KEYS, where + ': engine')
    draws: tuple[tuple[str, str], ...] = pairs_of(table, 'draws', where + ': engine')
    values: tuple[tuple[str, str], ...] = pairs_of(table, 'values', where + ': engine')
    pointer_table: dict[str, object] = table_of(table.get('pointers', {}), where + ': engine.pointers')
    pointers: list[tuple[str, str, str]] = []
    name: str
    for name in pointer_table:
        entry: dict[str, object] = table_of(pointer_table[name], where + ': engine.pointers.' + name)
        known_keys(entry, frozenset({'type', 'value'}), where + ': engine.pointers.' + name)
        pointers.append((identifier_checked(name, where + ': engine.pointers'),
                         text_of(entry, 'type', where + ': engine.pointers.' + name),
                         text_of(entry, 'value', where + ': engine.pointers.' + name)))
    names: dict[str, str] = dict(visible)
    for name, type_name in draws:
        if type_name not in UNSIGNED_TYPES and type_name not in SIGNED_TYPES and type_name != 'double':
            raise DeclarationError(where + ': engine.draws.' + name + ': ' + repr(type_name)
                                   + ' is not a type the engine draws')
        names[name] = type_name
    for name, _ in values:
        names.setdefault(name, 'value')
    for name, _, _ in pointers:
        names.setdefault(name, 'pointer')
    range_table: dict[str, object] = table_of(table.get('ranges', {}), where + ': engine.ranges')
    ranges: list[tuple[str, Node]] = []
    for name in range_table:
        if name not in names:
            raise DeclarationError(where + ': engine.ranges.' + name + ' names no parameter or draw')
        ranges.append((name, range_node(text_of(range_table, name, where + ': engine.ranges'), name, where)))
    effects: list[Effect] = []
    text: str
    for text in texts_of(table, 'ensures', where + ': engine'):
        parts: list[str] = text.split('=', 1)
        term: str = parts[0].strip()
        if len(parts) != 2 or term not in terms:
            raise DeclarationError(where + ': engine.ensures ' + repr(text) + ' must be `<term> = <value>`')
        right: str = parts[1].strip()
        if right == 'observed':
            effects.append(Effect(term, True, None, spelled(text)))
            continue
        parser: ClauseParser = ClauseParser(right)
        node: Node = parser.sum()
        if parser.peek().kind != 'end':
            raise DeclarationError(where + ': engine.ensures ' + repr(text) + ': unexpected '
                                   + repr(parser.peek().text))
        found: list[str] = []
        names_of(node, found)
        found_name: str
        for found_name in found:
            if found_name != 'old' and found_name not in names:
                raise DeclarationError(where + ': engine.ensures ' + repr(text) + ': ' + repr(found_name)
                                       + ' is not a parameter, draw, term or constant')
        effects.append(Effect(term, False, node, spelled(text)))
    needs: str = text_of(table, 'needs', where + ': engine', default_needs)
    if needs not in NEEDS:
        raise DeclarationError(where + ': engine.needs must be one of ' + ', '.join(NEEDS))
    state_by: tuple[str, tuple[str, ...]] | None = None
    if 'state_by' in table:
        by_table: dict[str, object] = table_of(table['state_by'], where + ': engine.state_by')
        known_keys(by_table, frozenset({'draw', 'states'}), where + ': engine.state_by')
        by_draw: str = text_of(by_table, 'draw', where + ': engine.state_by')
        by_states: tuple[str, ...] = texts_of(by_table, 'states', where + ': engine.state_by')
        if by_draw not in names or not by_states or any(state not in states for state in by_states):
            raise DeclarationError(where + ': engine.state_by needs a draw and declared states')
        state_by = (by_draw, by_states)
    into: object = table.get('into', default_into)
    empties: object = table.get('empties', False)
    if not isinstance(into, bool) or not isinstance(empties, bool):
        raise DeclarationError(where + ': engine.into and engine.empties are true or false')
    engine: CallEngine = CallEngine(text_of(table, 'invoke', where + ': engine', ''), draws, values, tuple(pointers),
                                    tuple(ranges), tuple(effects), texts_of(table, 'expect', where + ': engine'),
                                    into, empties, needs, state_by, text_of(table, 'skip', where + ': engine', ''),
                                    text_of(table, 'result_type', where + ': engine', ''),
                                    text_of(table, 'refused_when', where + ': engine', ''),
                                    text_of(table, 'failed_when', where + ': engine', ''),
                                    texts_of(table, 'assume', where + ': engine'))
    return engine


def engine_object_of(raw: dict[str, object], terms: dict[str, str], states: tuple[str, ...],
                     visible: dict[str, str]) -> EngineObject | None:
    if 'engine' not in raw:
        return None
    where: str = 'engine'
    table: dict[str, object] = table_of(raw['engine'], where)
    known_keys(table, ENGINE_KEYS, where)
    observe: tuple[tuple[str, str], ...] = pairs_of(table, 'observe', where)
    term: str
    for term, _ in observe:
        if term not in terms:
            raise DeclarationError('engine.observe.' + term + ' is not a declared term')
    missing: list[str] = [name for name in terms if name not in dict(observe)]
    skip: str = text_of(table, 'skip', where, '')
    if missing and not skip:
        raise DeclarationError('engine.observe must read every term: ' + ', '.join(missing))
    teardown: tuple[tuple[str, str], ...] = pairs_of(table, 'teardown', where)
    state: str
    for state, _ in teardown:
        if state not in states:
            raise DeclarationError('engine.teardown.' + state + ' is not a declared state')
    slots: object = table.get('slots', 2)
    if not isinstance(slots, int) or slots < 1 or slots > 4:
        raise DeclarationError('engine.slots must be 1 to 4')
    compounds: list[Compound] = []
    value: object
    position: int
    compound_value: object = table.get('compound', [])
    if not isinstance(compound_value, list):
        raise DeclarationError('engine.compound must be an array of tables')
    for position, value in enumerate(compound_value):
        compound_where: str = 'engine.compound ' + str(position + 1)
        compound_table: dict[str, object] = table_of(value, compound_where)
        known_keys(compound_table, COMPOUND_KEYS, compound_where)
        name: str = identifier_checked(text_of(compound_table, 'name', compound_where), compound_where)
        mode: str = text_of(compound_table, 'mode', compound_where, 'legal')
        if mode not in ('legal', 'misuse'):
            raise DeclarationError(compound_where + ': mode is legal or misuse')
        admitting: tuple[str, ...] = texts_of(compound_table, 'in', compound_where)
        if any(item not in states for item in admitting):
            raise DeclarationError(compound_where + ': in names undeclared states')
        engine_table: dict[str, object] = {key: compound_table[key] for key in compound_table
                                           if key in CALL_ENGINE_KEYS}
        compound_engine: CallEngine = call_engine_of(engine_table, compound_where, terms, visible, states, 'live',
                                                     False)
        if not compound_engine.invoke:
            raise DeclarationError(compound_where + ' needs invoke')
        compounds.append(Compound(name, mode, compound_engine.needs, admitting, compound_engine))
    engine: EngineObject = EngineObject(text_of(table, 'object', where, ''), texts_of(table, 'includes', where),
                                        text_of(table, 'make', where, ''), observe,
                                        text_of(table, 'state_query', where, ''), slots,
                                        text_of(table, 'codes', where, ''), texts_of(table, 'invariants', where),
                                        teardown, text_of(table, 'requirement_guard', where, ''), skip,
                                        tuple(compounds), pairs_of(table, 'state_answers', where),
                                        text_of(table, 'condition', where, ''))
    answered: list[str] = [state for state, _ in engine.state_answers]
    if engine.state_answers and sorted(answered) != sorted(states):
        raise DeclarationError('engine.state_answers must answer in every state')
    if engine.state_answers and not engine.state_query:
        raise DeclarationError('engine.state_answers needs engine.state_query')
    if not engine.skip and not engine.object:
        raise DeclarationError('engine needs object (the C++ type a slot holds), or skip with the reason')
    return engine


# ---- reading C++ declarations ------------------------------------------------------------------

def declared_return(declaration_text: str) -> str:
    """The return type of a declaration line, without attributes, `static`, macros and `constexpr`."""
    text: str = re.sub(r'\[\[[^\]]*\]\]', ' ', declaration_text)
    head: str = text.split('(')[0]
    words: list[str] = head.replace('&', ' & ').replace('*', ' * ').split()
    kept: list[str] = [word for word in words if word not in ('static', 'constexpr', 'inline', 'virtual', 'explicit')
                       and not (word.isupper() and word.startswith('STXLIB'))]
    if len(kept) < 2:
        return ''
    result: str = ''.join(piece if piece in ('&', '*') else ' ' + piece for piece in kept[:-1]).strip()
    return result


def declared_parameters(declaration_text: str) -> list[str]:
    """The parameter names of a declaration line (empty when the line does not hold them all)."""
    if '(' not in declaration_text or ')' not in declaration_text:
        return []
    inside: str = declaration_text[declaration_text.index('(') + 1:declaration_text.rindex(')')]
    if not inside.strip():
        return []
    names: list[str] = []
    depth: int = 0
    current: str = ''
    character: str
    pieces: list[str] = []
    for character in inside:
        if character in '<([{':
            depth = depth + 1
        elif character in '>)]}':
            depth = depth - 1
        if character == ',' and depth == 0:
            pieces.append(current)
            current = ''
            continue
        current = current + character
    pieces.append(current)
    piece: str
    for piece in pieces:
        before_default: str = piece.split('=')[0].strip()
        found: list[str] = CPP_IDENTIFIER.findall(before_default)
        if not found:
            return []
        names.append(found[-1])
    return names


def result_kind(call: Call, engine: CallEngine | None) -> str:
    returned: str = declared_return(call.declarations[0])
    if returned.startswith('OperationStatus'):
        return 'status'
    if returned.startswith('OperationResult'):
        return 'result'
    if returned == 'void' or returned == '':
        return 'none' if not (engine and engine.result_type) else 'value'
    return 'value'


def result_type_of(declaration: Declaration, call: Call, engine: CallEngine) -> str:
    if engine.result_type:
        return engine.result_type
    returned: str = declared_return(call.declarations[0])
    inner: str = returned
    if returned.startswith('OperationResult<') and returned.endswith('>'):
        inner = returned[len('OperationResult<'):-1]
    inner = inner.replace('const ', '').strip()
    if inner == declaration.component or inner.startswith(declaration.component + '<'):
        return 'Object'
    if inner in VOCABULARY_TYPES:
        return 'stxlib::' + inner
    if CPP_IDENTIFIER.fullmatch(inner):
        return declaration.namespace + '::' + inner
    raise DeclarationError(declaration.path + ': call ' + call.name + ': engine.result_type is needed for '
                           + repr(returned))


# ---- the generated C++ ---------------------------------------------------------------------------

@dataclasses.dataclass
class Variable:
    name: str
    type_name: str
    kind: str  # unsigned, signed, real, pointer, value
    cpp_type: str
    largest: str
    smallest: str = ''
    expression: str = ''


@dataclasses.dataclass
class Bound:
    low: list[tuple[Node, bool]]
    high: list[tuple[Node, bool]]
    properties: set[str]
    excluded: list[Node] = dataclasses.field(default_factory=list)  # `x != e`: outside is exactly e


class BlockWriter:
    """Writes one component's blocks."""

    def __init__(self, project: Project, declaration: Declaration) -> None:
        self.project: Project = project
        self.declaration: Declaration = declaration
        self.engine: EngineObject = declaration.engine  # type: ignore[assignment]
        self.engine_project: EngineProject = project.engine  # type: ignore[assignment]
        self.terms: list[str] = [name for name, _ in declaration.terms]
        self.term_types: dict[str, str] = dict(declaration.terms)
        self.states: tuple[str, ...] = declaration.states or ('any',)
        self.constants: dict[str, Constant] = {constant.name: constant for constant in declaration.constants}
        self.lines: list[str] = []
        self.entries: list[tuple[str, str, str, int, str, int, str]] = []
        self.skipped: list[tuple[str, str]] = []
        self.checks_namespace: str = '::' + declaration.checks.namespace if declaration.checks else ''

    # -- names

    def constant_text(self, name: str) -> str:
        constant: Constant = self.constants[name]
        if constant.cpp:
            if constant.cpp.startswith('::') or '::' in constant.cpp:
                return constant.cpp
            return '::' + self.declaration.namespace + '::' + constant.cpp
        return self.checks_namespace + '::' + name

    def local_of(self, name: str, variables: dict[str, Variable]) -> str:
        if name.startswith('old.'):
            return 't_' + name[len('old.'):]
        if name in variables:
            return 'v_' + name
        if name in self.term_types:
            return 't_' + name
        if name in self.constants:
            return self.constant_text(name)
        raise DeclarationError(self.declaration.path + ': ' + repr(name) + ' is not known to the engine')

    def clause_cpp(self, node: Node, variables: dict[str, Variable]) -> str:
        found: list[str] = []
        names_of_full(node, found)
        mapping: dict[str, str] = {name: self.local_of(name, variables) for name in found}
        renamed_node: Node = renamed(node, mapping)
        text: str = outer_stripped(cpp_of(renamed_node))
        return text

    def rename_cpp(self, text: str, variables: dict[str, Variable], used: set[str]) -> str:
        """A C++ text with the declaration's names as the block's locals (a name after `.`, `->` or `::` is a member
        and stays)."""
        pieces: list[str] = CPP_TOKEN.findall(text)
        output: list[str] = []
        index: int
        for index, piece in enumerate(pieces):
            previous: str = ''.join(pieces[:index]).rstrip()
            member: bool = previous.endswith('.') or previous.endswith('->') or previous.endswith('::')
            if CPP_IDENTIFIER.fullmatch(piece) and not member:
                if piece in variables:
                    used.add(piece)
                    output.append('v_' + piece)
                    continue
                if piece in self.term_types:
                    used.add(piece)
                    output.append('t_' + piece)
                    continue
                if piece in ('object', 'result', 'holder'):
                    used.add(piece)
                if piece == 'slot':
                    used.add(piece)
                    output.append('step.slot')
                    continue
            output.append(piece)
        result: str = ''.join(output)
        return result

    def line_pieces(self, text: str, variables: dict[str, Variable]) -> list[str]:
        """The transcript builder's calls for a C++ text: values for the block's names, the slot's object."""
        pieces: list[str] = CPP_TOKEN.findall(text)
        calls: list[str] = []
        pending: str = ''
        index: int
        for index, piece in enumerate(pieces):
            previous: str = ''.join(pieces[:index]).rstrip()
            member: bool = previous.endswith('.') or previous.endswith('->') or previous.endswith('::')
            replacement: str = ''
            if CPP_IDENTIFIER.fullmatch(piece) and not member:
                if piece in variables:
                    variable: Variable = variables[piece]
                    if variable.kind == 'unsigned':
                        replacement = '.unsigned_value(static_cast<stxlib::u64>(v_' + piece + '))'
                    elif variable.kind == 'signed':
                        replacement = '.signed_value(static_cast<stxlib::i64>(v_' + piece + '))'
                    elif variable.kind == 'real':
                        replacement = '.real_value(v_' + piece + ')'
                    elif variable.kind == 'pointer':
                        replacement = ('.text(v_' + piece + ' == nullptr ? "nullptr" : "'
                                       + cpp_string(variable.expression) + '")')
                    else:
                        replacement = '.text("' + cpp_string(variable.expression) + '")'
                elif piece == 'object':
                    replacement = '.object(step.slot)'
                elif piece == 'holder':
                    replacement = '.holder(step.slot)'
                elif piece == 'slot':
                    replacement = '.unsigned_value(step.slot)'
            if replacement:
                if pending:
                    calls.append('.text("' + cpp_string(pending) + '")')
                    pending = ''
                calls.append(replacement)
                continue
            pending = pending + piece
        if pending:
            calls.append('.text("' + cpp_string(pending) + '")')
        return calls

    # -- variables and their bounds

    def variables_of(self, call: Call | None, engine: CallEngine) -> dict[str, Variable]:
        variables: dict[str, Variable] = {}
        pointers: dict[str, tuple[str, str]] = {name: (type_name, value) for name, type_name, value in engine.pointers}
        values: dict[str, str] = dict(engine.values)
        declared: list[tuple[str, str]] = list(call.parameters) if call else []
        declared.extend(engine.draws)
        name: str
        type_name: str
        for name, type_name in declared:
            if name in variables:
                raise DeclarationError(self.declaration.path + ': ' + name + ' is both a parameter and a draw')
            if name in values:
                variables[name] = Variable(name, type_name, 'value', self.cpp_type(type_name, name), '', '',
                                           values[name])
            elif name in pointers:
                variables[name] = Variable(name, type_name, 'pointer', pointers[name][0], '', '', pointers[name][1])
            elif type_name in UNSIGNED_TYPES:
                variables[name] = Variable(name, type_name, 'unsigned', UNSIGNED_TYPES[type_name][0],
                                           UNSIGNED_TYPES[type_name][1])
            elif type_name in SIGNED_TYPES:
                signed: tuple[str, str, str] = SIGNED_TYPES[type_name]
                variables[name] = Variable(name, type_name, 'signed', signed[0], signed[2], signed[1])
            elif type_name == 'double':
                variables[name] = Variable(name, type_name, 'real', 'double', '')
            else:
                raise DeclarationError(self.declaration.path + ': ' + (call.name if call else 'compound') + ': '
                                       + name + ' has type ' + repr(type_name)
                                       + ', which the engine does not draw: give engine.values or engine.pointers')
        for name in values:
            if name not in variables:
                variables[name] = Variable(name, 'value', 'value', 'auto_type', '', '', values[name])
        for name in pointers:
            if name not in variables:
                variables[name] = Variable(name, 'pointer', 'pointer', pointers[name][0], '', '', pointers[name][1])
        # Values first: a clause's bound on a draw may name them (`glyph < glyph_count`).
        ordered: dict[str, Variable] = {name: variable for name, variable in variables.items()
                                        if variable.kind == 'value'}
        ordered.update(variables)
        return ordered

    def cpp_type(self, type_name: str, name: str) -> str:
        if type_name in UNSIGNED_TYPES:
            return UNSIGNED_TYPES[type_name][0]
        if type_name in SIGNED_TYPES:
            return SIGNED_TYPES[type_name][0]
        if type_name == 'double':
            return 'double'
        raise DeclarationError(self.declaration.path + ': engine.values.' + name + ' needs a type the engine knows')

    def bounds_of(self, clauses: list[Node], order: list[str], variables: dict[str, Variable]) -> dict[str, Bound]:
        """Each variable's bounds from the clauses' top-level conjuncts whose other side names only earlier
        variables, terms and constants."""
        bounds: dict[str, Bound] = {name: Bound([], [], set()) for name in order}
        position: dict[str, int] = {name: index for index, name in enumerate(order)}
        conjuncts: list[Node] = []
        clause: Node
        for clause in clauses:
            conjuncts.extend(top_conjuncts(clause))
        item: Node
        for item in conjuncts:
            if item.kind == 'predicate' and item.items[0].kind == 'name' and item.items[0].text in bounds:
                bounds[item.items[0].text].properties.add(item.text)
                continue
            if item.kind == 'interval' and item.items[0].kind == 'name' and item.items[0].text in bounds:
                subject: str = item.items[0].text
                if self.earlier(item.items[1], subject, position, variables):
                    bounds[subject].low.append((item.items[1], item.low_open))
                if self.earlier(item.items[2], subject, position, variables):
                    bounds[subject].high.append((item.items[2], item.high_open))
                continue
            if item.kind != 'compare':
                continue
            left: Node = item.items[0]
            right: Node = item.items[1]
            operator: str = item.operator
            if left.kind != 'name' or left.text not in bounds:
                if right.kind == 'name' and right.text in bounds:
                    left, right = right, left
                    operator = {'<': '>', '<=': '>=', '>': '<', '>=': '<=', '==': '==', '!=': '!='}[operator]
                else:
                    continue
            subject = left.text
            if not self.earlier(right, subject, position, variables):
                continue
            if operator in ('<', '<='):
                bounds[subject].high.append((right, operator == '<'))
            elif operator in ('>', '>='):
                bounds[subject].low.append((right, operator == '>'))
            elif operator == '==':
                bounds[subject].low.append((right, False))
                bounds[subject].high.append((right, False))
            elif operator == '!=':
                bounds[subject].excluded.append(right)
        return bounds

    def earlier(self, node: Node, subject: str, position: dict[str, int], variables: dict[str, Variable]) -> bool:
        found: list[str] = []
        names_of_full(node, found)
        name: str
        for name in found:
            if name in position:
                if position[name] >= position[subject]:
                    return False
            elif name not in self.term_types and name not in self.constants and not name.startswith('old.'):
                return False
        return True

    # -- the parts of a block

    def draw_lines(self, order: list[str], variables: dict[str, Variable], legal: dict[str, Bound],
                   ranges: dict[str, Node], target: str, target_bound: Bound | None, used: set[str],
                   label: str) -> list[str]:
        lines: list[str] = []
        name: str
        for name in order:
            variable: Variable = variables[name]
            if variable.kind == 'value':
                if variable.cpp_type == 'auto_type':
                    raise DeclarationError(self.declaration.path + ': ' + label + ': engine.values.' + name
                                           + ' needs a declared type (a parameter or a draw of that name)')
                lines.append('    const ' + variable.cpp_type + ' v_' + name + ' = ' + variable.expression + ';')
                continue
            if variable.kind == 'pointer':
                if name == target:
                    lines.append('    const ' + variable.cpp_type + ' v_' + name + ' = nullptr;')
                else:
                    lines.append('    const ' + variable.cpp_type + ' v_' + name + ' = ' + variable.expression + ';')
                continue
            if variable.kind == 'real':
                if name == target:
                    lines.append('    const double v_' + name + ' = step.draw.not_finite();')
                elif name in ranges:
                    node: Node = ranges[name]
                    lines.append('    const double v_' + name + ' = step.draw.real(static_cast<double>('
                                 + self.clause_cpp(node.items[1], variables) + '), static_cast<double>('
                                 + self.clause_cpp(node.items[2], variables) + '));')
                else:
                    lines.append('    const double v_' + name + ' = step.draw.any_real();')
                continue
            signed: bool = variable.kind == 'signed'
            word: str = 'stxlib::i64' if signed else 'stxlib::u64'
            helper: str = '_signed' if signed else ''
            bound: Bound = target_bound if (name == target and target_bound is not None) else legal[name]
            low_start: str = variable.smallest if signed else '0ULL'
            lines.append('    ' + word + ' ' + name + '_low = ' + low_start + ';')
            lines.append('    ' + word + ' ' + name + '_high = ' + variable.largest + ';')
            lines.append('    bool ' + name + '_open = true;')
            pieces: list[tuple[str, Node, bool]] = [('low', node, strict) for node, strict in bound.low]
            pieces.extend(('high', node, strict) for node, strict in bound.high)
            if name != target and name in ranges:
                node = ranges[name]
                pieces.append(('low', node.items[1], node.low_open))
                pieces.append(('high', node.items[2], node.high_open))
            side: str
            strict: bool
            for side, node, strict in pieces:
                lines.append('    ' + name + '_open = ' + name + '_open && ::stxlib_ic::tighten_' + side + helper + '('
                             + name + '_' + side + ', static_cast<' + word + '>(' + self.clause_cpp(node, variables)
                             + '), ' + ('true' if strict else 'false') + ');')
            if name == target and bound.excluded and not (bound.low or bound.high or bound.properties):
                lines.append('    const ' + word + ' ' + name + '_drawn = static_cast<' + word + '>('
                             + self.clause_cpp(bound.excluded[0], variables) + ');')
            elif name == target:
                if 'power_of_two' in bound.properties:
                    lines.append('    const ' + word + ' ' + name + '_drawn = step.draw.not_power_of_two(' + name
                                 + '_high);')
                else:
                    lines.append('    ' + word + ' ' + name + '_drawn = 0;')
                    lines.append('    if (!step.draw.outside' + helper + '(' + name + '_low, ' + name + '_high, '
                                 + (variable.smallest + ', ' if signed else '') + variable.largest + ', ' + name
                                 + '_drawn)) return step.bail("' + label + ': every value of ' + name
                                 + ' is admitted");')
            else:
                lines.append('    if (!' + name + '_open || ' + name + '_low > ' + name + '_high) return step.bail("'
                             + label + ': no admitted value of ' + name + '");')
                if 'power_of_two' in bound.properties and not signed:
                    lines.append('    const stxlib::u64 ' + name + '_drawn = step.draw.power_of_two(' + name + '_low, '
                                 + name + '_high);')
                else:
                    lines.append('    const ' + word + ' ' + name + '_drawn = step.draw.pick' + helper + '(' + name
                                 + '_low, ' + name + '_high);')
            lines.append('    const ' + variable.cpp_type + ' v_' + name + ' = static_cast<' + variable.cpp_type + '>('
                         + name + '_drawn);')
            lines.append('    static_cast<void>(' + name + '_low);')
            lines.append('    static_cast<void>(' + name + '_high);')
            lines.append('    static_cast<void>(' + name + '_open);')
        return lines

    def predicate_call(self, requirement: Requirement, variables: dict[str, Variable]) -> str:
        arguments: list[str] = []
        name: str
        for name in requirement.names:
            if name in self.constants:
                continue
            if name in variables:
                arguments.append('v_' + name)
            else:
                arguments.append('static_cast<' + qualified_vocabulary(self.term_types[name]) + '>(t_' + name + ')')
        call: str = self.checks_namespace + '::admits_' + requirement.check + '(' + ', '.join(arguments) + ')'
        return call

    def term_lines(self, used: set[str]) -> list[str]:
        lines: list[str] = []
        index: int
        term: str
        for index, term in enumerate(self.terms):
            if term in used:
                lines.append('    const stxlib::u64 t_' + term + ' = step.before.terms[' + str(index) + '];')
                lines.append('    static_cast<void>(t_' + term + ');')
        return lines

    def kinds_mask(self, failures: tuple[Failure, ...]) -> str:
        kinds: list[str] = [failure.failure for failure in failures if failure.failure in ERROR_KINDS]
        if not kinds:
            return '0U'
        return ' | '.join('::stxlib_ic::kind_bit(stxlib::ErrorKind::' + kind + ')' for kind in kinds)

    def codes_enum(self) -> str:
        return self.engine.codes or self.engine_project.codes

    def refusal_check(self, refusal: Refusal, outcome: str, result_kind_text: str, engine: CallEngine,
                      call_name: str) -> list[str]:
        """The lines that check a misuse was refused as declared; `outcome` is the status or result local."""
        lines: list[str] = []
        if refusal.kind == 'failure':
            if result_kind_text not in ('status', 'result'):
                raise DeclarationError(self.declaration.path + ': ' + call_name + ' refuses with a failure but '
                                       'returns no status')
            kind: str = '0U' if refusal.failure == 'any' else '::stxlib_ic::kind_bit(stxlib::ErrorKind::' + \
                refusal.failure + ')'
            if refusal.failure != 'any' and refusal.failure not in ERROR_KINDS:
                raise DeclarationError(self.declaration.path + ': ' + call_name + ': ' + refusal.failure
                                       + ' is not an ErrorKind')
            code: str = '0U'
            if refusal.code:
                if not self.codes_enum():
                    raise DeclarationError(self.declaration.path + ': a refusal code needs engine.codes')
                code = 'static_cast<stxlib::u32>(' + self.codes_enum() + '::' + refusal.code + ')'
            lines.append('    if (' + outcome + '.succeeded()) return step.defect('
                         '::stxlib_ic::DefectKind::misuse_accepted, "' + cpp_string(call_name) + ' succeeded");')
            lines.append('    return step.refused(*' + outcome + '.error(), ' + kind + ', ' + code + ', '
                         + ('true' if refusal.code else 'false') + ');')
            return lines
        condition: str = engine.refused_when
        if not condition:
            if refusal.kind == 'null':
                condition = 'result == nullptr'
            else:
                value: str = refusal.value
                condition = 'result == ' + (value if '::' in value and value.startswith('::')
                                            else '::' + self.declaration.namespace + '::' + value)
        lines.append('    if (!(' + condition + ')) return step.defect(::stxlib_ic::DefectKind::misuse_accepted, "'
                     + cpp_string(call_name) + ': not ' + cpp_string(refusal.described()) + '");')
        lines.append('    return step.refused_by_value();')
        return lines

    def targets(self, call: Call, engine: CallEngine, requirement: Requirement) -> list[str]:
        """The variables a misuse of `requirement` can draw outside the clause: each one the clause bounds."""
        variables: dict[str, Variable] = self.variables_of(call, engine)
        order: list[str] = list(variables)
        bounds: dict[str, Bound] = self.bounds_of([requirement.clause], order, variables)
        found: list[str] = []
        names_of_full(requirement.clause, found)
        result: list[str] = []
        name: str
        for name in order:
            if name in found and variables[name].kind in ('unsigned', 'signed', 'real', 'pointer'):
                bound: Bound = bounds[name]
                if bound.low or bound.high or bound.properties or bound.excluded:
                    result.append(name)
        return result

    def block(self, function: str, label: str, call: Call | None, engine: CallEngine, kind_text: str, mode: str,
              target: Requirement | None, refusal: Refusal | None, outside_state: str, chosen: str = '') -> None:
        """One block function."""
        declaration: Declaration = self.declaration
        variables: dict[str, Variable] = self.variables_of(call, engine)
        order: list[str] = list(variables)
        requirements: tuple[Requirement, ...] = call.requires if call else ()
        clauses: list[Node] = [requirement.clause for requirement in requirements]
        legal: dict[str, Bound] = self.bounds_of(clauses, order, variables)
        ranges: dict[str, Node] = dict(engine.ranges)
        target_name: str = ''
        target_bound: Bound | None = None
        if target is not None:
            target_bounds: dict[str, Bound] = self.bounds_of([target.clause], order, variables)
            found: list[str] = []
            names_of_full(target.clause, found)
            name: str
            for name in order:
                if chosen and name != chosen:
                    continue
                if name in found and variables[name].kind in ('unsigned', 'signed', 'real', 'pointer'):
                    candidate: Bound = target_bounds[name]
                    if candidate.low or candidate.high or candidate.properties or candidate.excluded:
                        target_name = name
                        target_bound = candidate
                        break
        used: set[str] = set()
        invoke: str = self.rename_cpp(engine.invoke, variables, used)
        expects: list[str] = [self.rename_cpp(text, variables, used) for text in engine.expect]
        refused_when: str = self.rename_cpp(engine.refused_when, variables, used) if engine.refused_when else ''
        failed_when: str = self.rename_cpp(engine.failed_when, variables, used) if engine.failed_when else ''
        engine_for_refusal: CallEngine = dataclasses.replace(engine, refused_when=refused_when)
        effects: list[str] = []
        effect: Effect
        for effect in engine.ensures:
            index: int = self.terms.index(effect.term)
            if effect.observed:
                effects.append('    step.observe_term(' + str(index) + 'U);')
                continue
            found_names: list[str] = []
            names_of_full(effect.node, found_names)  # type: ignore[arg-type]
            for name in found_names:
                used.add(name[len('old.'):] if name.startswith('old.') else name)
            effects.append('    step.after.terms[' + str(index) + '] = static_cast<stxlib::u64>('
                           + self.clause_cpp(effect.node, variables) + ');')  # type: ignore[arg-type]
        for requirement in requirements:
            used.update(requirement.names)
        for name in order:
            pieces_bounds: Bound = legal[name]
            for node, _ in pieces_bounds.low + pieces_bounds.high:
                found_bound: list[str] = []
                names_of_full(node, found_bound)
                used.update(item[len('old.'):] if item.startswith('old.') else item for item in found_bound)
        for name, node in ranges.items():
            found_range: list[str] = []
            names_of_full(node, found_range)
            used.update(item for item in found_range if item != name)
        if engine.state_by:
            used.add(engine.state_by[0])
        if mode == 'misuse' and refusal is not None and refusal.kind in ('null', 'value'):
            used.add('result')
        result_type: str = ''
        if kind_text == 'result' or (kind_text == 'value' and (engine.into or 'result' in used)):
            if engine.result_type:
                result_type = engine.result_type
            elif call is not None:
                result_type = result_type_of(declaration, call, engine)
            else:
                raise DeclarationError(declaration.path + ': ' + label + ' needs engine.result_type')
        self.lines.append('// ' + label + '.')
        self.lines.append('inline ::stxlib_ic::Outcome ' + function + '(::stxlib_ic::Step& step, ObjectHolder& holder) '
                          'noexcept {')
        body: list[str] = []
        if 'holder' not in used:
            body.append('    static_cast<void>(holder);')
        if engine.needs == 'live' and 'object' in used:
            body.append('    Object& object = *holder.get();')
            body.append('    static_cast<void>(object);')
        body.extend(self.term_lines(used))
        body.extend(self.draw_lines(order, variables, legal, ranges, target_name, target_bound, used, label))
        for name in order:
            body.append('    static_cast<void>(v_' + name + ');')
        if mode == 'legal':
            for requirement in requirements:
                body.append('    if (!' + self.predicate_call(requirement, variables) + ') return step.bail("' + label
                            + ': the draw misses ' + requirement.check + '");')
        else:
            for requirement in requirements:
                if target is not None and requirement.check == target.check:
                    body.append('    if (' + self.predicate_call(requirement, variables) + ') return step.bail("'
                                + label + ': ' + requirement.check + ' holds");')
                else:
                    body.append('    if (!' + self.predicate_call(requirement, variables) + ') return step.bail("'
                                + label + ': ' + requirement.check + ' fails too");')
        for text in engine.assume:
            body.append('    if (!(' + self.rename_cpp(text, variables, used) + ')) return step.bail("' + label
                        + ': the inputs cannot realise ' + cpp_string(text) + '");')
        if mode == 'misuse' and refusal is not None and refusal.kind == 'requirement':
            if self.engine.requirement_guard:
                body.append('    if (!(' + self.engine.requirement_guard + ')) return step.bail("' + label
                            + ': this build does not check it");')
            body.extend(self.requirement_lines(function, label, invoke, variables, engine, target))
            self.lines.extend(body)
            self.lines.append('}')
            self.lines.append('')
            return
        line: str
        if mode == 'misuse' and refusal is not None and refusal.kind == 'failure':
            line = 'CHECK(::stxlib_ic::refused(' + engine.invoke + '));'
        elif engine.into and kind_text == 'result':
            line = '::stxlib_ic::adopt(holder, ' + engine.invoke + ');'
        elif engine.into:
            line = 'holder.replace(' + engine.invoke + ');'
        else:
            line = 'static_cast<void>(' + engine.invoke + ');'
        body.append('    step.line()' + ''.join(self.line_pieces(line, variables)) + '.end();')
        object_after: str = ''
        if engine.into and ('object' in used):
            object_after = '    Object& object = *holder.get();'
        if kind_text == 'status':
            body.append('    const stxlib::OperationStatus status = ' + invoke + ';')
            outcome: str = 'status'
        elif kind_text == 'result':
            body.append('    stxlib::OperationResult<' + result_type + '> made = ' + invoke + ';')
            outcome = 'made'
        elif kind_text == 'value':
            if engine.into:
                body.append('    ' + result_type + ' result = ' + invoke + ';')
            elif 'result' in used:
                body.append('    const ' + result_type + ' result = ' + invoke + ';')
            else:
                body.append('    static_cast<void>(' + invoke + ');')
            outcome = 'result'
        else:
            body.append('    static_cast<void>(' + invoke + ');')
            outcome = ''
        if mode == 'misuse':
            assert refusal is not None
            body.extend(self.refusal_check(refusal, outcome, kind_text, engine_for_refusal, label))
            self.lines.extend(body)
            self.lines.append('}')
            self.lines.append('')
            return
        fails: tuple[Failure, ...] = call.fails if call else ()
        unchanged: bool = all(failure.leaves == 'unchanged' for failure in fails)
        moves_index: str = str(self.states.index(call.moves)) + 'U' if call and call.moves else 'step.before.state'
        if kind_text in ('status', 'result'):
            body.append('    if (' + outcome + '.failed()) return step.failed(*' + outcome + '.error(), '
                        + self.kinds_mask(fails) + ', ' + ('true' if unchanged else 'false') + ', '
                        + moves_index + ');')
        if failed_when:
            body.append('    if (' + failed_when + ') return step.failed_by_value(' + ('true' if fails else 'false')
                        + ');')
        if engine.into:
            body.append('    if (holder.present()) teardown(*holder.get(), step.before.state);')
            if kind_text == 'result':
                body.append('    holder.replace(static_cast<stxlib::OperationResult<' + result_type
                            + '>&&>(made).take_value());')
            else:
                body.append('    holder.replace(static_cast<' + result_type + '&&>(result));')
            if object_after:
                body.append(object_after)
        elif kind_text == 'result' and 'result' in used:
            body.append('    const ' + result_type + '& result = *made.value();')
        for text in expects:
            body.append('    if (!(' + text + ')) return step.defect(::stxlib_ic::DefectKind::expectation, "'
                        + cpp_string(label + ': ' + text) + '");')
        if engine.into:
            body.append('    step.after.live = true;')
            ensured: set[str] = {effect.term for effect in engine.ensures}
            for index, term in enumerate(self.terms):
                if term not in ensured:
                    body.append('    step.observe_term(' + str(index) + 'U);')
        if engine.empties:
            body.append('    step.after.live = false;')
        body.extend(effects)
        state_line: str = self.state_after(call, engine)
        if state_line:
            body.append(state_line)
        body.append('    return ::stxlib_ic::Outcome::completed;')
        self.lines.extend(body)
        self.lines.append('}')
        self.lines.append('')

    def state_after(self, call: Call | None, engine: CallEngine) -> str:
        if engine.state_by is not None:
            draw: str = engine.state_by[0]
            indices: list[str] = [str(self.states.index(state)) + 'U' for state in engine.state_by[1]]
            return ('    { constexpr stxlib::u32 by[] = {' + ', '.join(indices) + '}; step.after.state = by['
                    'static_cast<stxlib::usize>(v_' + draw + ') % ' + str(len(indices)) + 'U]; }')
        if call is not None and call.moves:
            return '    step.after.state = ' + str(self.states.index(call.moves)) + 'U;'
        if engine.into and call is not None:
            returned: object = dict(call.returns).get('state')
            if isinstance(returned, str):
                return '    step.after.state = ' + str(self.states.index(returned)) + 'U;'
            if self.declaration.initial:
                return '    step.after.state = ' + str(self.states.index(self.declaration.initial)) + 'U;'
        return ''

    def requirement_lines(self, function: str, label: str, invoke: str, variables: dict[str, Variable],
                          engine: CallEngine, target: Requirement | None) -> list[str]:
        """A refusal that stops the process: the call runs in a child process when the engine forks, and is shown by
        its predicate otherwise."""
        context: str = function + '_context'
        members: list[str] = ['ObjectHolder* holder{nullptr};', 'stxlib::usize slot{0};']
        assignments: list[str] = ['    context.holder = &holder;', '    context.slot = step.slot;']
        unpack: list[str] = ['    ObjectHolder& holder = *context.holder;', '    static_cast<void>(holder);',
                             '    const stxlib::usize slot = context.slot;', '    static_cast<void>(slot);']
        name: str
        for name in variables:
            variable: Variable = variables[name]
            members.append(variable.cpp_type + ' v_' + name + '{};')
            assignments.append('    context.v_' + name + ' = v_' + name + ';')
            unpack.append('    const ' + variable.cpp_type + ' v_' + name + ' = context.v_' + name + ';')
            unpack.append('    static_cast<void>(v_' + name + ');')
        prelude: list[str] = ['struct ' + context + ' final {']
        prelude.extend('    ' + member for member in members)
        prelude.append('};')
        prelude.append('')
        prelude.append('inline void ' + function + '_call(void* const raw) noexcept {')
        prelude.append('    ' + context + '& context = *static_cast<' + context + '*>(raw);')
        prelude.extend(unpack)
        call_text: str = invoke.replace('step.slot', 'slot')
        if 'object' in CPP_IDENTIFIER.findall(call_text):
            prelude.append('    Object& object = *holder.get();')
        prelude.append('    static_cast<void>(' + call_text + ');')
        prelude.append('}')
        prelude.append('')
        # The context and the call go before the block.
        insert_at: int = len(self.lines) - 2
        self.lines[insert_at:insert_at] = prelude
        message: str = target.message if target is not None else ''
        lines: list[str] = ['    step.line().text("// stops the process as declared: ")'
                            + ''.join(self.line_pieces(engine.invoke, variables)) + '.end();',
                            '    ' + context + ' context{};']
        lines.extend(assignments)
        lines.append('    return step.requirement(&' + function + '_call, &context, "' + cpp_string(message) + '");')
        return lines

    # -- the whole file

    def call_engine(self, call: Call) -> CallEngine | None:
        """The call's engine table, or one made from its declaration when every argument is a declared
        parameter; None (with the reason kept) when the engine does not drive the call."""
        if call.engine is not None:
            if call.engine.skip:
                self.skipped.append((call.name, call.engine.skip))
                return None
            if call.engine.invoke:
                return call.engine
        names: list[str] = declared_parameters(call.declarations[0])
        parameters: dict[str, str] = dict(call.parameters)
        drawable: bool = all(name in parameters and (parameters[name] in UNSIGNED_TYPES
                                                     or parameters[name] in SIGNED_TYPES)
                             for name in names)
        if not drawable:
            self.skipped.append((call.name, 'arguments the engine does not draw (no engine.invoke)'))
            return None
        arguments: str = ', '.join(names)
        invoke: str
        if call.name.startswith('operator'):
            if call.name != 'operator[]':
                self.skipped.append((call.name, 'an operator the engine does not call'))
                return None
            invoke = 'object[' + arguments + ']'
        elif call.kind == 'member':
            invoke = 'object.' + call.name + '(' + arguments + ')'
        elif call.kind == 'static':
            invoke = 'Object::' + call.name + '(' + arguments + ')'
        elif call.kind == 'free':
            invoke = '::' + self.declaration.namespace + '::' + call.name + '(' + arguments + ')'
        else:
            self.skipped.append((call.name, 'a constructor'))
            return None
        into: bool = call.kind == 'static' and declaration_returns_object(declared_return(call.declarations[0]),
                                                                         self.declaration.component)
        base: CallEngine = call.engine if call.engine is not None else CallEngine(
            '', (), (), (), (), (), (), into, False, 'any' if into else default_needs(call), None, '', '', '', '')
        made: CallEngine = dataclasses.replace(base, invoke=invoke)
        return made

    def text(self) -> str:
        declaration: Declaration = self.declaration
        engine: EngineObject = self.engine
        namespace: str = self.engine_project.namespace + '::' + declaration.identifier
        header: list[str] = ['#pragma once', '']
        header.extend(comment_lines(
            'The block engine\'s blocks for ' + declaration.manifest_key() + ', generated by ' + self.project.generator
            + ' from ' + declaration.path + '; edit the declaration, not this file. One legal block per driven '
            'call, one misuse block per clause and per state a call refuses (the IC study\'s phase IC2; '
            'docs/ic_declarations.md in template_harness, section 8). A block draws its arguments inside the '
            'declared bounds (legal) or outside one clause (misuse), checks them with the generated predicates, '
            'calls, and says what the declaration predicts; the engine compares that with what the object '
            'reports.', '', self.project.column_limit))
        header.append('')
        includes: list[str] = [self.engine_project.include]
        includes.extend(engine.includes)
        if declaration.checks is not None:
            includes.append(include_path(declaration.checks.path, self.engine_project.include_roots))
        include: str
        for include in includes:
            header.append('#include "' + include + '"')
        header.append('')
        header.append('namespace ' + namespace + ' {')
        header.append('')
        header.append('using Object = ' + engine.object + ';')
        header.append('using ObjectHolder = ::stxlib_ic::Holder<Object>;')
        header.append('')
        header.append('inline constexpr stxlib::usize slot_count = ' + str(engine.slots) + ';')
        term_names: list[str] = self.terms or ['']
        header.append('inline constexpr const char* term_names[] = {' + ', '.join('"' + name + '"'
                                                                                  for name in term_names) + '};')
        reads: list[str] = [dict(engine.observe).get(name, '') for name in self.terms] or ['']
        header.append('inline constexpr const char* term_reads[] = {' + ', '.join('"' + cpp_string(text) + '"'
                                                                                  for text in reads) + '};')
        header.append('inline constexpr const char* state_names[] = {' + ', '.join('"' + state + '"'
                                                                                   for state in self.states) + '};')
        calls: list[str] = [call.identifier for call in declaration.calls] + [compound.name for compound in
                                                                               engine.compounds]
        if engine.make:
            calls.append('make')
        header.append('inline constexpr const char* call_names[] = {' + ', '.join('"' + name + '"' for name in calls)
                      + '};')
        header.append('')
        header.append('struct Slots final {')
        header.append('    ObjectHolder holders[slot_count]{};')
        header.append('};')
        header.append('')
        header.append('inline void observe(const Object& object, ::stxlib_ic::Model& model) noexcept {')
        header.append('    static_cast<void>(object);')
        header.append('    static_cast<void>(model);')
        index: int
        name: str
        for index, name in enumerate(self.terms):
            expression: str = dict(engine.observe).get(name, '')
            if expression:
                header.append('    model.terms[' + str(index) + '] = static_cast<stxlib::u64>(' + expression + ');')
        header.append('}')
        header.append('')
        header.append('inline bool state_agrees(const Object& object, const stxlib::u32 state) noexcept {')
        header.append('    static_cast<void>(object);')
        query: str = engine.state_query
        if not query and declaration.state_query:
            query = 'object.' + declaration.state_query + '()'
        answers: tuple[tuple[str, str], ...] = declaration.observes
        if engine.state_answers:
            answers = tuple((state, dict(engine.state_answers)[state]) for state in self.states)
        if query and answers:
            header.append('    switch (state) {')
            for index, (state, answer) in enumerate(answers):
                header.append('    case ' + str(index) + 'U:')
                header.append('        return (' + query + ') == (' + answer + ');')
            header.append('    default:')
            header.append('        return false;')
            header.append('    }')
        else:
            header.append('    static_cast<void>(state);')
            header.append('    return true;')
        header.append('}')
        header.append('')
        header.append('inline bool invariants_hold(const Object& object) noexcept {')
        header.append('    static_cast<void>(object);')
        if engine.invariants:
            header.append('    const bool result = ' + ' && '.join('(' + text + ')' for text in engine.invariants)
                          + ';')
            header.append('    return result;')
        else:
            header.append('    return true;')
        header.append('}')
        header.append('')
        header.append('inline const char* invariant_text() noexcept { return "' + cpp_string(
            ' && '.join(engine.invariants) or 'true') + '"; }')
        header.append('')
        header.append('inline void teardown(Object& object, const stxlib::u32 state) noexcept {')
        header.append('    static_cast<void>(object);')
        if engine.teardown:
            header.append('    switch (state) {')
            state: str
            statement: str
            for state, statement in engine.teardown:
                header.append('    case ' + str(self.states.index(state)) + 'U:')
                header.append('        ' + statement)
                header.append('        return;')
            header.append('    default:')
            header.append('        return;')
            header.append('    }')
        else:
            header.append('    static_cast<void>(state);')
        header.append('}')
        header.append('')
        self.lines = header
        all_mask: int = (1 << len(self.states)) - 1
        if engine.make:
            make_line: str = 'holder.emplace();' if engine.make == 'default' else 'holder.replace(' + engine.make + ');'
            self.lines.append('// A fresh object in the initial state.')
            self.lines.append('inline ::stxlib_ic::Outcome legal_make(::stxlib_ic::Step& step, ObjectHolder& holder) '
                              'noexcept {')
            self.lines.append('    step.line().holder(step.slot).text(".' + ('emplace()' if engine.make == 'default'
                                                                            else 'replace(' + cpp_string(engine.make)
                                                                            + ')') + ';").end();')
            self.lines.append('    if (holder.present()) teardown(*holder.get(), step.before.state);')
            self.lines.append('    ' + make_line)
            self.lines.append('    step.after.live = true;')
            initial_index: int = self.states.index(declaration.initial) if declaration.initial else 0
            self.lines.append('    step.after.state = ' + str(initial_index) + 'U;')
            for index in range(len(self.terms)):
                self.lines.append('    step.observe_term(' + str(index) + 'U);')
            self.lines.append('    return ::stxlib_ic::Outcome::completed;')
            self.lines.append('}')
            self.lines.append('')
            self.entries.append(('legal_make', 'make', 'legal', all_mask, 'any', len(calls) - 1, ''))
        call_index: int
        call: Call
        for call_index, call in enumerate(declaration.calls):
            engine_of_call: CallEngine | None = self.call_engine(call)
            if engine_of_call is None:
                continue
            kind_text: str = result_kind(call, engine_of_call)
            admitted: tuple[str, ...] = call.admitting or (self.states if declaration.states else ('any',))
            mask: int = 0
            for state in admitted:
                mask = mask | (1 << self.states.index(state))
            legal_name: str = 'legal_' + call.identifier
            self.block(legal_name, declaration.component + '::' + call.name + ', legal', call, engine_of_call,
                       kind_text, 'legal', None, None, '')
            self.entries.append((legal_name, call.identifier, 'legal', mask, engine_of_call.needs, call_index, ''))
            requirement: Requirement
            for requirement in call.requires:
                # One misuse block per variable the clause bounds (each drawn outside in turn), or one when
                # the clause bounds none (a clause on terms alone).
                chosen_names: list[str] = self.targets(call, engine_of_call, requirement) or ['']
                chosen_name: str
                for chosen_name in chosen_names:
                    function: str = 'misuse_' + call.identifier + '_' + requirement.check
                    label_text: str = declaration.component + '::' + call.name + ', misuse of ' + requirement.check
                    if len(chosen_names) > 1:
                        function = function + '_' + chosen_name
                        label_text = label_text + ' (' + chosen_name + ')'
                    self.block(function, label_text, call, engine_of_call, kind_text, 'misuse', requirement,
                               requirement.refuses, '', chosen_name)
                    self.entries.append((function, call.identifier, 'misuse', mask, engine_of_call.needs, call_index,
                                         requirement.refuses.kind))
            refusal: Refusal
            for state, refusal in call.outside:
                function = 'outside_' + call.identifier + '_' + state
                outside_engine: CallEngine = dataclasses.replace(engine_of_call, needs='live' if engine_of_call.needs
                                                                 in ('live', 'any') else engine_of_call.needs)
                self.block(function, declaration.component + '::' + call.name + ' in ' + state, call, outside_engine,
                           kind_text, 'misuse', None, refusal, state)
                self.entries.append((function, call.identifier, 'outside', 1 << self.states.index(state),
                                     outside_engine.needs, call_index, refusal.kind))
        compound: Compound
        for index, compound in enumerate(engine.compounds):
            function = 'compound_' + compound.name
            admitted = compound.admitting or self.states
            mask = 0
            for state in admitted:
                mask = mask | (1 << self.states.index(state))
            kind_text = 'none'
            if compound.engine.result_type in ('stxlib::OperationStatus', 'OperationStatus'):
                kind_text = 'status'
            elif compound.engine.result_type.startswith('stxlib::OperationResult<'):
                kind_text = 'result'
            elif compound.engine.result_type:
                kind_text = 'value'
            self.block(function, declaration.component + ', compound ' + compound.name, None, compound.engine,
                       kind_text, 'legal', None, None, '')
            self.entries.append((function, compound.name, compound.mode, mask, compound.needs,
                                 len(declaration.calls) + index, ''))
        self.lines.append('inline ::stxlib_ic::Outcome run(void* const storage, const stxlib::usize block, '
                          '::stxlib_ic::Step& step) noexcept {')
        self.lines.append('    Slots& slots = *static_cast<Slots*>(storage);')
        self.lines.append('    ObjectHolder& holder = slots.holders[step.slot];')
        self.lines.append('    switch (block) {')
        entry: tuple[str, str, str, int, str, int, str]
        for index, entry in enumerate(self.entries):
            self.lines.append('    case ' + str(index) + 'U:')
            self.lines.append('        return ' + entry[0] + '(step, holder);')
        self.lines.append('    default:')
        self.lines.append('        return ::stxlib_ic::Outcome::skipped;')
        self.lines.append('    }')
        self.lines.append('}')
        self.lines.append('')
        self.lines.append('inline constexpr ::stxlib_ic::BlockInfo blocks[] = {')
        for entry in self.entries:
            mode_text: str = {'legal': 'legal', 'misuse': 'misuse', 'outside': 'outside'}[entry[2]]
            refusal_text: str = {'': 'none', 'requirement': 'stops', 'failure': 'failure', 'null': 'value',
                                 'value': 'value'}[entry[6]]
            self.lines.append('    {"' + entry[0] + '", ' + str(entry[5]) + 'U, ::stxlib_ic::BlockMode::' + mode_text
                              + ', ' + hex(entry[3]) + 'U, ::stxlib_ic::Needs::' + entry[4]
                              + ', ::stxlib_ic::Refusal::' + refusal_text + '},')
        if not self.entries:
            self.lines.append('    {"none", 0U, ::stxlib_ic::BlockMode::legal, 0x0U, ::stxlib_ic::Needs::none, '
                              '::stxlib_ic::Refusal::none},')
        self.lines.append('};')
        self.lines.append('')
        self.lines.append('inline void* create() noexcept { return ::stxlib_ic::create_slots<Slots>(); }')
        self.lines.append('')
        self.lines.append('inline void destroy(void* const storage, const ::stxlib_ic::Model* const models) noexcept {')
        self.lines.append('    Slots& slots = *static_cast<Slots*>(storage);')
        self.lines.append('    for (stxlib::usize index = 0; index < slot_count; ++index) {')
        self.lines.append('        if (slots.holders[index].present()) teardown(*slots.holders[index].get(), '
                          'models[index].state);')
        self.lines.append('    }')
        self.lines.append('    ::stxlib_ic::destroy_slots<Slots>(storage);')
        self.lines.append('}')
        self.lines.append('')
        self.lines.append('inline bool read(void* const storage, const stxlib::usize slot, ::stxlib_ic::Model& model, '
                          'const stxlib::u32 state, bool& agrees, bool& whole) noexcept {')
        self.lines.append('    Slots& slots = *static_cast<Slots*>(storage);')
        self.lines.append('    if (!slots.holders[slot].present()) return false;')
        self.lines.append('    const Object& object = *slots.holders[slot].get();')
        self.lines.append('    observe(object, model);')
        self.lines.append('    agrees = state_agrees(object, state);')
        self.lines.append('    whole = invariants_hold(object);')
        self.lines.append('    return true;')
        self.lines.append('}')
        self.lines.append('')
        self.lines.append('inline constexpr ::stxlib_ic::Component component{')
        self.lines.append('    "' + declaration.manifest_key() + '",')
        self.lines.append('    "' + declaration.path + '",')
        self.lines.append('    "' + cpp_string(engine.object) + '",')
        self.lines.append('    slot_count,')
        self.lines.append('    ' + str(len(self.terms)) + 'U,')
        self.lines.append('    term_names,')
        self.lines.append('    term_reads,')
        self.lines.append('    ' + str(len(self.states)) + 'U,')
        self.lines.append('    state_names,')
        initial: int = self.states.index(declaration.initial) if declaration.initial else 0
        self.lines.append('    ' + str(initial) + 'U,')
        self.lines.append('    ' + str(len(calls)) + 'U,')
        self.lines.append('    call_names,')
        self.lines.append('    ' + str(len(self.entries)) + 'U,')
        self.lines.append('    blocks,')
        self.lines.append('    &create,')
        self.lines.append('    &destroy,')
        self.lines.append('    &run,')
        self.lines.append('    &read,')
        self.lines.append('    &invariant_text,')
        self.lines.append('};')
        self.lines.append('')
        self.lines.append('} // namespace ' + namespace)
        self.lines.append('')
        result: str = '\n'.join(self.lines)
        return result


def default_needs(call: Call) -> str:
    if call.kind == 'member':
        return 'live'
    return 'none'


def declaration_returns_object(returned: str, component: str) -> bool:
    inner: str = returned
    if inner.startswith('OperationResult<') and inner.endswith('>'):
        inner = inner[len('OperationResult<'):-1]
    result: bool = inner == component or inner.startswith(component + '<')
    return result


def include_path(path: str, roots: tuple[str, ...]) -> str:
    root: str
    for root in roots:
        if path.startswith(root + '/'):
            return path[len(root) + 1:]
    return path


def qualified_vocabulary(type_name: str) -> str:
    if type_name in VOCABULARY_TYPES or type_name in UNSIGNED_TYPES or type_name in SIGNED_TYPES:
        if type_name == 'bool':
            return 'bool'
        return 'stxlib::' + type_name
    return type_name


def cpp_string(text: str) -> str:
    result: str = text.replace('\\', '\\\\').replace('"', '\\"')
    return result


def names_of_full(node: Node, found: list[str]) -> None:
    """The clause's names, whole (`old.count` stays one name), in order of first appearance."""
    pending: list[Node] = [node]
    while pending:
        current: Node = pending.pop()
        if current.kind == 'name':
            if current.text not in found:
                found.append(current.text)
            continue
        pending.extend(reversed(current.items))


def top_conjuncts(node: Node) -> list[Node]:
    if node.kind == 'logic' and node.operator == 'and':
        result: list[Node] = []
        item: Node
        for item in node.items:
            result.extend(top_conjuncts(item))
        return result
    return [node]


@dataclasses.dataclass(frozen=True)
class EngineSummary:
    """What a component's blocks cover, for the gate."""
    component: str
    declaration: str
    clauses: int
    calls: int
    driven: int
    skipped: tuple[tuple[str, str], ...]
    legal_blocks: int
    misuse_blocks: int
    states: int
    edges: int


def engine_outputs(project: Project, declarations: list[Declaration]) -> tuple[dict[str, str], list[EngineSummary]]:
    """The engine's generated files: one per declaration with an [engine] table, and the registry."""
    outputs: dict[str, str] = {}
    summaries: list[EngineSummary] = []
    if project.engine is None:
        return outputs, summaries
    registered: list[Declaration] = []
    declaration: Declaration
    for declaration in declarations:
        clauses: int = sum(len(call.requires) + len(call.outside) for call in declaration.calls)
        if declaration.engine is None or declaration.engine.skip:
            reason: str = declaration.engine.skip if declaration.engine is not None else 'no [engine] table'
            summaries.append(EngineSummary(declaration.manifest_key(), declaration.path, clauses,
                                           len(declaration.calls), 0, (('*', reason),), 0, 0,
                                           len(declaration.states) or 1, 0))
            continue
        writer: BlockWriter = BlockWriter(project, declaration)
        outputs[project.engine.path + '/' + declaration.identifier + '.blocks.inc'] = writer.text()
        registered.append(declaration)
        legal: int = sum(1 for entry in writer.entries if entry[2] == 'legal')
        misuse: int = len(writer.entries) - legal
        states: int = len(declaration.states) or 1
        driven: int = len({entry[1] for entry in writer.entries if entry[0] != 'legal_make'
                           and entry[1] in {call.identifier for call in declaration.calls}})
        edges: int = 0
        call: Call
        for call in declaration.calls:
            if call.name in {name for name, _ in writer.skipped}:
                continue
            admitted: int = len(call.admitting) if call.admitting else states
            edges = edges + admitted
        summaries.append(EngineSummary(declaration.manifest_key(), declaration.path, clauses, len(declaration.calls),
                                       driven, tuple(writer.skipped), legal, misuse, states, edges))
    lines: list[str] = ['#pragma once', '']
    lines.extend(comment_lines('Every component the block engine drives, generated by ' + project.generator
                               + ' from the declarations with an [engine] table; edit them, not this file.', '',
                               project.column_limit))
    lines.append('')
    for declaration in registered:
        guard: str = declaration.engine.condition if declaration.engine is not None else ''
        if guard:
            lines.append('#if ' + guard)
        lines.append('#include "' + declaration.identifier + '.blocks.inc"')
        if guard:
            lines.append('#endif')
    lines.append('')
    lines.append('namespace ' + project.engine.namespace + ' {')
    lines.append('')
    lines.append('inline constexpr const ::stxlib_ic::Component* components[] = {')
    for declaration in registered:
        guard = declaration.engine.condition if declaration.engine is not None else ''
        if guard:
            lines.append('#if ' + guard)
        lines.append('    &' + declaration.identifier + '::component,')
        if guard:
            lines.append('#endif')
    lines.append('};')
    lines.append('')
    lines.append('} // namespace ' + project.engine.namespace)
    lines.append('')
    outputs[project.engine.path + '/components.inc'] = '\n'.join(lines)
    return outputs, summaries


# ---- the gate ---------------------------------------------------------------------------------------

def gate_text(project: Project, summaries: list[EngineSummary], obligations: dict[str, str]) -> str:
    """The gate's results file: per component what is declared and generated, what the stated run reached
    (project gate_run, written by the engine's runner), and the obligations' status."""
    run: dict[str, object] = {}
    if project.gate_run:
        run_path: Path = project.root / project.gate_run
        if run_path.is_file():
            loaded_run: object = json.loads(run_path.read_text(encoding='utf-8'))
            if isinstance(loaded_run, dict):
                run = loaded_run
    components: dict[str, object] = table_of(run.get('components', {}), 'the gate run') if run else {}
    lines: list[str] = ['# The IC gate', '']
    lines.extend(textwrap.wrap(
        'Generated by ' + project.generator + ' from the declarations, the engine\'s generated blocks, the proof '
        'obligations and the stated run (' + (project.gate_run or 'none named') + '); edit those, not this file. '
        'The gate of the IC study (notes, gui_forms branch, planning/stages/IC-integrity-constraints/STUDY.md, '
        'section 8): per component, the clauses declared, the blocks generated, the states and edges a run '
        'reached, the defects found and fixed, and the legal-mode false positives over a stated run. Results of '
        'the engine are tests, by the owner\'s definition; the obligations column counts deductions.', width=100))
    lines.append('')
    if run:
        lines.append('Run: ' + str(run.get('stated', 'unstated')) + '.')
    else:
        lines.append('Run: not run yet. The runner writes ' + (project.gate_run or 'the run file')
                     + ' (tests/ic/ic_run.cpp, `--gate`); until then the run columns read "not run".')
    lines.append('')
    lines.append('| Component | Clauses | Calls driven | Blocks (legal + misuse) | States reached | Edges reached '
                 '| Defects (found / fixed) | Legal false positives | Obligations (discharged / all) |')
    lines.append('|---|---|---|---|---|---|---|---|---|')
    summary: EngineSummary
    for summary in summaries:
        record: dict[str, object] = table_of(components.get(summary.component, {}), 'the gate run') \
            if summary.component in components else {}
        states: str = 'not run'
        edges: str = 'not run'
        defects: str = 'not run'
        false_positives: str = 'not run'
        if record:
            states = str(record.get('states_reached', 0)) + ' of ' + str(summary.states)
            edges = str(record.get('edges_reached', 0)) + ' of ' + str(summary.edges)
            defects = str(record.get('defects_found', 0)) + ' / ' + str(record.get('defects_fixed', 0))
            false_positives = str(record.get('legal_false_positives', 0))
        if summary.legal_blocks == 0:
            states = 'not driven'
            edges = 'not driven'
        discharged: int = 0
        total: int = 0
        text: str | None = obligations.get(summary.declaration)
        if text is not None:
            document: dict[str, object] = json.loads(text)
            items: object = document.get('obligations', [])
            if isinstance(items, list):
                total = len(items)
                discharged = sum(1 for item in items if isinstance(item, dict) and item.get('status') == 'discharged')
        lines.append('| ' + summary.component + ' | ' + str(summary.clauses) + ' | ' + str(summary.driven) + ' of '
                     + str(summary.calls) + ' | ' + str(summary.legal_blocks) + ' + ' + str(summary.misuse_blocks)
                     + ' | ' + states + ' | ' + edges + ' | ' + defects + ' | ' + false_positives + ' | '
                     + str(discharged) + ' / ' + str(total) + ' |')
    lines.append('')
    lines.append('## Calls the engine does not drive')
    lines.append('')
    any_skipped: bool = False
    for summary in summaries:
        name: str
        reason: str
        for name, reason in summary.skipped:
            any_skipped = True
            lines.append('- ' + summary.component + ' ' + (name if name != '*' else '(every call)') + ': ' + reason
                         + '.')
    if not any_skipped:
        lines.append('None.')
    lines.append('')
    if run:
        found: object = run.get('defects', [])
        lines.append('## Defects the run found')
        lines.append('')
        if isinstance(found, list) and found:
            item: object
            for item in found:
                if isinstance(item, dict):
                    lines.append('- ' + str(item.get('component', '?')) + ', ' + str(item.get('block', '?')) + ': '
                                 + str(item.get('what', '?')) + ' (' + str(item.get('status', 'open')) + ').')
        else:
            lines.append('None.')
        lines.append('')
    result: str = '\n'.join(lines)
    return result


# ---- commands --------------------------------------------------------------------------------

def loaded(project: Project) -> tuple[list[Declaration], list[str]]:
    declarations: list[Declaration] = []
    errors: list[str] = []
    paths: list[str] = declaration_paths(project)
    if not paths:
        errors.append('the project file\'s declarations match no file')
    relative: str
    for relative in paths:
        try:
            declarations.append(load_declaration(project.root, relative))
        except DeclarationError as error:
            errors.append(relative + ': ' + str(error))
    return declarations, errors


def run(command: str, config: Path, root: Path) -> int:
    try:
        project: Project = load_project(config, root)
    except (DeclarationError, OSError) as error:
        print('ERROR ' + str(config) + ': ' + str(error))
        print('SUMMARY error')
        return 2
    declarations: list[Declaration]
    errors: list[str]
    declarations, errors = loaded(project)
    outputs: dict[str, str] = {}
    unused: list[str] = []
    if not errors:
        try:
            outputs = outputs_of(project, declarations)
            unused = unused_predicates(project, declarations, outputs)
        except (DeclarationError, OSError) as error:
            errors.append(str(error))
    message: str
    for message in errors:
        print('ERROR ' + message)
    if errors:
        print('SUMMARY ' + str(len(errors)) + ' error(s)')
        return 2
    if command == 'list':
        declaration: Declaration
        for declaration in declarations:
            call: Call
            for call in declaration.calls:
                print(declaration.manifest_key() + ' ' + call.name + ' (' + str(len(call.requires)) + ' clause(s))')
        print('SUMMARY ' + str(len(declarations)) + ' declaration(s)')
        return 0
    stale: list[str] = []
    path: str
    text: str
    for path, text in sorted(outputs.items()):
        target: Path = root / path
        current: str | None = target.read_text(encoding='utf-8') if target.is_file() else None
        if current == text:
            continue
        if command == 'generate':
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding='utf-8')
            print('WROTE ' + path)
        else:
            stale.append(path)
            print('STALE ' + path)
    finding: str
    for finding in unused:
        print('UNUSED ' + finding)
    if stale or unused:
        print('SUMMARY ' + str(len(stale)) + ' stale, ' + str(len(unused)) + ' unused')
        return 1
    verb: str = 'written or current' if command == 'generate' else 'current'
    print('SUMMARY ' + str(len(declarations)) + ' declaration(s), ' + str(len(outputs)) + ' output(s) ' + verb)
    return 0


def main(arguments: list[str]) -> int:
    parser: argparse.ArgumentParser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('command', choices=('generate', 'check', 'list'))
    parser.add_argument('--config', required=True, type=Path, help='the project file (docs/ic_declarations.md)')
    parser.add_argument('--root', type=Path, default=Path('.'), help='the repository root (default: .)')
    options: argparse.Namespace = parser.parse_args(arguments)
    status: int = run(options.command, options.config, options.root.resolve())
    return status


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
