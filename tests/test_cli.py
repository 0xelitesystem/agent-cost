import json

from agent_cost.cli import analyze_cost, main


def test_report_json_shape(simple_transcript, capsys):
    code = main(["report", simple_transcript, "--json"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert round(payload["total_cost"], 2) == 27.00
    assert payload["total_usage"]["total"] == 2_200_000
    assert len(payload["by_model"]) == 2
    assert "cache" in payload
    assert "loops" in payload


def test_report_json_includes_loop(loop_transcript, capsys):
    main(["report", loop_transcript, "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert len(payload["loops"]) == 1
    assert payload["loops"][0]["count"] == 6
    assert payload["loops"][0]["all_errored"] is True


def test_fail_over_gates_expensive_sessions(simple_transcript, capsys):
    # $27 session, budget $1 -> exit 1
    assert main(["report", simple_transcript, "--fail-over", "1", "--no-color"]) == 1


def test_fail_over_passes_cheap_sessions(simple_transcript, capsys):
    # $27 session, budget $100 -> exit 0
    assert main(["report", simple_transcript, "--fail-over", "100", "--no-color"]) == 0


def test_missing_target_exits_2(capsys):
    assert main(["report", "does-not-exist-xyz"]) == 2


def test_markdown_report(simple_transcript, tmp_path):
    out = tmp_path / "report.md"
    assert main(["report", simple_transcript, "--md", str(out), "--no-color"]) == 0
    content = out.read_text(encoding="utf-8")
    assert "agent-cost report" in content
    assert "Cost by model" in content


def test_prices_override_via_cli(simple_transcript, tmp_path, capsys):
    prices = tmp_path / "prices.json"
    prices.write_text(json.dumps({
        "sonnet": {"input": 0, "output": 0, "cache_write": 0, "cache_read": 0},
        "opus": {"input": 0, "output": 0, "cache_write": 0, "cache_read": 0},
    }), encoding="utf-8")
    main(["report", simple_transcript, "--prices", str(prices), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["total_cost"] == 0.0


def test_unknown_rate_flag_in_json(unknown_model_transcript, capsys):
    main(["report", unknown_model_transcript, "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["has_unknown_rates"] is True


def test_analyze_cost_library_entry(simple_transcript):
    result = analyze_cost(simple_transcript)
    assert round(result.total_cost, 2) == 27.00


def test_top_ranks_sessions_by_cost(simple_transcript, cache_transcript,
                                    loop_transcript, monkeypatch, capsys):
    from pathlib import Path

    from agent_cost import cli
    fixtures = [Path(simple_transcript), Path(cache_transcript),
                Path(loop_transcript)]
    monkeypatch.setattr(cli, "discover_transcripts", lambda _f=None: fixtures)

    assert main(["top", "--json", "--limit", "3"]) == 0
    payload = json.loads(capsys.readouterr().out)
    costs = [row["total_cost"] for row in payload]
    assert costs == sorted(costs, reverse=True)
    # the $27 simple session must rank first
    assert payload[0]["transcript"] == str(Path(simple_transcript))


def test_list_handles_no_transcripts(monkeypatch, capsys):
    from agent_cost import cli
    monkeypatch.setattr(cli, "discover_transcripts", lambda _f=None: [])
    assert main(["list"]) == 2
