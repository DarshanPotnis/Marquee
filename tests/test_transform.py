"""Raw page body -> clean rows. Pure: no I/O. All data here is synthetic."""

import copy
from datetime import UTC, date, datetime, time
from typing import Any

import pytest

from marquee.transform import transform_page

RUN_AT = datetime(2026, 10, 6, 18, 0, tzinfo=UTC)


def event(**overrides: Any) -> dict[str, Any]:
    e: dict[str, Any] = {
        "name": "Synthetic Band Live",
        "id": "vvSYNTH0000001",
        "url": "https://www.ticketmaster.com/synthetic/event/0000SYNTH",
        "sales": {"public": {"startDateTime": "2026-08-01T17:00:00Z", "startTBD": False,
                             "startTBA": False}},
        "dates": {
            "start": {"localDate": "2026-11-01", "localTime": "20:00:00",
                      "dateTime": "2026-11-02T04:00:00Z", "dateTBD": False, "dateTBA": False,
                      "timeTBA": False, "noSpecificTime": False},
            "timezone": "America/Los_Angeles",
            "status": {"code": "onsale"},
        },
        "classifications": [{"primary": True, "segment": {"name": "Music"},
                             "genre": {"name": "Rock"}}],
        "_embedded": {
            "venues": [{"name": "Synthetic Hall", "id": "KovSYNTH001",
                        "timezone": "America/Los_Angeles", "city": {"name": "Los Angeles"},
                        "state": {"stateCode": "CA"},
                        "location": {"longitude": "-118.25000", "latitude": "34.05000"}}],
            "attractions": [
                {"name": "Synthetic Band", "id": "K8vSYNTH001",
                 "classifications": [{"primary": True, "segment": {"name": "Music"},
                                      "genre": {"name": "Rock"}}]},
                {"name": "Synthetic Opener", "id": "K8vSYNTH002"},
            ],
        },
    }
    for path, value in overrides.items():
        target = e
        *parents, leaf = path.split("__")
        for p in parents:
            target = target.setdefault(p, {})
        if value is DELETE:
            target.pop(leaf, None)
        else:
            target[leaf] = value
    return e


DELETE = object()


def page(*events: dict[str, Any]) -> dict[str, Any]:
    body: dict[str, Any] = {"page": {"size": 200, "totalElements": len(events),
                                     "totalPages": 1, "number": 0}}
    if events:
        body["_embedded"] = {"events": list(events)}
    return body


def one(e: dict[str, Any]):  # type: ignore[no-untyped-def]
    rows = transform_page(page(e), run_at=RUN_AT)
    assert len(rows.events) == 1
    return rows, rows.events[0]


# --- The ordinary case -------------------------------------------------------------------------

def test_a_full_event_becomes_event_venue_and_attraction_rows() -> None:
    rows, ev = one(event())
    assert ev.source_id == "vvSYNTH0000001"
    assert ev.name == "Synthetic Band Live"
    assert ev.venue_source_id == "KovSYNTH001"
    assert (ev.local_date, ev.local_time) == (date(2026, 11, 1), time(20, 0))
    assert ev.starts_at == datetime(2026, 11, 2, 4, 0, tzinfo=UTC)
    assert (ev.status, ev.segment, ev.genre) == ("onsale", "Music", "Rock")
    assert ev.public_sale_start == datetime(2026, 8, 1, 17, 0, tzinfo=UTC)
    assert ev.url == "https://www.ticketmaster.com/synthetic/event/0000SYNTH"
    assert ev.attraction_ids == ("K8vSYNTH001", "K8vSYNTH002")  # position 0 is the headliner
    (venue,) = rows.venues
    assert (venue.source_id, venue.name, venue.city, venue.state, venue.timezone) == \
        ("KovSYNTH001", "Synthetic Hall", "Los Angeles", "CA", "America/Los_Angeles")
    assert (venue.latitude, venue.longitude) == (34.05, -118.25)  # parsed from strings
    assert [(a.source_id, a.name, a.segment, a.genre) for a in rows.attractions] == \
        [("K8vSYNTH001", "Synthetic Band", "Music", "Rock"),
         ("K8vSYNTH002", "Synthetic Opener", None, None)]
    assert rows.onsale_nulled == {}


def test_a_page_without_events_gives_no_rows() -> None:
    rows = transform_page(page(), run_at=RUN_AT)
    assert (rows.events, rows.venues, rows.attractions) == ((), (), ())


# --- Gaps in the data (api-notes §14) -----------------------------------------------------------

def test_a_missing_venue() -> None:
    rows, ev = one(event(_embedded__venues=DELETE))
    assert ev.venue_source_id is None
    assert rows.venues == ()


def test_missing_attractions() -> None:
    rows, ev = one(event(_embedded__attractions=DELETE))
    assert ev.attraction_ids == ()
    assert rows.attractions == ()


def test_a_tba_event_has_no_date_time_or_start() -> None:
    tba = event(dates__start={"dateTBD": False, "dateTBA": True, "timeTBA": False,
                              "noSpecificTime": False},
                dates__status={"code": "postponed"})
    _, ev = one(tba)
    assert (ev.local_date, ev.local_time, ev.starts_at, ev.status) == \
        (None, None, None, "postponed")


def test_a_no_specific_time_event_has_a_date_but_no_time_or_start() -> None:
    nst = event(dates__start={"localDate": "2026-11-01", "dateTBD": False, "dateTBA": False,
                              "timeTBA": False, "noSpecificTime": True})
    _, ev = one(nst)
    assert (ev.local_date, ev.local_time, ev.starts_at) == (date(2026, 11, 1), None, None)


@pytest.mark.parametrize("location", [DELETE, {"latitude": "abc", "longitude": ""}])
def test_unusable_coordinates_become_null(location: Any) -> None:
    rows, _ = one(event(**{"_embedded__venues": [
        {"name": "Hall", "id": "KovX", "location": location} if location is not DELETE
        else {"name": "Hall", "id": "KovX"}]}))
    assert (rows.venues[0].latitude, rows.venues[0].longitude) == (None, None)


def test_city_names_are_trimmed() -> None:
    venue = copy.deepcopy(event()["_embedded"]["venues"][0])
    venue["city"] = {"name": " Long Beach"}
    rows, _ = one(event(_embedded__venues=[venue]))
    assert rows.venues[0].city == "Long Beach"


def test_status_is_kept_as_the_api_spells_it() -> None:
    _, ev = one(event(dates__status={"code": "cancelled"}))
    assert ev.status == "cancelled"


def test_the_primary_classification_wins_over_the_first() -> None:
    _, ev = one(event(classifications=[
        {"primary": False, "segment": {"name": "Film"}, "genre": {"name": "Music"}},
        {"primary": True, "segment": {"name": "Music"}, "genre": {"name": "Jazz"}}]))
    assert (ev.segment, ev.genre) == ("Music", "Jazz")


@pytest.mark.parametrize(("genre", "stored"), [("Undefined", None), ("Other", "Other")])
def test_the_undefined_placeholder_becomes_null_but_other_is_kept(
    genre: str, stored: str | None
) -> None:
    _, ev = one(event(classifications=[{"primary": True, "segment": {"name": "Music"},
                                        "genre": {"name": genre}}]))
    assert ev.genre == stored


def test_an_attraction_listed_twice_keeps_its_first_position() -> None:
    a = event()["_embedded"]["attractions"]
    _, ev = one(event(_embedded__attractions=[a[0], a[1], a[0]]))
    assert ev.attraction_ids == ("K8vSYNTH001", "K8vSYNTH002")


def test_an_event_without_an_id_is_skipped_and_counted() -> None:
    rows = transform_page(page(event(id=DELETE), event(id="vvKEPT")), run_at=RUN_AT)
    assert [e.source_id for e in rows.events] == ["vvKEPT"]
    assert rows.skipped_events == 1


# --- Public onsale plausibility -----------------------------------------------------------------

def onsale(value: str | object, **overrides: Any):  # type: ignore[no-untyped-def]
    return one(event(sales__public__startDateTime=value, **overrides))


def test_a_missing_onsale_is_null_but_not_counted() -> None:
    rows, ev = onsale(DELETE)
    assert ev.public_sale_start is None
    assert rows.onsale_nulled == {}


@pytest.mark.parametrize(
    ("value", "reason"),
    [
        ("1900-01-01T18:00:00Z", "placeholder_1900"),
        ("1900-01-01T06:00:00Z", "placeholder_1900"),
        ("2026-11-02T04:00:01Z", "after_event"),  # one second after the start
        ("2023-08-01T17:00:00Z", "too_early"),  # more than 2 years before the event
        ("soon", "unparseable"),
    ],
)
def test_implausible_onsales_become_null_and_are_counted(value: str, reason: str) -> None:
    rows, ev = onsale(value)
    assert ev.public_sale_start is None
    assert rows.onsale_nulled == {reason: 1}


@pytest.mark.parametrize("value", ["2025-11-02T04:00:00Z", "2026-11-02T04:00:00Z"])
def test_plausible_onsales_are_kept(value: str) -> None:
    # Up to 2 years before the start, and on the start itself.
    rows, ev = onsale(value)
    assert ev.public_sale_start == datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=UTC)
    assert rows.onsale_nulled == {}


def test_a_date_only_event_counts_as_starting_at_the_end_of_its_local_day() -> None:
    # Nov 1 is 25 hours long in LA and ends at 2026-11-02T08:00:00Z. With no dates.timezone, the
    # venue's timezone is used; a UTC day would end at 00:00Z and wrongly call this "after".
    date_only = {"start": {"localDate": "2026-11-01", "noSpecificTime": True},
                 "status": {"code": "onsale"}}
    rows, ev = one(event(dates=date_only, sales__public__startDateTime="2026-11-02T07:30:00Z"))
    assert ev.public_sale_start == datetime(2026, 11, 2, 7, 30, tzinfo=UTC)
    rows, ev = one(event(dates=date_only, sales__public__startDateTime="2026-11-02T08:00:01Z"))
    assert rows.onsale_nulled == {"after_event": 1}


# An undated (TBA) event has no start to compare with. A postponed show's onsale can be years old
# and still real (Phase 4: a genuine 2024-07-26 onsale was wrongly nulled), so: use the original
# date when the API gives one (dates.initialStartDate); otherwise accept any past onsale, and only
# reject placeholders, unparseable values and onsales more than 2 years in the future.

def undated_event(value: str, initial: dict[str, str] | None = None) -> dict[str, Any]:
    dates: dict[str, Any] = {"start": {"dateTBA": True}, "status": {"code": "postponed"}}
    if initial is not None:
        dates["initialStartDate"] = initial
    return event(dates=dates, sales__public__startDateTime=value)


@pytest.mark.parametrize(
    ("value", "kept", "reason"),
    [
        ("2024-07-26T17:00:00Z", True, None),  # the real case from Phase 4: years before the run
        ("2019-03-01T17:00:00Z", True, None),  # any past onsale is accepted
        ("2027-06-01T17:00:00Z", True, None),  # within 2 years of the run
        ("2029-01-01T18:00:00Z", False, "too_late"),  # more than 2 years after the run
        ("1900-01-01T18:00:00Z", False, "placeholder_1900"),
        ("soon", False, "unparseable"),
    ],
)
def test_an_undated_event_without_an_original_date_accepts_past_onsales(
    value: str, kept: bool, reason: str | None
) -> None:
    rows, ev = one(undated_event(value))
    assert (ev.public_sale_start is not None) == kept
    assert rows.onsale_nulled == ({} if reason is None else {reason: 1})


ORIGINAL = {"localDate": "2026-08-30", "localTime": "18:00:00", "dateTime": "2026-08-31T01:00:00Z"}


@pytest.mark.parametrize(
    ("value", "kept", "reason"),
    [
        ("2026-05-01T17:00:00Z", True, None),  # before the original date
        ("2026-08-31T01:00:00Z", True, None),  # on it
        ("2026-09-15T17:00:00Z", False, "after_event"),  # after the original date
        ("2024-01-01T17:00:00Z", False, "too_early"),  # more than 2 years before it
    ],
)
def test_an_undated_event_with_an_original_date_is_checked_against_it(
    value: str, kept: bool, reason: str | None
) -> None:
    rows, ev = one(undated_event(value, ORIGINAL))
    assert (ev.public_sale_start is not None) == kept
    assert rows.onsale_nulled == ({} if reason is None else {reason: 1})


def test_an_original_date_without_a_time_counts_as_the_end_of_that_local_day() -> None:
    date_only = {"localDate": "2026-08-30"}  # ends 2026-08-31T07:00:00Z (PDT)
    rows, ev = one(undated_event("2026-08-31T06:30:00Z", date_only))
    assert ev.public_sale_start is not None
    rows, ev = one(undated_event("2026-08-31T07:00:01Z", date_only))
    assert rows.onsale_nulled == {"after_event": 1}
