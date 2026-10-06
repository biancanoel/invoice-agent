import pytest

from invoice_agent.durations import format_duration, parse_duration, parse_entry, to_hours


@pytest.mark.parametrize(
    "text, minutes",
    [("1h 30m", 90), ("1h 00m", 60), ("45m", 45), ("2h", 120), ("0m", 0), ("—", 0), ("15h 20m", 920),
     (" 20m ", 20), ("1h30m", 90)],
)
def test_parse_duration(text, minutes):
    assert parse_duration(text) == minutes


@pytest.mark.parametrize("bad", ["1.5", "abc", "1h 30", "h m"])
def test_parse_duration_rejects_garbage(bad):
    with pytest.raises(ValueError):
        parse_duration(bad)


@pytest.mark.parametrize(
    "text, minutes",
    [("40m", 40), ("1h 30m", 90), ("15h 20m", 920), (" 40m ", 40), ("1.5", 90), ("0.67", 40), ("8", 480), ("0m", 0)],
)
def test_parse_entry(text, minutes):
    assert parse_entry(text) == minutes


@pytest.mark.parametrize("bad", ["forty", "1h 30", "", "  "])
def test_parse_entry_rejects_garbage_and_blanks(bad):
    with pytest.raises(ValueError):
        parse_entry(bad)


@pytest.mark.parametrize("minutes, text", [(40, "40m"), (90, "1h 30m"), (60, "1h 00m"), (920, "15h 20m"), (0, "0m")])
def test_format_duration(minutes, text):
    assert format_duration(minutes) == text


@pytest.mark.parametrize("minutes", [0, 5, 40, 90, 920])
def test_format_then_parse_round_trips(minutes):
    assert parse_duration(format_duration(minutes)) == minutes


def test_to_hours_for_the_invoice():
    assert [to_hours(m) for m in (40, 20, 90, 920)] == [0.67, 0.33, 1.5, 15.33]
