"""
Every file a model is asked to judge must reach it whole enough to judge.

One bug class, found three times on a single deployed mission:

* The meeting cut the whole table -- every artifact concatenated -- to its
  first 3,000 characters. Three attendees objected that `render_research.md`
  "ends mid-URL" and "covers only Render"; the Railway and Fly.io notes were on
  the table and none of them could see it.
* Synthesis spent its budget first-come, so the files written last in a
  mission -- the final report, the recorded decision -- could be dropped from
  the conclusion entirely.
* The contract demanded cited URLs of `providers.csv`, a four-column data file
  with nowhere a URL could go. The step was rejected twice and the mission
  marked `unverified`.

These pin the fix for each: every file gets a fair share with its ending kept,
and a data file is not asked for prose.
"""
import contracts
import debate
import synthesis


def _table(names, size=6000):
    head = "Work already delivered to your desk by colleagues."
    blocks = [f"--- {n} (from Alice) ---\n" + "x" * size + f" END-OF-{n}" for n in names]
    return head + "\n\n" + "\n\n".join(blocks)


# --- the meeting table ------------------------------------------------------

def test_every_file_on_the_table_is_seen():
    """The regression: only the first file used to survive the cut."""
    names = ["render_research.md", "railway_research.md", "flyio_research.md"]
    room = debate._fit_room(_table(names))
    for name in names:
        assert f"--- {name}" in room, f"{name} vanished from the table"


def test_every_file_keeps_its_ending():
    """An objection is often about a conclusion, which sits at the end."""
    names = ["render_research.md", "railway_research.md", "flyio_research.md"]
    room = debate._fit_room(_table(names))
    for name in names:
        assert f"END-OF-{name}" in room


def test_the_table_stays_inside_its_budget():
    room = debate._fit_room(_table(["a.md", "b.md", "c.md", "d.md"]))
    assert len(room) <= debate.MAX_CONTEXT_CHARS * 1.05


def test_a_small_table_is_untouched():
    small = _table(["a.md"], size=200)
    assert debate._fit_room(small) == small


# --- synthesis --------------------------------------------------------------

def test_the_last_file_written_reaches_the_conclusion():
    """The final report is written last; first-come could drop it."""
    arts = [(f"note{i}.md", "x" * 5000 + f" END{i}") for i in range(7)]
    arts.append(("final_report.md", "y" * 5000 + " THE-RECOMMENDATION"))
    block = synthesis._artifact_block(arts)
    assert "final_report.md" in block
    assert "THE-RECOMMENDATION" in block
    assert all(f"note{i}.md" in block for i in range(7))


# --- data files and citations ----------------------------------------------

_DESC = ("Cross-check every row of providers.csv against a live web search "
         "using SQL, and cite sources")


def test_a_csv_is_not_asked_for_citations():
    spec = contracts.infer_spec("providers.csv", _DESC, citations_expected=True)
    assert spec.require_citations is False


def test_a_json_file_is_not_asked_for_citations():
    spec = contracts.infer_spec("prices.json", _DESC, citations_expected=True)
    assert spec.require_citations is False


def test_prose_still_is():
    """The exemption is for data files only; a markdown note can carry URLs."""
    spec = contracts.infer_spec("validation-and-cost.md", _DESC, citations_expected=True)
    assert spec.require_citations is True


def test_the_csv_from_the_observed_mission_now_passes():
    csv = ("provider,monthly_usd,disk_supported,cold_start_seconds\n"
           "Render,7,yes,0\nRailway,5,yes,0\nFly.io,2.02,yes,2\n")
    spec = contracts.infer_spec("providers.csv", _DESC, citations_expected=True)
    assert contracts.check(csv, spec).ok


# --- the contractor who was hired and never given the work ------------------

def test_a_producing_step_moves_to_the_contractor_hired_for_it():
    """
    The regression. `foundry.find_gaps` drops write_file from a producing
    step's needs (the orchestrator grants it at dispatch), so the contractor is
    hired without it. `regrant_steps` used to keep write_file in the SAME
    step's needs, found the contractor short of it, and left the step with the
    specialist who could not do the work -- Bob, with no web_search, asked to
    cross-check against the web.
    """
    import planner
    from schemas import PlanStep

    step = PlanStep(id="s2", agent_id="data_analyst",
                    description="cross-check providers.csv against the web with SQL",
                    needs_tools=["web_search", "query_data", "calculate", "write_file"],
                    produces="providers.csv")
    tools = {
        "data_analyst": ["query_data", "calculate", "read_file"],
        "researcher": ["web_search", "fetch_url", "calculate"],
        # Hired with exactly what find_gaps reported: no write_file.
        "hosting_cost_verification_specialist": ["web_search", "query_data", "calculate"],
    }
    planner.regrant_steps([step], list(tools), tools)
    assert step.agent_id == "hosting_cost_verification_specialist"


def test_a_step_that_writes_nothing_still_needs_write_file_granted():
    """The exemption is for PRODUCING steps only, which get write_file anyway."""
    import planner
    from schemas import PlanStep

    step = PlanStep(id="s9", agent_id="researcher", description="note it down",
                    needs_tools=["web_search", "write_file"], produces="")
    tools = {"researcher": ["web_search"], "creative": ["web_search", "write_file"]}
    planner.regrant_steps([step], list(tools), tools)
    assert step.agent_id == "creative"


# --- the judge can see what ran ----------------------------------------------

def test_the_critic_is_shown_which_tools_ran():
    """A step that called calculate three times was failed for 'no evidence'."""
    import critic
    assert critic._tool_record(["web_search", "calculate", "calculate", "query_data",
                                "calculate"]) == "web_search x1, calculate x3, query_data x1"
    assert critic._tool_record([]) == "none"
    assert critic._tool_record(None) == "none"
