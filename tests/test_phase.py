from trellis.core.phase import Phase, can_transition, get_agent_phase


def test_valid_transitions():
    assert can_transition(Phase.SUBMITTED, Phase.IDEATION)
    assert can_transition(Phase.IDEATION, Phase.IDEATION_REVIEW)
    assert can_transition(Phase.IDEATION_REVIEW, Phase.IMPLEMENTATION)
    assert can_transition(Phase.IDEATION_REVIEW, Phase.KILLED)
    assert can_transition(Phase.IMPLEMENTATION, Phase.IMPLEMENTATION_REVIEW)
    assert can_transition(Phase.VALIDATION_REVIEW, Phase.RELEASE)
    assert can_transition(Phase.RELEASE, Phase.RELEASED)


def test_invalid_transitions():
    assert not can_transition(Phase.SUBMITTED, Phase.IMPLEMENTATION)
    assert not can_transition(Phase.IDEATION, Phase.RELEASE)
    assert not can_transition(Phase.KILLED, Phase.IDEATION)


def test_released_loops_back():
    # Released ideas can loop back to ideation for refinement
    assert can_transition(Phase.RELEASED, Phase.IDEATION)
    assert can_transition(Phase.RELEASED, Phase.KILLED)


def test_loop_back_transitions():
    assert can_transition(Phase.IDEATION_REVIEW, Phase.IDEATION)
    assert can_transition(Phase.IMPLEMENTATION_REVIEW, Phase.IMPLEMENTATION)
    assert can_transition(Phase.VALIDATION_REVIEW, Phase.IMPLEMENTATION)


def test_get_agent_phase():
    assert get_agent_phase(Phase.IDEATION) == Phase.IDEATION
    assert get_agent_phase(Phase.IMPLEMENTATION) == Phase.IMPLEMENTATION
    assert get_agent_phase(Phase.IDEATION_REVIEW) is None
    assert get_agent_phase(Phase.SUBMITTED) is None
