"""No two project files under projects/ produce names that can collide.

Artifacts, Actions caches, release assets, environment variables and build
trees share one namespace when two products meet: one repository running
another product's tools, or one workspace holding both. A name collides
when it equals another project's or begins with it: GUI.Forms' artifact
prefix gui- would find another product's gui-tree-... artifacts as its own
tree receipts. docs/adopting.md section 2 gives the rules this test holds.

The Windows toolchain is the one name products may share. A project that
names another's toolchain repository and asset prefix exactly reuses that
published toolchain (the example app builds with GUI.Forms'), so equal
(repository, prefix) pairs pass; overlapping but unequal prefixes in one
toolchain repository do not.
"""
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
import unittest

import harness_testing
import project

PROJECTS: Path = harness_testing.REPOSITORY / 'projects'


@dataclass(frozen=True)
class NamedPrefix:
    """One project's value for one kind of name, with the separator its derived names add."""
    project: str
    kind: str
    value: str
    # Appended before comparing: env_prefix names are <prefix>_<NAME>, build
    # trees <build_name>[-sanitize]; the CI prefixes already end with '-'.
    separator: str


def overlapping(first: str, second: str) -> bool:
    """Whether either name equals the other or begins with it."""
    result: bool = first.startswith(second) or second.startswith(first)
    return result


def project_prefixes(product: project.Project) -> list[NamedPrefix]:
    """The names of one project that must not overlap another project's names of the same kind."""
    prefixes: list[NamedPrefix] = [
        NamedPrefix(product.name, 'project.name', product.name, ''),
        NamedPrefix(product.name, 'project.env_prefix', product.env_prefix, '_'),
        NamedPrefix(product.name, 'project.build_name', product.build_name, '-'),
        NamedPrefix(product.name, 'ci.artifact_prefix', product.artifact_prefix, ''),
        NamedPrefix(product.name, 'ci.cache_asset_prefix', product.cache_asset_prefix, ''),
    ]
    return prefixes


def prefix_collisions(products: list[project.Project]) -> list[str]:
    """Every pair of projects whose names of one kind overlap, as readable lines (empty when none do)."""
    findings: list[str] = []
    first_index: int
    for first_index in range(len(products)):
        second_index: int
        for second_index in range(first_index + 1, len(products)):
            first: project.Project = products[first_index]
            second: project.Project = products[second_index]
            first_names: list[NamedPrefix] = project_prefixes(first)
            second_names: list[NamedPrefix] = project_prefixes(second)
            position: int
            for position in range(len(first_names)):
                left: NamedPrefix = first_names[position]
                right: NamedPrefix = second_names[position]
                if overlapping(left.value + left.separator, right.value + right.separator):
                    findings.append(left.kind + ': ' + left.project + ' ' + repr(left.value) + ' overlaps '
                                    + right.project + ' ' + repr(right.value))
            same_toolchain: bool = (first.toolchain_repository == second.toolchain_repository
                                    and first.toolchain_asset_prefix == second.toolchain_asset_prefix)
            if (first.toolchain_repository == second.toolchain_repository and not same_toolchain
                    and overlapping(first.toolchain_asset_prefix, second.toolchain_asset_prefix)):
                findings.append('toolchain.asset_prefix in ' + first.toolchain_repository + ': ' + first.name + ' '
                                + repr(first.toolchain_asset_prefix) + ' overlaps ' + second.name + ' '
                                + repr(second.toolchain_asset_prefix))
    return findings


def every_project() -> list[project.Project]:
    paths: list[Path] = sorted(PROJECTS.glob('*.toml'))
    products: list[project.Project] = []
    path: Path
    for path in paths:
        products.append(project.load(path))
    return products


class ProjectPrefixTests(unittest.TestCase):
    def test_no_two_project_files_collide(self) -> None:
        products: list[project.Project] = every_project()
        names: list[str] = []
        product: project.Project
        for product in products:
            names.append(product.name)
        self.assertIn('gui_forms', names)
        self.assertEqual(prefix_collisions(products), [])

    def test_a_prefix_that_begins_with_another_is_found(self) -> None:
        reference: project.Project = project.load(harness_testing.REFERENCE_PROJECT)
        # gui-tree-: its artifacts would read as GUI.Forms' tree receipts.
        other: project.Project = self.variant(reference, name='other', env_prefix='OTHER', build_name='other',
                                              artifact_prefix='gui-tree-', cache_asset_prefix='other-cache-',
                                              toolchain_asset_prefix='other-toolchain-')
        findings: list[str] = prefix_collisions([reference, other])
        self.assertEqual(len(findings), 1, findings)
        self.assertTrue(findings[0].startswith('ci.artifact_prefix: '), findings)

    def test_separators_keep_distinct_names_apart(self) -> None:
        reference: project.Project = project.load(harness_testing.REFERENCE_PROJECT)
        # GUI_FORMSX_* and gui-formsx do not overlap GUI_FORMS_* and gui-forms[-...];
        # GUI_FORMS_X_* and gui-forms-x would.
        apart: project.Project = self.variant(reference, name='other', env_prefix='GUI_FORMSX', build_name='gui-formsx',
                                              artifact_prefix='other-', cache_asset_prefix='other-cache-',
                                              toolchain_asset_prefix='other-toolchain-')
        self.assertEqual(prefix_collisions([reference, apart]), [])
        nested: project.Project = self.variant(reference, name='other', env_prefix='GUI_FORMS_X',
                                               build_name='gui-forms-x', artifact_prefix='other-',
                                               cache_asset_prefix='other-cache-',
                                               toolchain_asset_prefix='other-toolchain-')
        kinds: list[str] = []
        finding: str
        for finding in prefix_collisions([reference, nested]):
            kinds.append(finding.partition(':')[0])
        self.assertEqual(kinds, ['project.env_prefix', 'project.build_name'])

    def test_a_shared_toolchain_passes_and_an_overlapping_one_does_not(self) -> None:
        reference: project.Project = project.load(harness_testing.REFERENCE_PROJECT)
        sharing: project.Project = self.variant(reference, name='other', env_prefix='OTHER', build_name='other',
                                                artifact_prefix='other-', cache_asset_prefix='other-cache-',
                                                toolchain_asset_prefix=reference.toolchain_asset_prefix)
        self.assertEqual(prefix_collisions([reference, sharing]), [])
        overlapping_toolchain: project.Project = self.variant(
            reference, name='other', env_prefix='OTHER', build_name='other', artifact_prefix='other-',
            cache_asset_prefix='other-cache-', toolchain_asset_prefix=reference.toolchain_asset_prefix + 'x-')
        findings: list[str] = prefix_collisions([reference, overlapping_toolchain])
        self.assertEqual(len(findings), 1, findings)
        self.assertTrue(findings[0].startswith('toolchain.asset_prefix in '), findings)

    def variant(self, base: project.Project, name: str, env_prefix: str, build_name: str, artifact_prefix: str,
                cache_asset_prefix: str, toolchain_asset_prefix: str) -> project.Project:
        """The reference project under other names, as a second product's file would give."""
        product: project.Project = project.Project(
            path=base.path, name=name, repository=base.repository, source_dir=base.source_dir,
            build_name=build_name, env_prefix=env_prefix, artifact_prefix=artifact_prefix,
            cache_asset_prefix=cache_asset_prefix, native_workflow=base.native_workflow,
            native_job=base.native_job, toolchain_repository=base.toolchain_repository,
            toolchain_asset_prefix=toolchain_asset_prefix, pinned_cache_revisions=base.pinned_cache_revisions,
            test_areas_file=base.test_areas_file, test_name_prefixes=base.test_name_prefixes,
            linux_packages=base.linux_packages, first_party_dirs=base.first_party_dirs,
            vocabulary_headers=base.vocabulary_headers, state_machine_areas=base.state_machine_areas,
            full_tree_triggers=base.full_tree_triggers, configure_variable_names=base.configure_variable_names,
            cache_profile=base.cache_profile, native_options=base.native_options,
            platform_native_options=base.platform_native_options, checkpoint=base.checkpoint,
            bootstrap=base.bootstrap)
        return product


if __name__ == '__main__':
    unittest.main()
