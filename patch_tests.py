import re

c = open("tests/test_act_phase.py").read()

c = c.replace("""<<<<<<< HEAD
    # Even without fabricating rates from the critical label, measured outage
    # signals keep the plan behind approval.
    report = build_act_report(
        _state(
            alert,
            plan,
            results=_measured_results(
                error_rate=0.85,
                slo_burn_rate=18.0,
                slo_breached=True,
                saturation=0.8,
                still_escalating=True,
            ),
        ),
        evaluate_fn=ALLOW,
    )
=======
    report = _build(_state(alert, plan))
>>>>>>> master""", """    # Even without fabricating rates from the critical label, measured outage
    # signals keep the plan behind approval.
    report = _build(
        _state(
            alert,
            plan,
            results=_measured_results(
                error_rate=0.85,
                slo_burn_rate=18.0,
                slo_breached=True,
                saturation=0.8,
                still_escalating=True,
            ),
        ),
    )""")

with open("tests/test_act_phase.py", "w") as f:
    f.write(c)

c2 = open("tests/test_severity_engine.py").read()
c2 = c2.replace("""<<<<<<< HEAD
        affected_services=1,
        user_facing=False,
        error_rate=0.02,
        slo_breached=False,
        slo_burn_rate=0.5,
        saturation=0.1,
        still_escalating=False,
        error_rate_slope=0.0,
        hypothesis_confidence=1.0,
=======
        affected_services=1, user_facing=False, error_rate=0.02, slo_breached=False,
        slo_burn_rate=0.5, saturation=0.1, still_escalating=False,
        hypothesis_confidence=1.0,
        hypothesis_confidence_calibrated=True,
>>>>>>> master""", """        affected_services=1,
        user_facing=False,
        error_rate=0.02,
        slo_breached=False,
        slo_burn_rate=0.5,
        saturation=0.1,
        still_escalating=False,
        error_rate_slope=0.0,
        hypothesis_confidence=1.0,
        hypothesis_confidence_calibrated=True,""")

c2 = c2.replace("""<<<<<<< HEAD
        affected_services=5,
        user_facing=True,
        revenue_impacting=True,
        error_rate=0.8,
        slo_breached=True,
        slo_burn_rate=0.0,
        saturation=0.0,
        still_escalating=False,
        error_rate_slope=0.0,
        hypothesis_confidence=1.0,
=======
        affected_services=5, user_facing=True, revenue_impacting=True,
        error_rate=0.8, slo_breached=True,
        slo_burn_rate=0.0, saturation=0.0, still_escalating=False,
        hypothesis_confidence=1.0,
        hypothesis_confidence_calibrated=True,
>>>>>>> master""", """        affected_services=5,
        user_facing=True,
        revenue_impacting=True,
        error_rate=0.8,
        slo_breached=True,
        slo_burn_rate=0.0,
        saturation=0.0,
        still_escalating=False,
        error_rate_slope=0.0,
        hypothesis_confidence=1.0,
        hypothesis_confidence_calibrated=True,""")

with open("tests/test_severity_engine.py", "w") as f:
    f.write(c2)
