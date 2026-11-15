"""Headroom parsing from canned vm_stat / memory_pressure / loadavg / meminfo outputs."""

import unittest

from helpers import sushirice

VM_STAT = """Mach Virtual Memory Statistics: (page size of 16384 bytes)
Pages free:                                     3632.
Pages active:                                 133584.
Pages inactive:                               129250.
Pages speculative:                              2751.
Pages throttled:                                   0.
Pages wired down:                             137523.
Pages purgeable:                                5798.
"Translation faults":                     9790730983.
Pages copy-on-write:                      1344546705.
File-backed pages:                             91393.
Anonymous pages:                              174192.
Pages stored in compressor:                   703819.
Pages occupied by compressor:                  87527.
Swapins:                                    22194801.
Swapouts:                                   26152996.
"""

MEMORY_PRESSURE_Q = """The system has 8589934592 (524288 pages with a page size of 16384).
System-wide memory free percentage: 54%
"""

MEMINFO = """MemTotal:       16303428 kB
MemFree:          812344 kB
MemAvailable:    4075857 kB
Buffers:          123456 kB
Cached:          2345678 kB
"""

PSI_CALM = """some avg10=0.00 avg60=0.10 avg300=0.05 total=123
full avg10=0.00 avg60=0.00 avg300=0.00 total=45
"""
PSI_WARN = "some avg10=25.00 avg60=10.00 avg300=2.00 total=1\nfull avg10=1.00 avg60=0 avg300=0 total=1\n"
PSI_CRITICAL = "some avg10=30.00 avg60=10.00 avg300=2.00 total=1\nfull avg10=12.50 avg60=0 avg300=0 total=1\n"

PS_OUTPUT = """    1     0     1     0 Ss    7-06:21:10 /sbin/launchd
  980   900   900   501 SN         00:03 /var/folders/x/T/tmp.t49Z/fake-clang
  981   900   900   501 R       01:02:03 /opt/homebrew/opt/llvm/bin/clang++
  982     1   982   502 R          00:10 /usr/bin/clang
  983     1   983   501 T          00:05 /Applications/Some App.app/Contents/MacOS/Some App
  984   981   900   501 R          00:01 clang-18
"""


class ParseTests(unittest.TestCase):
    def test_sysctl_loadavg(self):
        self.assertAlmostEqual(sushirice.parse_loadavg_sysctl("{ 3.18 3.41 3.48 }\n"), 3.18)
        self.assertIsNone(sushirice.parse_loadavg_sysctl(""))

    def test_proc_loadavg(self):
        self.assertAlmostEqual(sushirice.parse_proc_loadavg("7.52 6.10 5.00 3/812 12345\n"), 7.52)
        self.assertIsNone(sushirice.parse_proc_loadavg(None))

    def test_memory_pressure_q(self):
        self.assertEqual(sushirice.parse_memory_pressure(MEMORY_PRESSURE_Q), 54)
        self.assertIsNone(sushirice.parse_memory_pressure("garbage"))

    def test_vm_stat_with_total(self):
        # (3632 + 129250 + 2751 + 5798) / 524288 pages = 27%
        self.assertEqual(sushirice.parse_vm_stat(VM_STAT, 8589934592), 27)

    def test_vm_stat_without_total(self):
        value = sushirice.parse_vm_stat(VM_STAT)
        self.assertTrue(20 <= value <= 40, value)
        self.assertIsNone(sushirice.parse_vm_stat(""))

    def test_pressure_level_sysctl(self):
        self.assertEqual(sushirice.parse_pressure_level_sysctl("kern.memorystatus_vm_pressure_level: 1"), "normal")
        self.assertEqual(sushirice.parse_pressure_level_sysctl("kern.memorystatus_vm_pressure_level: 2"), "warn")
        self.assertEqual(sushirice.parse_pressure_level_sysctl("4\n"), "critical")
        self.assertIsNone(sushirice.parse_pressure_level_sysctl(""))

    def test_meminfo(self):
        self.assertEqual(sushirice.parse_meminfo(MEMINFO), 25)
        self.assertIsNone(sushirice.parse_meminfo(""))

    def test_psi(self):
        self.assertEqual(sushirice.parse_psi(PSI_CALM), "normal")
        self.assertEqual(sushirice.parse_psi(PSI_WARN), "warn")
        self.assertEqual(sushirice.parse_psi(PSI_CRITICAL), "critical")
        self.assertIsNone(sushirice.parse_psi(None))

    def test_etime(self):
        self.assertEqual(sushirice.parse_etime("00:03"), 3)
        self.assertEqual(sushirice.parse_etime("01:02:03"), 3723)
        self.assertEqual(sushirice.parse_etime("7-06:21:10"), 7 * 86400 + 6 * 3600 + 21 * 60 + 10)

    def test_ps_and_compiler_matching(self):
        processes = sushirice.parse_ps(PS_OUTPUT)
        self.assertEqual(len(processes), 6)
        self.assertEqual(processes[4]["name"], "Some App")
        self.assertEqual((processes[5]["ppid"], processes[5]["pgid"]), (981, 900))
        config = dict(sushirice.DEFAULT_CONFIG)
        names = sorted(p["name"] for p in sushirice.compiler_processes(processes, config))
        self.assertEqual(names, ["clang", "clang++", "clang-18"])
        mine = sushirice.compiler_processes(processes, config, uid=501)
        self.assertEqual(sorted(p["pid"] for p in mine), [981, 984])
        config["compiler_names"] = ["fake-clang"]
        self.assertEqual([p["pid"] for p in sushirice.compiler_processes(processes, config)], [980])

    def test_tasklist(self):
        text = '"cl.exe","4321","Console","1","120,000 K"\n"link.exe","4322","Console","1","9,000 K"\n'
        processes = sushirice.parse_tasklist(text)
        self.assertEqual([p["pid"] for p in processes], [4321, 4322])
        self.assertEqual(len(sushirice.compiler_processes(processes, sushirice.DEFAULT_CONFIG)), 2)


class AllowTests(unittest.TestCase):
    def setUp(self):
        self.config = sushirice.merge(sushirice.json.loads(sushirice.json.dumps(sushirice.DEFAULT_CONFIG)), {})

    def headroom(self, **values):
        base = {"cores": 10, "load": 2.0, "free_percent": 50, "pressure": "normal", "compilers": 0}
        base.update(values)
        return base

    def test_calm_machine_allows(self):
        self.assertEqual(sushirice.headroom_allows(self.headroom(), self.config, False), (True, "ok"))

    def test_load_blocks_normal_not_critical(self):
        allowed, reason = sushirice.headroom_allows(self.headroom(load=9.5), self.config, False)
        self.assertFalse(allowed)
        self.assertIn("load", reason)
        self.assertTrue(sushirice.headroom_allows(self.headroom(load=9.5), self.config, True)[0])

    def test_memory_blocks_even_critical(self):
        for values in ({"pressure": "critical"}, {"free_percent": 5}):
            self.assertFalse(sushirice.headroom_allows(self.headroom(**values), self.config, False)[0])
            self.assertFalse(sushirice.headroom_allows(self.headroom(**values), self.config, True)[0])

    def test_warn_pressure_allowed_by_default(self):
        self.assertTrue(sushirice.headroom_allows(self.headroom(pressure="warn"), self.config, False)[0])

    def test_compiler_count_blocks(self):
        allowed, reason = sushirice.headroom_allows(self.headroom(compilers=10), self.config, False)
        self.assertFalse(allowed)
        self.assertIn("compilers", reason)

    def test_unknown_readings_allow(self):
        headroom = {"cores": 4, "load": None, "free_percent": None, "pressure": None, "compilers": 0}
        self.assertTrue(sushirice.headroom_allows(headroom, self.config, False)[0])

    def test_disabled(self):
        self.config["headroom"]["enabled"] = False
        self.assertTrue(sushirice.headroom_allows(self.headroom(load=99, pressure="critical"), self.config, False)[0])


if __name__ == "__main__":
    unittest.main()
