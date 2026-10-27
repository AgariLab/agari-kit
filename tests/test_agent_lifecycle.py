import unittest
from unittest.mock import Mock, patch

from agari_kit.agent import managed_agents


class AgentLifecycleTests(unittest.TestCase):
    def test_normal_exit_and_partial_factory_failure_close_every_loaded_agent(self):
        first, second = Mock(), Mock()
        with patch("agari_kit.agent.make_agent", side_effect=[first, second]):
            with managed_agents(["a", "b"]) as agents:
                self.assertEqual(agents, [first, second])
        first.close.assert_called_once_with()
        second.close.assert_called_once_with()
        first.close.reset_mock()
        with patch("agari_kit.agent.make_agent", side_effect=[first, ValueError("bad factory")]):
            with self.assertRaisesRegex(ValueError, "bad factory"), managed_agents(["a", "bad"]):
                self.fail("Incomplete agents must not run")
        first.close.assert_called_once_with()

    def test_cleanup_errors_do_not_prevent_other_cleanup_or_mask_game_error(self):
        first, second = Mock(), Mock()
        second.close.side_effect = OSError("private implementation detail")
        with patch("agari_kit.agent.make_agent", side_effect=[first, second]):
            with self.assertRaisesRegex(ValueError, "original game error") as caught:
                with managed_agents(["a", "b"]):
                    raise ValueError("original game error")
        first.close.assert_called_once_with()
        second.close.assert_called_once_with()
        self.assertEqual(caught.exception.__notes__, ["Agent cleanup failed: OSError"])

    def test_cleanup_failure_does_not_report_a_successful_run(self):
        agent = Mock()
        agent.close.side_effect = OSError("private implementation detail")
        with patch("agari_kit.agent.make_agent", return_value=agent):
            with self.assertRaisesRegex(RuntimeError, "Agent cleanup failed: OSError") as caught:
                with managed_agents(["a"]):
                    pass
        self.assertNotIn("private", str(caught.exception))
