import json
import socket

import pytest

import pytunnel


# ==========================
# parse_config_object
# ==========================

def test_parse_dict_forwards():
    parsed = pytunnel.parse_config_object({"h1": {"forwards": {10001: "10.0.0.1:80"}}})
    assert parsed == {"h1": {"forwards": {10001: "10.0.0.1:80"}, "remote_forwards": {}, "dynamic": []}}


def test_parse_list_forwards():
    parsed = pytunnel.parse_config_object(
        {"h1": {"forwards": [{"local": "10001", "remote": "10.0.0.1:80"}]}}
    )
    assert parsed["h1"]["forwards"] == {10001: "10.0.0.1:80"}


def test_parse_jump_hosts_wrapper():
    parsed = pytunnel.parse_config_object(
        {"jump_hosts": {"h1": {"forwards": {1: "a:1"}}}}
    )
    assert list(parsed) == ["h1"]


def test_parse_optional_fields():
    parsed = pytunnel.parse_config_object(
        {"h1": {"forwards": {1: "a:1"}, "user": "u", "port": "2222", "identity_file": "~/.ssh/k"}}
    )
    assert parsed["h1"]["user"] == "u"
    assert parsed["h1"]["port"] == 2222
    assert parsed["h1"]["identity_file"] == "~/.ssh/k"


def test_parse_remote_and_dynamic_forwards():
    parsed = pytunnel.parse_config_object(
        {
            "h1": {
                "remote_forwards": [{"remote": 19022, "local": "127.0.0.1:22"}],
                "dynamic": 1080,
            }
        }
    )
    assert parsed["h1"]["remote_forwards"] == {19022: "127.0.0.1:22"}
    assert parsed["h1"]["dynamic"] == [1080]


def test_parse_dynamic_list():
    parsed = pytunnel.parse_config_object({"h1": {"dynamic": [1080, "1081"]}})
    assert parsed["h1"]["dynamic"] == [1080, 1081]


@pytest.mark.parametrize(
    "raw,match",
    [
        ([], "top level"),
        ({"jump_hosts": []}, "jump_hosts"),
        ({"h1": "nope"}, "must be an object"),
        ({"h1": {"forwards": {"x": "a:1"}}}, "invalid port"),
        ({"h1": {"forwards": {70000: "a:1"}}}, "out of range"),
        ({"h1": {"forwards": {1: "no-port"}}}, "must be 'host:port'"),
        ({"h1": {"forwards": [{"local": 1}]}}, "list items"),
        ({"h1": {"forwards": "nope"}}, "must be a list or object"),
        ({"h1": {"dynamic": {"a": 1}}}, "port or list of ports"),
        ({"h1": {}}, "defines no forwards"),
    ],
)
def test_parse_invalid_configs(raw, match):
    with pytest.raises(RuntimeError, match=match):
        pytunnel.parse_config_object(raw)


# ==========================
# load_config
# ==========================

def test_load_config_yaml(tmp_path):
    cfg = tmp_path / "c.yaml"
    cfg.write_text("jump_hosts:\n  h1:\n    forwards:\n      10001: '10.0.0.1:80'\n")
    parsed = pytunnel.load_config(str(cfg))
    assert parsed["h1"]["forwards"] == {10001: "10.0.0.1:80"}


def test_load_config_json(tmp_path):
    cfg = tmp_path / "c.json"
    cfg.write_text(json.dumps({"h1": {"forwards": {"10001": "10.0.0.1:80"}}}))
    parsed = pytunnel.load_config(str(cfg))
    assert parsed["h1"]["forwards"] == {10001: "10.0.0.1:80"}


def test_load_config_missing(monkeypatch):
    monkeypatch.setattr(pytunnel, "find_default_config_path", lambda: None)
    with pytest.raises(RuntimeError, match="No config file found"):
        pytunnel.load_config(None)


def test_load_config_bad_extension(tmp_path):
    cfg = tmp_path / "c.toml"
    cfg.write_text("")
    with pytest.raises(RuntimeError, match="Unsupported config extension"):
        pytunnel.load_config(str(cfg))


def test_example_config_parses():
    parsed = pytunnel.load_config("config.example.yaml")
    gw = parsed["gateway.example.com"]
    assert gw["forwards"] == {10003: "10.0.0.50:22"}
    assert gw["remote_forwards"] == {19022: "127.0.0.1:22"}
    assert gw["dynamic"] == [1080]
    assert gw["user"] == "admin"
    assert gw["port"] == 2222


# ==========================
# build_ssh_command
# ==========================

def test_build_ssh_command_full():
    cfg = {
        "forwards": {10001: "10.0.0.1:80"},
        "remote_forwards": {19022: "127.0.0.1:22"},
        "dynamic": [1080],
        "user": "u",
        "port": 2222,
        "identity_file": "~/.ssh/key",
    }
    cmd = pytunnel.build_ssh_command("h1", cfg)
    assert cmd[0] == "ssh"
    assert cmd[-1] == "u@h1"
    assert "-fN" in cmd
    assert ["-L", "10001:10.0.0.1:80"] == cmd[cmd.index("-L"):cmd.index("-L") + 2]
    assert ["-R", "19022:127.0.0.1:22"] == cmd[cmd.index("-R"):cmd.index("-R") + 2]
    assert ["-D", "1080"] == cmd[cmd.index("-D"):cmd.index("-D") + 2]
    assert ["-p", "2222"] == cmd[cmd.index("-p"):cmd.index("-p") + 2]
    assert cmd[cmd.index("-i") + 1].endswith("/.ssh/key")
    # %C control path keeps socket paths short
    assert cmd[cmd.index("-S") + 1].endswith("/.ssh/cm/%C")


def test_build_ssh_command_minimal():
    cmd = pytunnel.build_ssh_command("h1", {"forwards": {1: "a:1"}})
    assert cmd[-1] == "h1"
    assert "-p" not in cmd
    assert "-i" not in cmd
    assert "-v" not in cmd


def test_build_ssh_command_verbose():
    cmd = pytunnel.build_ssh_command("h1", {"forwards": {1: "a:1"}}, verbose=True)
    assert "-v" in cmd


# ==========================
# iter_target_hosts
# ==========================

def test_iter_target_hosts_selection():
    hosts = {"a": {}, "b": {}}
    assert list(pytunnel.iter_target_hosts(hosts, ["b"])) == ["b"]
    assert pytunnel.iter_target_hosts(hosts, None) is hosts


def test_iter_target_hosts_unknown():
    with pytest.raises(ValueError, match="Unknown jump host"):
        pytunnel.iter_target_hosts({"a": {}}, ["nope"])


# ==========================
# Port helpers
# ==========================

def test_local_port_is_free_and_listening():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        port = s.getsockname()[1]
        assert not pytunnel.local_port_is_free(port)
        assert pytunnel.local_port_is_listening(port)
    assert pytunnel.local_port_is_free(port)
    assert not pytunnel.local_port_is_listening(port)


# ==========================
# main dispatch (dry-run, mocked ssh)
# ==========================

@pytest.fixture
def config_file(tmp_path):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({
        "h1": {"forwards": {"10001": "10.0.0.1:80"}, "user": "u", "port": 2222},
        "h2": {"dynamic": [1080]},
    }))
    return str(cfg)


def test_main_show(config_file, capsys):
    rc = pytunnel.main(["-c", config_file, "show"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "Jump Host: h1 (user=u, port=2222)" in out
    assert "-L 10001 -> 10.0.0.1:80" in out
    assert "-D 1080" in out


def test_main_show_json(config_file, capsys):
    rc = pytunnel.main(["-c", config_file, "show", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["h1"]["forwards"][0] == {"type": "local", "port": 10001, "destination": "10.0.0.1:80"}


def test_main_start_dry_run(config_file, capsys, monkeypatch):
    monkeypatch.setattr(pytunnel, "tunnel_status", lambda *a, **k: False)
    monkeypatch.setattr(pytunnel, "local_port_is_free", lambda *a, **k: True)
    rc = pytunnel.main(["-c", config_file, "start", "--dry-run"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "h1: ok - ssh" in out
    assert "-L 10001:10.0.0.1:80" in out
    assert "-p 2222" in out
    assert "u@h1" in out
    assert "-D 1080" in out


def test_main_start_already_running(config_file, capsys, monkeypatch):
    monkeypatch.setattr(pytunnel, "tunnel_status", lambda *a, **k: True)
    rc = pytunnel.main(["-c", config_file, "start"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "h1: ok - already running" in out


def test_main_start_busy_port(config_file, capsys, monkeypatch):
    monkeypatch.setattr(pytunnel, "tunnel_status", lambda *a, **k: False)
    monkeypatch.setattr(pytunnel, "local_port_is_free", lambda *a, **k: False)
    rc = pytunnel.main(["-c", config_file, "start"])
    out = capsys.readouterr().out
    assert rc == 1
    assert "h1: error - local port(s) busy: 10001" in out


def test_main_restart_dry_run(config_file, capsys, monkeypatch):
    monkeypatch.setattr(pytunnel, "tunnel_status", lambda *a, **k: True)
    monkeypatch.setattr(pytunnel, "local_port_is_free", lambda *a, **k: True)
    rc = pytunnel.main(["-c", config_file, "restart", "--dry-run"])
    out = capsys.readouterr().out
    assert rc == 0
    assert out.count("ssh") >= 2  # stop is skipped in dry-run; start command printed per host


def test_main_stop_dry_run(config_file, capsys, monkeypatch):
    monkeypatch.setattr(pytunnel, "tunnel_status", lambda *a, **k: True)
    rc = pytunnel.main(["-c", config_file, "stop", "--dry-run"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "-O exit" in out
    assert "-p 2222" in out  # port must match start for the %C hash


def test_main_stop_not_running(config_file, capsys, monkeypatch):
    monkeypatch.setattr(pytunnel, "tunnel_status", lambda *a, **k: False)
    rc = pytunnel.main(["-c", config_file, "stop"])
    assert rc == 0
    assert "not running" in capsys.readouterr().out


def test_main_status(config_file, capsys, monkeypatch):
    monkeypatch.setattr(pytunnel, "tunnel_status", lambda target, *a, **k: target == "u@h1")
    monkeypatch.setattr(pytunnel, "local_port_is_listening", lambda *a, **k: True)
    rc = pytunnel.main(["-c", config_file, "status"])
    out = capsys.readouterr().out
    assert rc == 1  # h2 is down
    assert "h1: running" in out
    assert "-L 10001 -> 10.0.0.1:80 (listening)" in out
    assert "h2: stopped" in out


def test_main_status_json(config_file, capsys, monkeypatch):
    monkeypatch.setattr(pytunnel, "tunnel_status", lambda *a, **k: True)
    monkeypatch.setattr(pytunnel, "local_port_is_listening", lambda *a, **k: False)
    rc = pytunnel.main(["-c", config_file, "status", "--json"])
    results = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert results[0]["host"] == "h1"
    assert results[0]["state"] == "running"
    assert results[0]["forwards"][0]["listening"] is False


def test_main_host_selection(config_file, capsys, monkeypatch):
    monkeypatch.setattr(pytunnel, "tunnel_status", lambda *a, **k: True)
    rc = pytunnel.main(["-c", config_file, "status", "-j", "h2"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "h1" not in out
    assert "h2: running" in out


def test_main_unknown_host(config_file, capsys):
    rc = pytunnel.main(["-c", config_file, "status", "-j", "nope"])
    assert rc == 2


def test_main_missing_config(monkeypatch, capsys):
    monkeypatch.setattr(pytunnel, "find_default_config_path", lambda: None)
    rc = pytunnel.main(["status"])
    assert rc == 2


# ==========================
# Error detail formatting
# ==========================

def test_error_detail_includes_stderr():
    import subprocess
    exc = subprocess.CalledProcessError(255, ["ssh"], stderr="line1\nPermission denied (publickey).\n")
    detail = pytunnel._error_detail(exc)
    assert "255" in detail
    assert "Permission denied" in detail


def test_error_detail_no_stderr():
    import subprocess
    exc = subprocess.CalledProcessError(1, ["ssh"], stderr=None)
    assert pytunnel._error_detail(exc) == "exit status 1"


def test_tail_truncates():
    text = "\n".join(f"l{i}" for i in range(10))
    assert pytunnel._tail(text, lines=2) == "l8; l9"
