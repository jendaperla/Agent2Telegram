"""Case expansion must preserve independent tests under both test runners."""
import unittest

from tests.cases import cases, expand_cases


class CaseExpansionTests(unittest.TestCase):
    def test_cartesian_product_is_collected_and_executed_independently(self):
        seen = []

        @expand_cases
        class Example(unittest.TestCase):
            @cases(left=[1, 2], right=['a', 'b', 'c'])
            def test_pair(self, left, right):
                seen.append((left, right))

        suite = unittest.defaultTestLoader.loadTestsFromTestCase(Example)
        self.assertEqual(suite.countTestCases(), 6)
        result = unittest.TestResult()
        suite.run(result)
        self.assertTrue(result.wasSuccessful())
        self.assertEqual(result.testsRun, 6)
        self.assertEqual(seen, [(1, 'a'), (1, 'b'), (1, 'c'), (2, 'a'), (2, 'b'), (2, 'c')])

    def test_failed_case_is_visible_without_skipping_other_combinations(self):
        @expand_cases
        class Example(unittest.TestCase):
            @cases(value=[1, 2, 3])
            def test_value(self, value):
                self.assertNotEqual(value, 2)

        result = unittest.TestResult()
        unittest.defaultTestLoader.loadTestsFromTestCase(Example).run(result)
        self.assertEqual(result.testsRun, 3)
        self.assertEqual(len(result.failures), 1)
        self.assertFalse(result.errors)

    def test_empty_cases_are_an_error_not_silent_loss_of_coverage(self):
        class Example(unittest.TestCase):
            @cases(value=[])
            def test_value(self, value):
                pass

        with self.assertRaisesRegex(ValueError, 'empty test cases'):
            expand_cases(Example)

    def test_case_names_cannot_overwrite_an_existing_test(self):
        class Example(unittest.TestCase):
            @cases(value=[1])
            def test_value(self, value):
                pass

            def test_value_00(self):
                pass

        with self.assertRaisesRegex(ValueError, 'duplicate test name'):
            expand_cases(Example)
