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
- fuzz block stubs (JSON) for the block fuzzer (the IC study's phase IC2);
- proof obligations (JSON), open until a deduction discharges them.

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
    {'schema', 'declarations', 'manifest_column', 'blocks', 'obligations', 'generator', 'column_limit'})
DECLARATION_KEYS: frozenset[str] = frozenset(
    {'schema', 'component', 'namespace', 'header', 'anchor', 'keys', 'thread', 'states', 'initial',
     'state_query', 'identity', 'terms', 'constants', 'checks', 'call', 'obligation'})
KEY_KEYS: frozenset[str] = frozenset({'manifest', 'abi', 'registry', 'surface'})
STATE_QUERY_KEYS: frozenset[str] = frozenset({'call', 'observes'})
IDENTITY_KEYS: frozenset[str] = frozenset({'kind', 'stale'})
CONSTANT_KEYS: frozenset[str] = frozenset({'value', 'cpp', 'type'})
CHECKS_KEYS: frozenset[str] = frozenset({'path', 'namespace', 'includes', 'used_in'})
CALL_KEYS: frozenset[str] = frozenset(
    {'name', 'id', 'declarations', 'kind', 'thread', 'in', 'outside', 'moves', 'parameters', 'requires',
     'fails', 'returns', 'transfers'})
REQUIRE_KEYS: frozenset[str] = frozenset({'holds', 'refuses', 'message', 'check', 'leaves'})
FAIL_KEYS: frozenset[str] = frozenset({'failure', 'when', 'leaves'})
RETURN_KEYS: frozenset[str] = frozenset({'type', 'owns', 'state', 'borrows', 'until', 'revokes'})
TRANSFER_KEYS: frozenset[str] = frozenset({'what', 'from', 'to'})
OBLIGATION_KEYS: frozenset[str] = frozenset({'id', 'statement', 'method', 'premises'})


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
    call: Call = Call(name, identifier, declarations, kind, thread, admitting, tuple(outside), moves,
                      tuple(parameters), tuple(requirements), tuple(fails), tuple(returns_table.items()),
                      tuple(transfers))
    return call


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
                                      texts_of(obligation_table, 'premises', obligation_where)))
    declaration: Declaration = Declaration(relative, identifier, text, component, namespace, header, anchor,
                                           tuple(keys), thread, states, initial, state_query, tuple(observes),
                                           identity_kind, identity_stale, tuple(terms), tuple(constants), checks,
                                           tuple(calls), tuple(obligations))
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
                               text_of(raw, 'generator', 'the project file', 'harness/ic_declarations.py'), limit)
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
            parameters: list[str] = ['const ' + call_types[name] + ' ' + name for name in requirement.names
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
    if len(single) <= limit or clause.kind != 'logic':
        return [single]
    joiner: str = '&& ' if clause.operator == 'and' else '|| '
    operands: list[str] = [cpp_of(item) for item in clause.items]
    lines: list[str] = [first + operands[0]]
    operand: str
    for operand in operands[1:]:
        lines.append(' ' * (len(first) - len(joiner)) + joiner + operand)
    lines[-1] = lines[-1] + ';'
    return lines


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
            if requirement.refuses.kind != 'requirement' and requirement.leaves == 'unchanged':
                obligations.append(open_obligation(base + '.refusal.' + requirement.check, 'refusal-leaves',
                                                   'Unless ' + requirement.holds + ', ' + call.name
                                                   + ' changes nothing observable.', ''))
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
            obligations_text(project, declaration)
    outputs[project.manifest_column] = column_text(project, declarations)
    return outputs


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
