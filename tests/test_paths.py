from timeexisting import paths


def test_config_file_lives_under_config_dir():
    assert paths.config_file() == paths.config_dir() / "config.toml"


def test_ledger_and_logs_live_under_state_dir():
    assert paths.ledger_dir() == paths.state_dir() / "ledger"
    assert paths.logs_dir() == paths.state_dir() / "logs"
    assert paths.log_file() == paths.logs_dir() / "tracker.log"


def test_config_dir_and_state_dir_are_distinct():
    assert paths.config_dir() != paths.state_dir()
