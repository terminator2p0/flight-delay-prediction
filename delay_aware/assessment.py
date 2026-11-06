"""Compose BTS history and FlightAware evidence without inventing a pooled forecast."""

from datetime import date


def timing_phrase(minutes):
    if abs(minutes) < 0.5:
        return "on time"
    return f"{abs(minutes):.0f} min {'early' if minutes < 0 else 'late'}"


def traveler_view(flight, history, aeroapi, selected_history):
    chosen = aeroapi.get("selected") if aeroapi.get("selected_flight_verified") else None
    chosen = chosen or {}
    flagged = chosen.get("cancelled_or_untracked") is True
    diverted = chosen.get("diverted") is True
    status = "Check with airline" if flagged else "Diverted" if diverted else chosen.get("status") or "Live status unavailable"
    note = ("This flight is flagged as cancelled or no longer tracked. Confirm with the airline." if flagged else
            "This flight has been reported as diverted." if diverted else
            "Live details for this departure aren't available yet." if not chosen else None)
    times = {}
    for name, event, fallback, airport in (("departure", "out", "departure_local", "origin"),
                                            ("arrival", "in", "arrival_local", "destination")):
        kind = next((kind for kind in ("actual", "estimated", "scheduled")
                     if chosen.get(f"{kind}_{event}_local") and (kind != "estimated" or not (flagged or diverted))), "scheduled")
        times[name] = {"time": chosen.get(f"{kind}_{event}_local") or flight.get(fallback),
                       "kind": {"actual": "Actual", "estimated": "Expected", "scheduled": "Scheduled"}[kind],
                       "airport": flight[airport]}
    baseline = selected_history
    if not chosen and history.get("available") and history.get("historical_identity_verified"):
        baseline = {"overall": history["overall"], "first_date": history.get("observed_first_date"),
                    "last_date": history.get("observed_last_date")}
    performance = None
    if baseline:
        stats = baseline["overall"]
        scheduled = stats["scheduled_flights"]
        cancellations = stats.get("cancelled_flights")
        performance = {"delay_rate": stats.get("historical_delay_rate"),
                       "arrival_timing": stats.get("arrival_timing"),
                       "delayed_arrivals": stats.get("delayed_arrivals_15_minutes_or_more"),
                       "measured_arrivals": stats.get("completed_with_arrival_delay"),
                       "average_lateness_minutes": stats.get("mean_arrival_lateness_minutes"),
                       "cancelled_flights": cancellations,
                       "scheduled_flights": scheduled,
                       "cancellation_rate": cancellations / scheduled if scheduled and cancellations is not None else None,
                       "first_date": baseline.get("first_date"), "last_date": baseline.get("last_date")}
    groups = aeroapi.get("recent", {}).get("groups", [])
    if chosen:
        groups = [g for g in groups if g["operating_ident"] == chosen["operating_ident"]]
    recent = groups[0] if len(groups) == 1 and groups[0]["measured_gate_arrivals"] else None
    delay = chosen.get("actual_arrival_delay_minutes")
    actual = delay is not None
    if delay is None:
        delay = chosen.get("estimated_arrival_delay_minutes")
    arrival_message = None
    if delay is not None and not (flagged or diverted):
        minutes = round(abs(delay))
        timing = "on time" if minutes == 0 else f"{minutes} min {'early' if delay < 0 else 'late'}"
        arrival_message = f"{'Arrived' if actual else 'Arrival expected'} {timing}"
    parts = [(arrival_message or status) + "."]
    if performance and performance["delay_rate"] is not None:
        parts.append(f"Previously, {performance['delayed_arrivals']} of {performance['measured_arrivals']} arrivals were 15+ minutes late ({performance['delay_rate']:.1%}).")
        timing = performance["arrival_timing"]
        if timing and timing["mean_minutes"] is not None:
            parts.append(f"Historical average arrival: {timing_phrase(timing['mean_minutes'])}; typical arrival (median): {timing_phrase(timing['median_minutes'])}.")
        if performance["cancelled_flights"] is not None:
            parts.append(f"{performance['cancelled_flights']} of {performance['scheduled_flights']} flights were cancelled.")
    else:
        parts.append("There isn't enough confirmed history to summarize this flight's reliability.")
    if recent:
        parts.append(f"Recently, {recent['arrived_15_minutes_or_more_late']} of {recent['measured_gate_arrivals']} measured arrivals were 15+ minutes late—a small sample, not a forecast.")
        timing = recent.get("arrival_timing")
        if timing and timing["mean_minutes"] is not None:
            parts.append(f"Recent average arrival: {timing_phrase(timing['mean_minutes'])}; typical arrival (median): {timing_phrase(timing['median_minutes'])}. {timing['early_arrivals']} of {timing['measured_arrivals']} arrived early.")
    return {"status": status, "status_note": note, "attention": flagged or diverted,
            **times, "arrival_message": arrival_message, "performance": performance, "recent": recent,
            "updated_at": aeroapi.get("fetched_at"), "summary_text": " ".join(parts)}


def combine(flight, history, aeroapi, history_store, months):
    matched = aeroapi.get("selected_flight_verified", False)
    chosen = aeroapi.get("selected") if matched else None
    selected_history = None
    cautions = []
    if chosen:
        operating = chosen["operating_ident"]
        services = history.get("operating_services", []) if history.get("historical_identity_verified") else []
        corresponding = next((s for s in services if s["operating_carrier"] + s["operating_flight_number"] == operating), None)
        if corresponding:
            selected_history = {"source": history["source"], "operating_ident": operating,
                                "first_date": corresponding["observed_first_date"],
                                "last_date": corresponding["observed_last_date"], "overall": corresponding["overall"],
                                "match_scope": "Explicit BTS marketed-to-operating history on this route, across historical departure times."}
        elif history.get("reason_code") == "marketing_records_conflicted":
            cautions.append("Conflicting historical BTS records remain excluded. A current AeroAPI identity match does not resolve past source disagreements.")
        else:
            strict = history_store.report(operating[:2], operating[2:], flight["origin"], flight["destination"],
                                          as_of=date.fromisoformat(flight["departure_local"][:10]), months=months, match_carrier=True)
            if strict["available"]:
                selected_history = {"source": strict["source"], "operating_ident": operating,
                                    "first_date": strict["observed_first_date"], "last_date": strict["observed_last_date"],
                                    "overall": strict["overall"],
                                    "match_scope": "Exact operating airline, flight number, and route in BTS. Past marketing relationships and departure-time comparability are not established."}
            if services:
                cautions.append("FlightAware's selected operating flight differs from the operating flights in this marketed flight's BTS history. The marketed-flight rate must not be treated as a measured rate for the newly identified operator.")
        if not selected_history:
            cautions.append("No usable BTS history for the selected operating flight has been established. Current operational status cannot fill that historical gap.")
        if chosen.get("cancelled_or_untracked") is True:
            cautions.append("FlightAware marks this flight cancelled or no longer tracked. That flag is not always an airline cancellation; confirm with the airline.")
        if chosen.get("diverted") is True:
            cautions.append("FlightAware reports a diversion for the matched departure.")
    else:
        cautions.append("The exact selected departure's operating identity remains unverified by AeroAPI. Recent same-number/route observations do not establish a future assignment.")
    if aeroapi.get("partial_results"):
        cautions.append("Only one AeroAPI results page was retrieved to limit usage. Recent coverage may be incomplete.")
    if aeroapi.get("conflicting_records_excluded"):
        cautions.append("Conflicting FlightAware identity or duplicate records were excluded.")

    parts = []
    if chosen:
        parts.append(f"FlightAware matched this departure to operating flight {chosen['operating_ident']}.")
    else:
        parts.append(aeroapi.get("reason", "FlightAware evidence is unavailable."))
    if selected_history:
        stats = selected_history["overall"]
        rate = stats["historical_delay_rate"]
        parts.append(f"For that operating flight and route, BTS contains {stats['scheduled_flights']} historical operations"
                     + (f", with {stats['delayed_arrivals_15_minutes_or_more']} of {stats['completed_with_arrival_delay']} measured eligible arrivals 15+ minutes late ({rate:.1%})." if rate is not None else "; an arrival-delay rate is unavailable."))
    elif chosen:
        parts.append("A BTS historical baseline for that selected operating flight is unavailable.")
    elif history.get("historical_identity_verified") and history.get("available"):
        parts.append(f"BTS separately provides {history['overall']['scheduled_flights']} operations for the marketed flight's historical service; its relationship to this departure is not fully verified.")
    else:
        parts.append("A verified historical baseline for the selected departure has not been established.")
    for group in aeroapi.get("recent", {}).get("groups", []):
        if chosen and group["operating_ident"] != chosen["operating_ident"]:
            continue
        if group["observed_delay_rate"] is not None:
            parts.append(f"Recent AeroAPI records for {group['operating_ident']} show {group['arrived_15_minutes_or_more_late']} of {group['measured_gate_arrivals']} measured gate arrivals 15+ minutes late ({group['observed_delay_rate']:.1%}); this is a small recent sample, not a forecast.")
    return {"source": "BTS + FlightAware AeroAPI", "selected_flight_verified": matched,
            "traveler": traveler_view(flight, history, aeroapi, selected_history),
            "selected_operator_history": selected_history, "summary": " ".join(parts), "cautions": cautions,
            "pooled_delay_rate_available": False,
            "notice": "BTS long-term statistics and AeroAPI recent observations/live estimates remain separate. They are not averaged, counted twice, or converted into a validated delay probability."}
