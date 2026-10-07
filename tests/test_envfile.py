import os

from wakeel import envfile


def test_env_file_is_loaded_without_overriding(tmp_path, monkeypatch):
    f = tmp_path / ".env"
    f.write_text('# comment\nWAKEEL_T1=from-file\nexport WAKEEL_T2="quoted"\nWAKEEL_T3=\nWAKEEL_T4=keep-env\n')
    monkeypatch.delenv("WAKEEL_T1", raising=False)
    monkeypatch.delenv("WAKEEL_T2", raising=False)
    monkeypatch.setenv("WAKEEL_T4", "already-set")
    envfile.load(f)
    assert os.environ["WAKEEL_T1"] == "from-file" and os.environ["WAKEEL_T2"] == "quoted"
    assert "WAKEEL_T3" not in os.environ and os.environ["WAKEEL_T4"] == "already-set"
