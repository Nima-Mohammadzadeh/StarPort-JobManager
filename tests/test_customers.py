"""Customer names typed every which way still land on the one customer they mean."""
import pytest

import customers as C


@pytest.mark.parametrize("name", [
    "GO2", "go2", "Go2", "gO2", "  go2 ", "GO 2", "go-2", "Go_2", "GO2.", "go2!", "G0 2", "g02",
    "Go2Partners", "Go2 Partners", "GO2 Partners LLC", "go2partners, inc.", "Go 2 Partners Inc",
    "go2 partner", "Go Two", "go two partners", "Go2Partenrs", "Go2 Partnres", "Go2Partners - Austin",
])
def test_every_way_of_writing_go2(name):
    assert C.canonical(name) == "GO2"


@pytest.mark.parametrize("name", [
    "Venture Research", "venture research", "VENTURE RESEARCH", "Venture", "venture", " VENTURE ",
    "Venture Research Inc", "Venture Research, Inc.", "Venture Research LLC", "venture-research",
    "VentureResearch", "Venture_Research", "Ventur Research", "Venture Reserch", "Ventrue Research",
    "Venture Reasearch", "Ventrue", "Ventur", "Venture Research Inc. (Austin)",
])
def test_every_way_of_writing_venture(name):
    assert C.canonical(name) == "Venture Research"


@pytest.mark.parametrize("name", ["CDO", "cdo", " cDo ", "C.D.O."])
def test_cdo_still_by_name(name):
    assert C.canonical(name) == "CDO"


@pytest.mark.parametrize("name", [
    "", None, "   ", "Acme", "GO23", "go3", "G2", "G2 ID Source", "g2idsource", "GO", "Go Partners",
    "Vantage ID", "Vera Bradley", "Versatile", "Vista Outdoors", "Vent", "Adventure", "Venture Capital",
    "Research", "CDO2", "CDO Technologies", "Ventures Research Labs Unlimited Group",
])
def test_other_names_are_not_pulled_in(name):
    assert C.canonical(name) is None


def test_a_real_customer_close_to_an_alias_wins_over_the_typo():
    # "go2p" style shorthand is fine, but a name that is exactly another known customer is that customer.
    assert C.canonical("Go2Partners", known=["Go2Partnerz"]) == "GO2"      # exact alias beats a known look-alike
    assert C.canonical("Go2Partnerz", known=["Go2Partnerz"]) is None       # the look-alike is its own customer


def test_how_it_matched():
    assert C.match("Venture Research").how == "name"
    assert C.match("venture").how == "alias"
    assert C.match("Venture Research Austin").how == "starts with"
    assert C.match("Ventrue Research").how == "spelling"


def test_folder_for_each_customer():
    assert C.folder_for("GO2") == "Go2Partners"
    assert C.folder_for("Venture Research") == "Venture Research"
    assert C.folder_for("CDO") == "CDO"
    assert C.folder_for("Acme") == "Acme"


def test_distance_counts_a_swap_as_one():
    assert C._distance("ventrue", "venture") == 1
    assert C._distance("abc", "abc") == 0
    assert C._distance("go2", "g2") == 1
