"use strict";
const $ = (id) => document.getElementById(id);
const state = {result: null, selected: null, airports: new Map(), status: null, chatBusy: false, version: 0, visibleCount: 6, activeAlternative: null, reportBusy: false};
const money = (value) => typeof value === "number" ? new Intl.NumberFormat("en-US", {style:"currency",currency:"USD",maximumFractionDigits:0}).format(value) : "—";
const el = (tag, className, text) => { const node=document.createElement(tag); if(className) node.className=className; if(text!==undefined) node.textContent=text; return node; };
const flightTime = (value) => {const time=value.slice(11,16).split(":"); const h=Number(time[0]); return `${h%12||12}:${time[1]} ${h<12?"am":"pm"}`;};
const friendlyDate = (value) => new Date(value.slice(0,10)+"T12:00:00").toLocaleDateString("en-US",{month:"short",day:"numeric"});
const duration = (minutes) => typeof minutes === "number" ? `${Math.floor(minutes/60)}h ${minutes%60}m` : "Duration unavailable";
const arrivalLabel = (flight) => {const days=Math.round((new Date(flight.arrival_local.slice(0,10)+"T00:00:00Z")-new Date(flight.departure_local.slice(0,10)+"T00:00:00Z"))/86400000);return flight.destination+(days?` (${days>0?"+":""}${days}d)`:"");};

async function api(path, body) {
  const response=await fetch(path,body===undefined?{}:{method:"POST",headers:{"Content-Type":"application/json","X-Delay-Aware":"local"},body:JSON.stringify(body)});
  const data=await response.json();
  if(!response.ok) throw new Error(data.error||"This request could not be completed.");
  return data;
}
function contextPayload() { return {search_id:state.result.search_id,flight_index:state.selected,...(state.activeAlternative?{alternative_id:state.activeAlternative.alternative_id}:{})}; }
function journeyId(flight) { return JSON.stringify((flight.legs||[flight]).map(l=>[l.flight_number,l.origin,l.destination,l.departure_local,l.arrival_local])); }
function selectedFlight() { return (state.activeAlternative||state.result).flights[state.selected]; }
function stopLabel(flight) { return flight.stops===1?`1 connection · ${flight.layovers[0].airport}`:"Nonstop"; }
function ticketLabel(flight) { const status=flight.ticket_check?.status||flight.ticket_status; return ({sold_together:"Sold-together offer found",unavailable:"Ticket type could not be checked",separate_tickets:"Separate tickets — excluded",over_budget:"Updated fare exceeds budget"})[status]||"Ticket type not yet checked"; }
function connectionText(flight) { return (flight.layovers||[]).map(l=>`${duration(l.duration_minutes)} at ${l.airport}${l.overnight?" · Overnight":""} · ${friendlyDate(l.arrival_local)} ${flightTime(l.arrival_local)} → ${friendlyDate(l.departure_local)} ${flightTime(l.departure_local)}`).join(" · "); }
function updateAirportNames() {
  for(const field of ["origin","destination"]) { const code=$(field).value.trim().toUpperCase(); $(field+"-name").textContent=state.airports.get(code)?.name||"Choose an airport code or suggestion"; }
}
function setError(error) { $("search-error").textContent=error||""; $("search-error").hidden=!error; }
function setChatEnabled() {
  const enabled=state.selected!==null && Boolean(state.status?.models.length) && !state.chatBusy && !state.reportBusy;
  $("model").disabled=state.chatBusy||state.reportBusy;$("message").disabled=!enabled; $("send-button").disabled=!enabled;
  $("message").placeholder=state.selected===null?"Select a flight to start a conversation…":!state.status?.models.length?"Start Ollama to use your assistant…":"Ask about this flight…";
  document.querySelectorAll("#quick-prompts button").forEach(button=>button.disabled=!enabled);
}
function appendMessage(role,text,tools=[]) {
  const node=el("div","chat-message "+role); node.append(el("span","message-label",role==="user"?"YOU":"FLIGHT ASSISTANT"));
  const body=el("div");
  for(const part of text.split(/(\*\*[^*]+\*\*)/g)) body.append(part.startsWith("**")&&part.endsWith("**")?el("strong","",part.slice(2,-2)):document.createTextNode(part));
  node.append(body);
  if(tools.length) {const trace=el("details","tool-trace");trace.append(el("summary","","Details checked"),el("div","",tools.map(tool=>`${tool.status==="unavailable"?"○":"✓"} ${tool.source||tool.tool.replaceAll("_"," ")}`).join(" · ")));node.append(trace);}
  $("conversation").append(node); $("conversation").scrollTop=$("conversation").scrollHeight; return node;
}
function renderResults() {
  const filter=$("itinerary-type").value;
  const flights=state.result.flights.map((flight,index)=>({...flight,index})).filter(f=>f.ticket_check?.eligible!==false&&(filter==="all"||(filter==="nonstop"?f.stops!==1:f.stops===1)));
  $("result-meta").textContent=`${flights.length} options shown of ${state.result.flights.length} returned · ${friendlyDate(state.result.query.outbound_date)} · ${({all:"Nonstop & one connection",nonstop:"Nonstop only",one_connection:"One connection only"})[filter]} · ${state.result.cached?"Cached search":"Live search"}`;
  $("result-notice").textContent=state.result.notice;$("result-notice").hidden=false;
  const sort=$("sort").value;
  flights.sort((a,b)=>sort==="departure"?a.departure_local.localeCompare(b.departure_local):sort==="duration"?(a.duration_minutes??Infinity)-(b.duration_minutes??Infinity):(a.price??Infinity)-(b.price??Infinity));
  $("results").replaceChildren();
  if(!flights.length) { const empty=el("div","empty-state"); empty.append(el("h3","","No matching flights returned"),el("p","","Try another date, route, or maximum fare. We haven’t relaxed your filters.")); $("results").append(empty); return; }
  for(const flight of flights.slice(0,state.visibleCount)) {
    const card=el("article","flight-card"+(!state.activeAlternative&&state.selected===flight.index?" selected":""));
    const top=el("div","flight-card-top"); top.append(el("span","airline-badge",flight.flight_number.split(" ")[0]),el("span","",flight.airline||"Airline"),el("span","flight-number",flight.flight_number),el("span","nonstop-badge",flight.stops===1?"1 CONNECTION":"NONSTOP"));
    const main=el("div","flight-card-main");
    const from=el("div","flight-time",flightTime(flight.departure_local)); from.append(el("span","airport-code",flight.origin));
    const line=el("div","flight-duration",duration(flight.duration_minutes)); line.append(el("div","flight-line"),el("span","",stopLabel(flight)));
    const to=el("div","flight-time",flightTime(flight.arrival_local)); to.append(el("span","airport-code",arrivalLabel(flight)));
    const price=el("div","flight-price",money(flight.ticket_check?.price??flight.price)); price.append(el("small","",flight.ticket_check?.status==="sold_together"?"checked fare · USD":flight.price===null?"Price unavailable":"quoted fare · USD")); main.append(from,line,to,price);
    const bottom=el("div","flight-card-bottom"); bottom.append(el("span","history-label","Explore history & travel conditions"));
    const button=el("button","view-flight",!state.activeAlternative&&state.selected===flight.index?"Selected ✓":"View flight ↗"); button.disabled=state.reportBusy;button.setAttribute("aria-label",`View ${flight.flight_number}`); button.onclick=()=>selectFlight(flight.index); bottom.append(button); card.append(top,main);
    if(flight.stops===1)card.append(el("p","connection-caption",connectionText(flight)),el("p","ticket-caption",ticketLabel(flight)));
    card.append(bottom); $("results").append(card);
  }
  if(flights.length>state.visibleCount){const more=el("button","subtle-button",`Show ${Math.min(6,flights.length-state.visibleCount)} more flights`);more.onclick=()=>{state.visibleCount+=6;renderResults();};$("results").append(more);}
}
async function selectFlight(index,alternative=null) {
  state.reportBusy=true;state.activeAlternative=alternative;state.selected=index; state.version++; const version=state.version;
  const flight=selectedFlight(); renderResults(); $("report").hidden=false; $("report").replaceChildren(el("p","","Checking ticket details and flight history…")); $("alternatives").hidden=true;
  $("selected-context").hidden=false; $("selected-context").replaceChildren(el("strong","",`${flight.flight_number} · ${flight.origin} → ${flight.destination}`),el("span","",`${friendlyDate(flight.departure_local)} · ${flightTime(flight.departure_local)} departure · ${money(flight.price)}`));
  $("conversation").replaceChildren(); appendMessage("assistant",`You’ve selected ${flight.flight_number}. What would you like to explore: its history, the weather, or other flight options?`); $("quick-prompts").hidden=false; setChatEnabled();
  if(state.status?.models.length)$("assistant-status").textContent=`Ready to explore ${flight.flight_number}`;
  try {const report=await api("/api/report",contextPayload()); if(version!==state.version)return; flight.ticket_check=report.ticket;for(const option of state.result.flights)if(journeyId(option)===journeyId(flight))option.ticket_check=report.ticket;renderResults();renderReport(report);$("report").scrollIntoView({behavior:"smooth",block:"nearest"});}
  catch(error) {if(version===state.version)$("report").replaceChildren(el("p","error",error.message));}
  finally{state.reportBusy=false;setChatEnabled();if(state.result)renderResults();}
}
function timingLabel(minutes) {
  if(typeof minutes!=="number")return "Unavailable";
  if(Math.abs(minutes)<0.5)return "On time";
  const rounded=Math.round(Math.abs(minutes));
  return `${rounded} min ${minutes<0?"early":"late"}`;
}
function timingCard(title,period,recent) {
  const card=el("section","timing-card");card.append(el("h4","",title));
  const timing=period?.arrival_timing;
  if(!timing?.measured_arrivals) {
    card.append(el("p","timing-empty",recent?"Recent measured arrivals aren’t available yet.":"Not enough confirmed historical arrivals."));return card;
  }
  const first=recent?period.first_measured_departure:period.first_date,last=recent?period.last_measured_departure:period.last_date;
  const periodDate=(value)=>new Date(value.slice(0,10)+"T12:00:00").toLocaleDateString("en-US",{month:"short",day:recent?"numeric":undefined,year:recent?undefined:"numeric"});
  if(first&&last)card.append(el("p","timing-period",`${periodDate(first)} – ${periodDate(last)}`));
  card.append(el("p","timing-average"+(timing.mean_minutes<0?" is-early":""),timingLabel(timing.mean_minutes)),el("p","timing-label","AVERAGE ARRIVAL"));
  card.append(el("p","timing-median",`Typical: ${timingLabel(timing.median_minutes)} (median)`));
  const n=timing.measured_arrivals;
  for(const [count,label] of [[timing.early_arrivals,"Arrived early"],[timing.on_time_arrivals+timing.under_15_minutes_late,"On time to <15 min late"],[timing.at_least_15_minutes_late,"15+ min late"]]) {
    const row=el("div","timing-count");row.append(el("span","",label),el("strong","",`${count}/${n}`));card.append(row);
  }
  card.append(el("p","timing-sample",`${n} measured arrivals${recent?" · Small recent sample":""}`));return card;
}
function renderReport(report,panel=$("report"),legNumber=null) {
  if(report.journey)return renderJourneyReport(report,panel);
  const view=report.analysis?.traveler;
  panel.replaceChildren(el("p","eyebrow",legNumber?`FLIGHT ${legNumber} · HISTORY & RECENT PERFORMANCE`:"YOUR FLIGHT, AT A GLANCE"),el("h3","",`${report.flight.flight_number} · ${report.flight.origin} → ${report.flight.destination}`));
  if(view) {
    const status=el("div","flight-status-row");status.append(el("span","flight-status"+(view.attention?" needs-attention":""),view.status),el("span","",friendlyDate(report.flight.departure_local)));panel.append(status);
    const journey=el("div","report-journey");
    for(const [name,label] of [["departure","Departure"],["arrival","Arrival"]]) {
      const item=view[name],column=el("div","report-time");
      column.append(el("span","",label),el("strong","",item.time?flightTime(item.time):"—"),el("small","",`${item.kind} · ${item.airport}`));
      if(item.time&&item.time.slice(0,10)!==report.flight.departure_local.slice(0,10))column.append(el("small","",friendlyDate(item.time)));
      journey.append(column);
    }
    panel.append(journey);
    if(view.arrival_message)panel.append(el("p","arrival-outlook",view.arrival_message));
    if(view.status_note)panel.append(el("p",view.attention?"error":"",view.status_note));
    const comparison=el("div","timing-comparison");
    comparison.append(timingCard("Historical timing",view.performance,false),timingCard("Recent timing",view.recent,true));
    panel.append(comparison);
    const past=view.performance;
    if(past?.cancellation_rate!==null&&past?.cancellation_rate!==undefined)panel.append(el("p","source-footnote",`Historical cancellations: ${(past.cancellation_rate*100).toFixed(1)}% · ${past.cancelled_flights} of ${past.scheduled_flights} scheduled flights.`));
    if(view.recent?.latest_arrivals?.length) {
      const latest=el("div","latest-arrivals");latest.append(el("p","timing-label","LAST FEW ARRIVALS"));
      const list=el("div","arrival-list");
      for(const arrival of view.recent.latest_arrivals) {
        const item=el("div","arrival-chip");item.append(el("span","",friendlyDate(arrival.departure_date)),el("strong",arrival.minutes<0?"is-early":"",timingLabel(arrival.minutes)));
        item.title=`Scheduled gate arrival: ${friendlyDate(arrival.scheduled_arrival_local)} ${flightTime(arrival.scheduled_arrival_local)}. Actual: ${friendlyDate(arrival.actual_arrival_local)} ${flightTime(arrival.actual_arrival_local)}. Destination local time; date label is departure day.`;
        list.append(item);
      }
      latest.append(list);panel.append(latest);
    }
    panel.append(el("p","source-footnote",`All times are local to each airport. Past performance is not a prediction.${view.updated_at?` Updated ${new Date(view.updated_at).toLocaleTimeString("en-US",{hour:"numeric",minute:"2-digit"})}.`:""}`));
  }
  if(!legNumber){const compare=el("button","subtle-button","Explore other flights ↗");compare.onclick=()=>loadAlternatives(compare);panel.append(compare);}
  const details=el("details","analysis-details");details.append(el("summary","","Sources & calculation details"));
  const evidence=el("div","analysis-evidence");renderEvidenceDetails(report,evidence);details.append(evidence);panel.append(details);
}
function renderJourneyReport(report,panel) {
  const flight=report.flight,layover=report.connection,ticket=report.ticket;
  panel.replaceChildren(el("p","eyebrow","YOUR JOURNEY, AT A GLANCE"),el("h3","",`${flight.origin} → ${layover.airport} → ${flight.destination}`),el("p","",`${friendlyDate(flight.departure_local)} · ${duration(flight.duration_minutes)} total · ${flight.flight_number}`));
  const journey=el("div","report-journey");
  for(const [label,time,airport] of [["Scheduled departure",flight.departure_local,flight.origin],["Scheduled final arrival",flight.arrival_local,flight.destination]]) {
    const item=el("div","report-time");item.append(el("span","",label),el("strong","",flightTime(time)),el("small","",`${friendlyDate(time)} · ${airport}`));journey.append(item);
  }
  panel.append(journey);
  const ticketBox=el("div","ticket-summary"+(ticket.eligible===false?" needs-attention":""));
  ticketBox.append(el("strong","",ticketLabel({...flight,ticket_check:ticket})),el("p","",ticket.notice));
  if(ticket.price!==undefined)ticketBox.append(el("p","",`${money(ticket.price)} · ${ticket.seller}${ticket.fare_name?` · ${ticket.fare_name}`:""}${ticket.price!==flight.price?` · Search quote was ${money(flight.price)}`:""}`));
  if(ticket.reason)ticketBox.append(el("p","source-footnote",ticket.reason));
  panel.append(ticketBox,el("h4","","Your connection"),el("p","connection-summary",connectionText(flight)),el("p","source-footnote",layover.notice));
  const legs=el("div","journey-legs");
  report.legs.forEach((leg,index)=>{const section=el("section","journey-leg");renderReport({...leg,history_coverage:report.history_coverage,history_sync:report.history_sync,marketing_sync:report.marketing_sync},section,index+1);legs.append(section);});
  panel.append(legs);
  const compare=el("button","subtle-button","Explore other journeys ↗");compare.onclick=()=>loadAlternatives(compare);panel.append(compare);
}
function renderEvidenceDetails(report,panel) {
  const history=report.history;
  renderCombinedEvidence(panel,report);
  panel.append(el("h4","","BTS · Historical marketed-flight service"));
  const stats=el("div","stat-grid"), overall=history.overall;
  const rows=history.available?[[overall.scheduled_flights,"OBSERVED FLIGHTS"],[overall.historical_delay_rate===null?"—":`${(overall.historical_delay_rate*100).toFixed(1)}%`,"ARRIVED 15+ MIN LATE"],[overall.cancelled_flights,"CANCELLATIONS"]]:[["—","OBSERVED FLIGHTS"],["—","HISTORICAL DELAYS"],["—","CANCELLATIONS"]];
  for(const [value,label]of rows) {const stat=el("div","stat");stat.append(el("span","stat-value",value),el("span","stat-label",label));stats.append(stat);} panel.append(stats);
  const missing=history.reason_code==="flight_not_matched"?"BTS history is loaded, but no records match this flight number and route. Flight numbers and operating airlines can change. Reliability estimates for this flight remain unavailable.":history.reason_code==="history_not_loaded"?"Flight-level BTS history has not been loaded for this trip yet. Reliability estimates remain unavailable until matching records are imported.":history.reason||"This flight cannot yet be matched to historical records.";
  if(history.match_method==="bts_marketing_operating_key") {
    const currentIdentity=report.analysis?.selected_flight_verified?"The selected departure was matched separately by FlightAware; its operating-flight baseline is described above.":"The operator of your selected departure is not yet verified by FlightAware.";
    panel.append(el("p","",history.available?`Historical marketing-to-operating relationship confirmed by BTS for ${history.identity.requested_airline} ${history.identity.flight_number} on ${history.identity.origin}–${history.identity.destination}. Observations: ${history.observed_first_date} to ${history.observed_last_date}, across ${history.months_with_observations.length} months. ${currentIdentity}`:missing));
    for(const service of history.operating_services||[]) {
      panel.append(el("p","",`Operating flight ${service.operating_carrier} ${service.operating_flight_number} · ${service.overall.scheduled_flights} historical operations · ${service.observed_first_date} to ${service.observed_last_date}`));
    }
    const join=history.join_coverage;
    panel.append(el("p","source-footnote",`${join.matched_reporting_operations} operations matched across both BTS datasets using date, operating airline and flight number, route, and scheduled departure time. ${join.marketing_only_operations} additional operations come from Marketing Carrier data only. Each operation is counted once. ${join.outcome_conflicts_excluded} conflicting outcomes and ${join.ambiguous_departures_excluded} ambiguous departures excluded.`));
  } else {
    panel.append(el("p","",history.available?`Candidate match by numeric flight number and route under BTS reporting airline ${history.matched_reporting_carrier}; the airline prefix was not used to filter history. Records observed from ${history.observed_first_date} to ${history.observed_last_date}, across ${history.months_with_observations.length} months. No explicit marketing-to-operating relationship has been established for these records, so these figures are not a verified prediction for this flight.`:missing));
  }
  if(history.available)panel.append(el("p","",`${overall.delayed_arrivals_15_minutes_or_more} of ${overall.completed_with_arrival_delay} measured, non-cancelled, non-diverted arrivals were 15+ minutes late. ${overall.diverted_flights} diversions; ${overall.completed_without_delay_measurement} completed flights without a delay measurement. Cancellations and diversions are excluded from the arrival-delay percentage.`));
  for(const candidate of history.carrier_candidates||[]) {
    const rate=candidate.overall.historical_delay_rate;
    panel.append(el("p","",`BTS reporting airline ${candidate.reporting_carrier}: ${candidate.overall.scheduled_flights} observed flights · ${rate===null?"Delay rate unavailable":`${Math.round(rate*100)}% arrived 15+ min late`} · ${candidate.overall.cancelled_flights} cancellations. Observations: ${candidate.observed_first_date} to ${candidate.observed_last_date}. Identity unverified.`));
  }
  panel.append(el("p","source-footnote",`${historyCoverageText(report)} History is an observed rate, not a weather-adjusted forecast.`));
}
function renderCombinedEvidence(panel,report) {
  const live=report.aeroapi,analysis=report.analysis;
  if(!live||!analysis)return;
  panel.append(el("h4","","BTS + FlightAware · Selected flight assessment"));
  panel.append(el("p","",live.reason_code==="not_configured"?"FlightAware is not connected on this Mac yet. BTS history remains available below.":analysis.summary));
  if(live.selected_flight_verified) {
    const flight=live.selected;
    panel.append(el("p","",`Operating flight ${flight.operating_ident} · FlightAware status: ${flight.status||"Unavailable"}`));
    for(const [kind,label] of [["scheduled","Scheduled"],["estimated","Estimated"],["actual","Actual"]]) {
      const out=flight[`${kind}_out_local`],arrival=flight[`${kind}_in_local`];
      if(out||arrival)panel.append(el("p","",`${label} gate times: ${out?`${friendlyDate(out)} ${flightTime(out)} ${flight.origin}`:"Departure unavailable"} → ${arrival?`${friendlyDate(arrival)} ${flightTime(arrival)} ${flight.destination}`:"Arrival unavailable"}. Each time is local to its airport.`));
    }
    const actual=flight.actual_arrival_delay_minutes,estimate=flight.estimated_arrival_delay_minutes;
    if(actual!==null)panel.append(el("p","",`Measured gate arrival: ${Math.abs(actual).toFixed(0)} minutes ${actual<0?"early":"late"}.`));
    else if(estimate!==null)panel.append(el("p","",`Estimated gate arrival: ${Math.abs(estimate).toFixed(0)} minutes ${estimate<0?"early":"late"}. This is a current estimate, not an observed outcome.`));
  }
  if(live.recent?.available) {
    panel.append(el("h4","","FlightAware · Recent returned operations"));
    for(const group of live.recent.groups) {
      const measured=group.measured_gate_arrivals;
      const rate=group.observed_delay_rate;
      panel.append(el("p","",`${group.operating_ident}: ${group.observed_operations} past-scheduled operations returned; ${rate===null?"no measured arrival-delay rate":`${group.arrived_15_minutes_or_more_late} of ${measured} measured gate arrivals delayed 15+ minutes (${(rate*100).toFixed(1)}%)`}. ${group.cancelled_or_untracked} flagged cancelled or no longer tracked; ${group.diverted} diversions. Period: ${friendlyDate(group.first_departure)}–${friendlyDate(group.last_departure)}.`));
    }
    panel.append(el("p","source-footnote",live.recent.notice));
  }
  for(const caution of analysis.cautions)panel.append(el("p","source-footnote",caution));
  panel.append(el("p","source-footnote",`${live.fetched_at?`FlightAware retrieved ${new Date(live.fetched_at).toLocaleString()}${live.cached?" · cached":""}. `:""}${analysis.notice}`));
}
function historyCoverageText(data) {
  const coverage=data.history_coverage,sync=data.history_sync;
  const count=coverage?.loaded_months?.length||0;
  const dates=coverage?.has_records?` Records span ${coverage.first_date} to ${coverage.last_date}.`:"";
  if(["discovering","downloading","importing"].includes(sync?.state))return `BTS history is loading: ${sync.completed_months.length}/${sync.target_months} months complete.${dates}`;
  if(sync?.state==="error")return `BTS history loading paused after ${count}/${sync.target_months} months. Rerun the history sync to continue.${dates}`;
  const marketing=coverage?.marketing;
  const marketingMonths=marketing?.loaded_months?.length||0;
  const enrichment=data.marketing_sync;
  const mapping=marketingMonths?` Marketing Carrier: ${marketingMonths} months loaded.`:" Marketing-to-operating history has not been imported yet.";
  const progress=["discovering","downloading","importing"].includes(enrichment?.state)?` Marketing import in progress (${enrichment.completed_months.length}/${enrichment.target_months} months).`:enrichment?.state==="error"?" Marketing import paused; resume it to complete coverage.":"";
  return coverage?.has_records?`BTS Reporting Carrier: ${count} downloaded months loaded.${dates}${mapping}${progress}`:"Flight-level BTS Reporting Carrier history has not been loaded yet.";
}
async function loadAlternatives(button) {
  const version=state.version; button.disabled=true; button.textContent="Checking dates and nearby airports…";
  const panel=$("alternatives");panel.hidden=false; panel.replaceChildren(el("p","","Searching the selected date and one day either side…"));
  try {
    const result=await api("/api/alternatives",contextPayload());if(version!==state.version)return;
    panel.replaceChildren(el("p","eyebrow","ROOM TO EXPLORE"),el("h3","","Other ways to make the trip"),el("p","",result.notice));
    if(!result.flights.length)panel.append(el("p","","No other matching flight options were returned within your filters."));
    const list=el("div","alternative-list");panel.append(list);renderAlternativeRows(result,list);
    for(const error of result.errors)panel.append(el("p","error",`${friendlyDate(error.date)}: ${error.reason}`));
    panel.append(el("p","source-footnote",`${result.dates_checked.length} dates checked. Nearby airports are within 50 straight-line miles. Fares exclude unquoted fees and ground transport.`));
  }catch(error){if(version===state.version)panel.replaceChildren(el("p","error",error.message));}
  finally{button.disabled=false;button.textContent="Compare nearby dates & airports ↗";}
}
function renderAlternativeRows(result,panel,count=8) {
  panel.replaceChildren();
  result.flights.slice(0,count).forEach((flight,index)=>{
    const card=el("div","alt-card"),detail=el("div");
    detail.append(el("strong","",`${flight.flight_number} · ${flight.origin} → ${flight.destination}`),el("p","",`${friendlyDate(flight.departure_local)} · ${flightTime(flight.departure_local)} → ${friendlyDate(flight.arrival_local)} ${flightTime(flight.arrival_local)} · ${duration(flight.duration_minutes)} · ${stopLabel(flight)}`));
    if(flight.stops===1)detail.append(el("p","",connectionText(flight)),el("p","ticket-caption",ticketLabel(flight)));
    const changes=[];if(flight.date_difference_days)changes.push(flight.date_difference_days<0?"One day earlier":"One day later");if(flight.changed_origin)changes.push("Different departure airport");if(flight.changed_destination)changes.push("Different arrival airport");if(changes.length)detail.append(el("p","",changes.join(" · ")));
    const choose=el("button","view-flight","View journey ↗");choose.onclick=()=>selectFlight(index,result);detail.append(choose);
    const price=el("div","alt-price",money(flight.price));if(flight.price_difference!==null)price.append(el("small","",flight.price_difference===0?"Same quoted fare":`${money(Math.abs(flight.price_difference))} ${flight.price_difference<0?"less":"more"}`));card.append(detail,price);panel.append(card);
  });
  if(result.flights.length>count){const more=el("button","subtle-button",`Show ${Math.min(8,result.flights.length-count)} more alternatives (${result.flights.length} total)`);more.onclick=()=>renderAlternativeRows(result,panel,count+8);panel.append(more);}
}
async function sendQuestion(question) {
  if(state.chatBusy||state.selected===null||!question.trim())return;
  const version=state.version;state.chatBusy=true;setChatEnabled();$("assistant-status").textContent="Checking your selected flight…";$("message").value="";appendMessage("user",question);
  const pending=el("div","chat-message pending","Your local assistant is checking…");$("conversation").append(pending);$("conversation").scrollTop=$("conversation").scrollHeight;
  try{const response=await api("/api/chat",{...contextPayload(),message:question,model:$("model").value});if(version===state.version)appendMessage("assistant",response.answer,response.tools_used);}
  catch(error){if(version===state.version)appendMessage("assistant",error.message);}
  finally{pending.remove();state.chatBusy=false;setChatEnabled();if(state.selected!==null&&state.status?.models.length)$("assistant-status").textContent=`Ready to explore ${selectedFlight().flight_number}`;}
}
$("search-form").addEventListener("submit",async(event)=>{
  event.preventDefault();setError("");const button=$("search-button");button.disabled=true;button.textContent="Finding your options…";state.version++;state.visibleCount=6;state.selected=null;state.activeAlternative=null;state.result=null;setChatEnabled();$("selected-context").hidden=true;$("quick-prompts").hidden=true;$("report").hidden=true;$("alternatives").hidden=true;$("sort-label").hidden=true;$("result-notice").hidden=true;$("results").replaceChildren(el("div","loading","Searching available journeys…"));
  $("conversation").replaceChildren();appendMessage("assistant","Select a flight from this search to explore its history, weather, or alternatives.");if(state.status?.models.length)$("assistant-status").textContent="Ready when you select a flight";
  try{const budget=$("budget").value; const data=await api("/api/search",{origin:$("origin").value.trim().toUpperCase(),destination:$("destination").value.trim().toUpperCase(),date:$("departure").value,itinerary_type:$("itinerary-type").value,max_price:budget===""?null:Number(budget)});state.result=data;$("results-title").textContent=`${data.query.departure_id} to ${data.query.arrival_id}`;$("result-meta").textContent=`${data.flights.length} options returned · ${friendlyDate(data.query.outbound_date)} · ${({all:"Nonstop & one connection",nonstop:"Nonstop only",one_connection:"One connection only"})[data.query.itinerary_type]} · ${data.cached?"Cached search":"Live search"} · Retrieved ${new Date(data.fetched_at).toLocaleTimeString("en-US",{hour:"numeric",minute:"2-digit"})}`;$("sort-label").hidden=false;renderResults();}
  catch(error){setError(error.message);$("results").replaceChildren(el("div","empty-state","Your search couldn’t be completed. Check the message above and try again."));}
  finally{button.disabled=false;button.replaceChildren(document.createTextNode("Find flights →"));}
});
$("sort").onchange=()=>state.result&&renderResults();
$("itinerary-type").onchange=()=>{state.visibleCount=6;if(!state.result)return;if(state.result.query.itinerary_type==="all")renderResults();else $("search-form").requestSubmit();};
$("swap-button").onclick=()=>{const from=$("origin").value;$("origin").value=$("destination").value;$("destination").value=from;updateAirportNames();};
$("origin").oninput=updateAirportNames;$("destination").oninput=updateAirportNames;
$("chat-form").onsubmit=(event)=>{event.preventDefault();sendQuestion($("message").value);};
$("message").addEventListener("keydown",event=>{if(event.key==="Enter"&&!event.shiftKey){event.preventDefault();sendQuestion($("message").value);}});
document.querySelectorAll("[data-prompt]").forEach(button=>button.onclick=()=>sendQuestion(button.dataset.prompt));
$("about-button").onclick=()=>$("about-dialog").showModal();$("close-about").onclick=()=>$("about-dialog").close();
$("assistant-toggle").onclick=()=>{const open=$("assistant-panel").classList.toggle("is-open");$("assistant-toggle").setAttribute("aria-expanded",String(open));$("assistant-toggle").textContent=open?"Close assistant ×":"Ask assistant ↗";};
$("model").onchange=()=>{state.version++;$("conversation").replaceChildren();appendMessage("assistant",`Using ${$("model").value}. Select a flight or ask a question to continue.`);};
async function start(){
  try{const [status,reference]=await Promise.all([api("/api/status"),api("/api/airports")]);state.status=status;for(const airport of reference.airports){state.airports.set(airport.iata,airport);const option=el("option","",airport.name);option.value=airport.iata;$("airport-options").append(option);}updateAirportNames();$("departure").min=status.today;$("departure").max=status.last_departure_date;const defaultDate=new Date(status.today+"T12:00:00");defaultDate.setDate(defaultDate.getDate()+7);$("departure").value=`${defaultDate.getFullYear()}-${String(defaultDate.getMonth()+1).padStart(2,"0")}-${String(defaultDate.getDate()).padStart(2,"0")}`;$("model").value=status.default_model;for(const option of $("model").options)option.disabled=!status.models.includes(option.value);if(status.models.length&&!status.models.includes($("model").value))$("model").value=status.models[0];$("assistant-status").textContent=status.models.length?"Ready when you select a flight":"Ollama is offline — start it on your Mac";$("history-setup-note").textContent=historyCoverageText(status);if(!status.search_ready)setError("Add your SerpApi key to the local configuration to enable flight search.");setChatEnabled();}
  catch(error){setError(error.message);$("assistant-status").textContent="Could not connect to the local application";}
}
start();
