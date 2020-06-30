try:
    import unittest2 as unittest
except ImportError:
    import unittest

from rhsmlib.facts import pkg_arches
from test.fixture import OPEN_FUNCTION
from mock import patch


class TestSupportedArchesCollector(unittest.TestCase):

    def setUp(self):
        self.collector = pkg_arches.SupportedArchesCollector()

    @patch("os.path.exists")
    @patch(OPEN_FUNCTION)
    @patch('subprocess.check_output')
    def test_single_arch_on_ubuntu(self, MockCheckOutput, MockOpen, MockExists):

        def single(*args, **kwargs):
            if args[0] == 'dpkg --print-architecture':
                return 'amd64\n'
            return ''

        MockExists.side_effect = [False, True]
        MockOpen.return_value.readline.return_value = "Debian release 18.04.01 (Bionic Beaver)"
        MockCheckOutput.side_effect = single
        fact = self.collector.get_all()
        self.assertEqual(fact["supported_architectures"], 'amd64')

    @patch("os.path.exists")
    @patch(OPEN_FUNCTION)
    @patch('subprocess.check_output')
    def test_multi_arch_on_ubuntu(self, MockCheckOutput, MockOpen, MockExists):

        def multi(*args, **kwargs):
            if args[0] == 'dpkg --print-architecture':
                return 'amd64\n'
            elif args[0] == 'dpkg --print-foreign-architectures':
                return 'i386\n'
            return ''

        MockExists.side_effect = [False, True]
        MockOpen.return_value.readline.return_value = "Debian release 18.04.01 (Bionic Beaver)"
        MockCheckOutput.side_effect = multi
        fact = self.collector.get_all()
        self.assertEqual(fact["supported_architectures"], 'amd64,i386')

    @patch("os.path.exists")
    @patch(OPEN_FUNCTION)
    def test_none_arch_on_redhat(self, MockOpen, MockExists):

        MockExists.side_effect = [False, True]
        MockOpen.return_value.readline.return_value = "Redhat release 8 (EL8)"
        fact = self.collector.get_all()
        self.assertEqual(fact, {})
