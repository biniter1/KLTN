from tier2_agent.config import Tier2Config


def test_token_budget_is_ratio_of_context_window():
    cfg = Tier2Config(context_window_tokens=1000, normalize_token_budget_ratio=0.7)
    assert cfg.token_budget == 700


def test_defaults_match_design_placeholders():
    cfg = Tier2Config()
    assert cfg.max_iterations == 8
    assert cfg.tool_call_timeout_s == 30.0
    assert cfg.session_timeout_s == 300.0
    assert cfg.max_open_hypotheses == 4
    assert cfg.max_tool_calls_per_round == 4
    assert cfg.max_consecutive_tool_errors == 2
    assert cfg.tool_output_char_cap == 4000
