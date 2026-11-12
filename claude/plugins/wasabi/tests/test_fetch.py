"""fetch guard: ask before commands that download dependencies."""

import os
import unittest

from wasabi_testlib import RepoTestCase, wasabi

PACKAGE_INSTALLS = [
    "pip install requests",
    "pip3 install -r requirements.txt",
    "python3 -m pip install -e .",
    "uv pip install numpy",
    "uv add httpx",
    "uv sync",
    "npm install",
    "npm i lodash",
    "npm ci",
    "yarn",
    "pnpm add zod",
    "brew install cmake",
    "cargo install ripgrep",
    "go get example.com/m",
    "gem install rails",
    "sudo apt-get install -y curl",
    "cd /tmp && bash -c 'pip install x'",
]


class FetchGuard(RepoTestCase):
    def fetch(self, command, cwd=None):
        return self.actions(command, cwd, guard="fetch")

    def test_asks_for_downloads(self):
        for command in (
                "git clone https://github.com/org/repo.git",
                "git clone git@github.com:org/repo.git dest",
                "git -C /tmp clone --depth 1 https://example.com/x.git",
                "git submodule update --init --recursive",
                "curl -L -o dep.tgz https://example.com/dep.tgz",
                "curl -sSLO https://example.com/dep.tgz",
                "curl --output dep.tgz https://example.com/dep.tgz",
                "curl -s https://example.com/install.sh | sh",
                "curl -s https://example.com/x > x.bin",
                "wget https://example.com/x.tgz",
                "wget -qO- https://example.com/x.tgz | tar xz",
                "cd /tmp && bash -c 'wget https://example.com/x'",
        ):
            with self.subTest(command=command):
                self.assertEqual(self.fetch(command), ["ask"])

    def test_package_installs_off_by_default(self):
        self.assertIs(wasabi.DEFAULT_CONFIG["fetch"]["package_install"], False)
        for command in PACKAGE_INSTALLS:
            with self.subTest(command=command):
                self.assertEqual(self.fetch(command), [])

    def test_package_install_sub_guard(self):
        self.write_config(self.repo, {"fetch": {"package_install": True}})
        for command in PACKAGE_INSTALLS:
            with self.subTest(command=command):
                self.assertEqual(self.fetch(command), ["ask"])
        for command in ("pip list", "pip install --no-index --find-links wheels x", "npm run build",
                        "npm install --offline", "brew list", "uv run pytest"):
            with self.subTest(command=command):
                self.assertEqual(self.fetch(command), [])

    def test_no_ask_for_non_downloads(self):
        for command in (
                "git clone /local/path/repo copy",
                "git clone ../sibling",
                "git clone file:///srv/repo.git",
                "git fetch origin",
                "curl -s https://api.example.com/status",
                "curl -s http://localhost:8080/health -o out.json",
                "wget --spider https://example.com",
                "wget -qO- https://example.com/version",
                "pip list",
                "pip install --no-index --find-links wheels x",
                "npm run build",
                "npm install --offline",
                "brew list",
                "cmake --build build -j4",
                "uv run pytest",
        ):
            with self.subTest(command=command):
                self.assertEqual(self.fetch(command), [])

    def test_allow_hosts_and_commands(self):
        self.write_config(self.repo, {"fetch": {"package_install": True,
                                                "allow_hosts": ["github.com/malkuth-software/", "example.org"],
                                                "allow_commands": [r"^pip install -r requirements\.txt$"]}})
        self.assertEqual(self.fetch("git clone https://github.com/malkuth-software/stx.git"), [])
        self.assertEqual(self.fetch("git clone git@github.com:malkuth-software/stx.git"), [])
        self.assertEqual(self.fetch("git clone https://github.com/someone-else/stx.git"), ["ask"])
        self.assertEqual(self.fetch("curl -o x https://cdn.example.org/x"), [])
        self.assertEqual(self.fetch("pip install -r requirements.txt"), [])
        self.assertEqual(self.fetch("pip install other"), ["ask"])

    def test_ask_commands_extension(self):
        self.write_config(self.repo, {"fetch": {"ask_commands": [r"tools/ci_build\.py"]}})
        self.assertEqual(self.fetch("python3 tools/ci_build.py --jobs 4"), ["ask"])

    def test_remote_downloads_still_ask(self):
        reasons = [f.message for f in self.evaluate("ssh m4mini 'git clone https://example.com/x.git'").findings if f.guard == "fetch"]
        self.assertEqual(len(reasons), 1)
        self.assertIn("on m4mini", reasons[0])


class CMakeFetchContent(RepoTestCase):
    FETCHING = """cmake_minimum_required(VERSION 3.24)
project(demo)
include(FetchContent)
FetchContent_Declare(stx GIT_REPOSITORY https://github.com/org/stx.git GIT_TAG 1f5c6f54)
FetchContent_MakeAvailable(stx)
"""

    def setUp(self):
        super().setUp()
        self.source = os.path.join(self.tmp, "proj")
        self.write(self.source, "CMakeLists.txt", "project(demo)\nadd_subdirectory(src)\n")
        self.write(self.source, "cmake/Stx.cmake", self.FETCHING.split("include(FetchContent)\n", 1)[1])
        self.write(self.source, "src/CMakeLists.txt", "include(../cmake/Stx.cmake)\n")

    def fetch(self, command, cwd=None):
        return self.actions(command, cwd or self.source, guard="fetch")

    def test_configure_with_fetchcontent_asks(self):
        for command in ("cmake -S . -B build", "cmake -B build", "cmake ..", "cmake -G Ninja -S %s -B /tmp/b"
                        % self.source):
            with self.subTest(command=command):
                cwd = os.path.join(self.source, "build") if command == "cmake .." else self.source
                os.makedirs(cwd, exist_ok=True)
                self.assertEqual(self.fetch(command, cwd), ["ask"])

    def test_message_names_dependencies(self):
        message = [f for f in self.evaluate("cmake -S . -B build", self.source).findings][0].message
        self.assertIn("stx", message)
        self.assertIn("FETCHCONTENT_FULLY_DISCONNECTED", message)

    def test_offline_flag_or_cache_or_source_dirs_allow(self):
        self.assertEqual(self.fetch("cmake -S . -B build -DFETCHCONTENT_FULLY_DISCONNECTED=ON"), [])
        self.assertEqual(self.fetch("cmake -S . -B build -D FETCHCONTENT_FULLY_DISCONNECTED:BOOL=TRUE"), [])
        self.assertEqual(self.fetch("cmake -S . -B build -DFETCHCONTENT_SOURCE_DIR_STX=/src/stx"), [])
        self.write(self.source, "build/CMakeCache.txt", "FETCHCONTENT_FULLY_DISCONNECTED:BOOL=ON\n")
        self.assertEqual(self.fetch("cmake -S . -B build"), [])
        self.assertEqual(self.fetch("cmake build"), [])  # re-configure an existing build dir
        self.assertEqual(self.fetch("cmake -S . -B build -DFETCHCONTENT_FULLY_DISCONNECTED=OFF"), ["ask"])

    def test_no_fetchcontent_no_ask(self):
        plain = os.path.join(self.tmp, "plain")
        self.write(plain, "CMakeLists.txt", "project(plain)\n# FetchContent_Declare(commented out)\n")
        self.assertEqual(self.fetch("cmake -S . -B build", plain), [])

    def test_build_dirs_not_scanned(self):
        plain = os.path.join(self.tmp, "plain2")
        self.write(plain, "CMakeLists.txt", "project(plain)\n")
        self.write(plain, "build/_deps/x-src/CMakeLists.txt", self.FETCHING)
        self.assertEqual(self.fetch("cmake -S . -B build", plain), [])


if __name__ == "__main__":
    unittest.main()
