"""Explicit native history requests must not fall through to legacy Save."""

import pytest

from josh_room import cli


@pytest.mark.parametrize(
    "command, options",
    [
        ("create", ["--agent-selection", "{}"]),
        ("create", ["--agent-plan", "{}"]),
    ],
)
def test_local_backend_refuses_agent_requests_before_any_capture(
    tmp_path, monkeypatch, command, options
):
    calls = []
    monkeypatch.setattr(cli, "_room_identity", lambda _project: ("room", "Room"))
    monkeypatch.setattr(cli, "_effective_dimension", lambda _args: None)
    monkeypatch.setattr(cli, "_recipients", lambda: calls.append("recipients") or ["a", "b"])
    monkeypatch.setattr(cli, "_jat_root", lambda: tmp_path)
    monkeypatch.setattr(cli, "_backend_for_args", lambda *_args: object())
    monkeypatch.setattr(cli, "create_snapshot", lambda *_args, **_kwargs: calls.append("capture") or {})
    args = cli.build_parser().parse_args(
        ["snapshot", command, "room", "--source", str(tmp_path), "--backend", "local", *options]
    )
    with pytest.raises(ValueError, match="native Room Store"):
        cli.dispatch(args, tmp_path)
    assert calls == []
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("amounts", [[31], [17, 29]])
def test_human_noop_reports_agent_history_upload_bytes(capsys, amounts):
    cli.emit(
        {
            "ok": True,
            "status": "already-saved",
            "agent_result": {"entries": [{"data_uploaded": amount} for amount in amounts]},
        },
        False,
    )
    output = capsys.readouterr().out
    assert f"{sum(amounts)} bytes uploaded" in output
    assert "agent history" in output
    assert "0 bytes uploaded" not in output


def test_human_noop_does_not_call_unknown_history_upload_zero(capsys):
    cli.emit(
        {
            "ok": True,
            "status": "already-saved",
            "agent_result": {"entries": [{"data_uploaded": None, "upload_uncertain": True}]},
        },
        False,
    )
    output = capsys.readouterr().out
    assert "unknown" in output
    assert "0 bytes uploaded" not in output


@pytest.mark.parametrize("agent_result", [None, {"entries": [{"data_uploaded": 0}]}])
def test_human_true_zero_byte_noop_keeps_existing_message(capsys, agent_result):
    result = {"ok": True, "status": "already-saved"}
    if agent_result is not None:
        result["agent_result"] = agent_result
    cli.emit(result, False)
    assert capsys.readouterr().out == "Already saved — 0 bytes uploaded\n"
