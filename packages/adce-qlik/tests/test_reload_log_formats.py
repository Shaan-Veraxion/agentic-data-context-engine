from adce_qlik import parse_reload_log


def test_qlikview_progress_lines_with_space_grouped_counts():
    text = (
        "1/10/2026 2:14:07 AM: Execution started.\n"
        "1/10/2026 2:14:07 AM: 0012  Orders:\n"
        "1/10/2026 2:14:19 AM:       Orders << orders_v2 48 213 lines fetched\n"
        "1/10/2026 2:14:30 AM:       Execution finished.\n"
    )
    log = parse_reload_log(text)
    assert [(f.table, f.source, f.lines) for f in log.fetches] == [("Orders", "orders_v2", 48213)]
    assert log.started_at == "1/10/2026 2:14:07 AM"


def test_errors_are_collected():
    log = parse_reload_log("20261001T021400.000+0000 Error: Field 'X' not found\n")
    assert log.errors == ["Error: Field 'X' not found"]


def test_lines_fetched_colon_format():
    log = parse_reload_log("20261001T021400.000+0000 0003  T:\n20261001T021400.100+0000 Lines fetched: 1,024\n")
    assert [(f.table, f.lines) for f in log.fetches] == [("T", 1024)]
