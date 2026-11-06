"""Journey behavior: routing, ticket evidence, both histories, and alternatives."""

from copy import deepcopy
from datetime import date
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from delay_aware.application import Application
from delay_aware.agent import answer
from delay_aware.core import DataError, LocalStore, Settings
from delay_aware.itineraries import journey_key, normalize_offer
from delay_aware.search import FlightSearch
from delay_aware.tickets import TicketCheck

AIRPORTS = '''iata_code,iso_country,latitude_deg,longitude_deg,name,ident,scheduled_service,type
BOS,US,42.3656,-71.0096,Logan,KBOS,yes,large_airport
EWR,US,40.6895,-74.1745,Newark,KEWR,yes,large_airport
ORD,US,41.9742,-87.9073,OHare,KORD,yes,large_airport
MDW,US,41.786,-87.7524,Midway,KMDW,yes,large_airport
LHR,GB,51.47,-0.45,Heathrow,EGLL,yes,large_airport
'''
TODAY = date(2026, 10, 23)
DAY = date(2026, 10, 25)
QUERY = {'engine': 'google_flights', 'departure_id': 'BOS', 'arrival_id': 'ORD',
         'type': 2, 'adults': 1, 'travel_class': 1, 'currency': 'USD', 'gl': 'us', 'hl': 'en',
         'itinerary_type': 'nonstop', 'max_price': 250}


def leg(number, origin, destination, out, arrival):
    return {'flight_number': number, 'airline': 'United', 'duration': 90,
            'departure_airport': {'id': origin, 'time': out},
            'arrival_airport': {'id': destination, 'time': arrival}}


def connection():
    return {'flights': [leg('UA 1', 'BOS', 'EWR', '2026-10-25 08:00', '2026-10-25 09:30'),
                        leg('UA 2', 'EWR', 'ORD', '2026-10-25 10:00', '2026-10-25 11:30')],
            'layovers': [{'id': 'EWR', 'duration': 30}], 'price': 200,
            'total_duration': 210, 'booking_token': 'opaque-token'}


def direct():
    return {'flights': [leg('UA 3', 'BOS', 'ORD', '2026-10-25 08:00', '2026-10-25 10:00')], 'price': 180}


def response(*offers):
    return {'search_metadata': {'status': 'Success'}, 'best_flights': list(offers)}


class JourneySetup(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'data').mkdir()
        (self.root / 'data' / 'airports.csv').write_text(AIRPORTS)
        self.app = Application(Settings(self.root, serpapi_key='do-not-leak'))
        self.app.today = Mock(return_value=TODAY)
        self.transport = Mock()
        self.store = LocalStore(self.root / 'test-cache.sqlite')
        self.searcher = FlightSearch(self.app.settings, self.transport, self.store, self.app.airports)
        self.tickets = TicketCheck(self.app.settings, self.transport, self.store, self.app.airports)

    def normalize(self, offer=None):
        return normalize_offer(offer or connection(), ['BOS'], ['ORD'], DAY,
                               self.app.airports.load(), 'all', None)

    def search(self, **kwargs):
        return self.searcher.search('BOS', 'ORD', DAY, today=TODAY, **kwargs)

    def booking(self, options=None):
        return {'search_metadata': {'status': 'Success'}, 'selected_flights': [connection()],
                'booking_options': options if options is not None else [{'together': {'book_with': 'United',
                    'marketed_as': ['UA 1', 'UA 2'], 'price': 220}}]}


class JourneySearchTests(JourneySetup):
    def test_default_returns_both_types_without_ticket_lookup(self):
        self.transport.get_json.return_value = response(direct(), connection())
        result = self.search()
        self.assertEqual([f['stops'] for f in result['flights']], [0, 1])
        self.assertEqual(result['query']['stops'], 2)
        self.assertEqual(result['flights'][1]['ticket_status'], 'not_checked')
        self.transport.get_json.assert_called_once()

    def test_filters_are_enforced_and_cache_keys_separate(self):
        self.transport.get_json.return_value = response(direct(), connection())
        self.assertEqual(len(self.search()['flights']), 2)
        self.assertEqual([f['stops'] for f in self.search(itinerary_type='nonstop')['flights']], [0])
        self.assertEqual([f['stops'] for f in self.search(itinerary_type='one_connection')['flights']], [1])
        self.assertTrue(self.search(itinerary_type='all')['cached'])
        self.assertEqual(self.transport.get_json.call_count, 3)
        for value in ('two_connections', None, [], True):
            with self.subTest(value=value), self.assertRaises(DataError):
                self.search(itinerary_type=value)
        self.assertEqual(self.transport.get_json.call_count, 3)

    def test_rejects_airport_changes_foreign_layovers_extra_legs_and_split_tickets(self):
        bad = []
        for mutate in (
            lambda o: o['flights'][1]['departure_airport'].update(id='MDW'),
            lambda o: (o['flights'][0]['arrival_airport'].update(id='LHR'), o['flights'][1]['departure_airport'].update(id='LHR'), o['layovers'][0].update(id='LHR')),
            lambda o: o['flights'].append(deepcopy(o['flights'][1])),
            lambda o: o.update(separate_tickets=True),
            lambda o: o.update(extensions=['Self-transfer']),
            lambda o: o['flights'][1].pop('flight_number'),
            lambda o: o['layovers'][0].update(duration=-30),
            lambda o: o['layovers'][0].update(id='MDW'),
            lambda o: o['flights'][0]['departure_airport'].update(time='2026-10-26 08:00'),
            lambda o: o['flights'][1]['arrival_airport'].update(id=[]),
        ):
            candidate = connection()
            mutate(candidate)
            bad.append(candidate)
        self.transport.get_json.return_value = response(*bad, connection())
        self.assertEqual(len(self.search()['flights']), 1)

    def test_short_long_and_overnight_layovers_remain_visible(self):
        short = connection()
        short['flights'][1]['departure_airport']['time'] = '2026-10-25 09:40'
        short['layovers'][0]['duration'] = 10
        long = connection()
        long['flights'][1]['departure_airport']['time'] = '2026-10-27 10:00'
        long['flights'][1]['arrival_airport']['time'] = '2026-10-27 11:30'
        long['layovers'][0]['duration'] = 2910
        self.transport.get_json.return_value = response(short, long)
        flights = self.search()['flights']
        self.assertEqual([f['layovers'][0]['duration_minutes'] for f in flights], [10, 2910])
        self.assertTrue(flights[1]['layovers'][0]['overnight'])
        self.assertFalse(flights[0]['layovers'][0]['minimum_connection_time_verified'])

    def test_identity_includes_the_second_leg_and_fare_cap_covers_whole_journey(self):
        other = connection()
        other['flights'][1]['flight_number'] = 'UA 9'
        expensive = connection()
        expensive.update(price=251)
        unknown = connection()
        unknown.pop('price')
        self.transport.get_json.return_value = response(connection(), connection(), other, expensive, unknown)
        flights = self.search(max_price=250)['flights']
        self.assertEqual(len(flights), 2)
        self.assertNotEqual(journey_key(flights[0]), journey_key(flights[1]))


class TicketTests(JourneySetup):
    def test_together_requires_matching_both_flights_and_returns_checked_price(self):
        self.transport.get_json.return_value = self.booking()
        flight = self.normalize()
        result = self.tickets.check(flight, QUERY)
        self.assertEqual(result['status'], 'sold_together')
        self.assertTrue(result['eligible'])
        self.assertEqual(result['price'], 220)
        self.assertTrue(self.tickets.check(flight, QUERY)['cached'])
        self.transport.get_json.assert_called_once()
        persisted = (self.root / 'test-cache.sqlite').read_bytes()
        self.assertNotIn(b'do-not-leak', persisted)
        self.assertNotIn(b'opaque-token', persisted)

    def test_shared_airline_or_absent_split_flag_alone_is_not_ticket_proof(self):
        for options in ([{'together': {'book_with': 'United', 'price': 200}}],
                        [{'separate_tickets': False}], [],
                        [{'together': {'book_with': 'United', 'marketed_as': ['UA 1'], 'price': 200}}]):
            with self.subTest(options=options):
                result = self.tickets.normalize(self.booking(options), self.normalize(), QUERY,
                                               {'status': 'unavailable', 'eligible': None})
                self.assertEqual(result['status'], 'unavailable')

    def test_wrong_second_flight_route_or_time_cannot_verify_ticket(self):
        for field, value in (('flight_number', 'UA 99'), ('departure_airport', {'id': 'EWR', 'time': '2026-10-25 11:00'}),
                             ('arrival_airport', {'id': 'MDW', 'time': '2026-10-25 11:30'})):
            raw = self.booking()
            raw['selected_flights'][0]['flights'][1][field] = value
            result = self.tickets.normalize(raw, self.normalize(), QUERY, {'status': 'unavailable', 'eligible': None})
            self.assertEqual(result['status'], 'unavailable')

    def test_split_offers_never_become_eligible_even_with_together_field(self):
        raw = self.booking([{'separate_tickets': True, 'together': {'book_with': 'United',
                              'marketed_as': ['UA 1', 'UA 2'], 'price': 150}}])
        result = self.tickets.normalize(raw, self.normalize(), QUERY, {'status': 'unavailable', 'eligible': None})
        self.assertEqual(result['status'], 'separate_tickets')
        self.assertFalse(result['eligible'])
        raw['booking_options'].append(self.booking()['booking_options'][0])
        result = self.tickets.normalize(raw, self.normalize(), QUERY, {'status': 'unavailable', 'eligible': None})
        self.assertEqual(result['status'], 'sold_together')
        self.assertEqual(result['price'], 220)

    def test_updated_together_price_rechecks_budget(self):
        raw = self.booking()
        raw['booking_options'][0]['together']['price'] = 251
        result = self.tickets.normalize(raw, self.normalize(), QUERY, {'status': 'unavailable', 'eligible': None})
        self.assertEqual(result['status'], 'over_budget')
        self.assertFalse(result['eligible'])
        raw['booking_options'][0]['together']['price'] = None
        result = self.tickets.normalize(raw, self.normalize(), QUERY, {'status': 'unavailable', 'eligible': None})
        self.assertEqual(result['status'], 'unavailable')

    def test_failed_checks_are_cached_and_quota_still_applies(self):
        self.transport.get_json.side_effect = DataError('Temporarily unavailable')
        self.assertEqual(self.tickets.check(self.normalize(), QUERY)['status'], 'unavailable')
        self.assertTrue(self.tickets.check(self.normalize(), QUERY)['cached'])
        self.transport.get_json.assert_called_once()
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM requests').fetchone()[0], 1)
        other = self.normalize()
        other['booking_token'] = 'different-token'
        with self.store.connect() as db:
            db.executemany('INSERT INTO requests VALUES (?)', [(time.time(),)] * 50)
        result = self.tickets.check(other, QUERY)
        self.assertEqual(result['status'], 'unavailable')
        self.transport.get_json.assert_called_once()

    def test_nonstop_or_missing_token_spends_no_lookup(self):
        self.assertEqual(self.tickets.check(self.normalize(direct()), QUERY)['status'], 'not_applicable')
        flight = self.normalize()
        flight.pop('booking_token')
        self.assertEqual(self.tickets.check(flight, QUERY)['status'], 'unavailable')
        self.transport.get_json.assert_not_called()


class ApplicationJourneyTests(JourneySetup):
    def test_history_fetches_both_legs_and_preserves_missing_second_leg(self):
        self.app.history.report = Mock(side_effect=[{'available': True, 'matched_reporting_carrier': 'UA'},
                                                    {'available': False}])
        result = self.app.history_for(self.normalize())
        self.assertTrue(result['available'])
        self.assertFalse(result['all_legs_available'])
        self.assertEqual(self.app.history.report.call_count, 2)
        self.assertEqual(self.app.history.report.call_args_list[0].args, ('UA', '1', 'BOS', 'EWR'))
        self.assertEqual(self.app.history.report.call_args_list[1].args, ('UA', '2', 'EWR', 'ORD'))
        self.assertFalse(result['legs'][1]['history']['available'])
        self.assertNotIn('historical_delay_rate', result)

    def test_analysis_keeps_both_leg_results_and_separate_identity(self):
        self.app.history_for = Mock(return_value={'available': False})
        self.app.aeroapi.lookup = Mock(side_effect=[{'selected_flight_verified': False}, {'selected_flight_verified': False}])
        result = self.app.analysis_for(self.normalize())
        self.assertTrue(result['journey'])
        self.assertEqual([r['flight']['flight_number'] for r in result['legs']], ['UA 1', 'UA 2'])
        self.assertEqual(self.app.aeroapi.lookup.call_count, 2)
        self.assertFalse(result['connection']['minimum_connection_time_verified'])
        self.assertNotIn('delay_probability', result)

    def test_weather_includes_connection_on_both_dates(self):
        offer = connection()
        offer['flights'][1]['departure_airport']['time'] = '2026-10-26 10:00'
        offer['flights'][1]['arrival_airport']['time'] = '2026-10-26 11:30'
        offer['layovers'][0]['duration'] = 1470
        self.app.weather.for_date = Mock(return_value={'available': False})
        result = self.app.weather_for(self.normalize(offer))
        self.assertEqual([(r['airport'], r['local_date']) for r in result['airports']],
                         [('BOS', '2026-10-25'), ('EWR', '2026-10-25'), ('EWR', '2026-10-26'), ('ORD', '2026-10-26')])

    def test_alternatives_include_all_types_and_more_than_twenty_and_are_selectable(self):
        options = []
        for n in range(30):
            option = connection()
            option['flights'][1]['flight_number'] = f'UA {n+10}'
            options.append(self.normalize(option))
        options.append(self.normalize(direct()))
        self.app.searcher.search_airports = Mock(return_value={'flights': options, 'cached': False, 'fetched_at': '2026-10-23'})
        session = {'result': {'query': QUERY, 'flights': [self.normalize()]}, 'alternatives': {},
                   'created': time.monotonic(), 'lock': threading.Lock()}
        self.app.sessions['saved'] = session
        result = self.app.alternatives_for(session, self.normalize())
        self.assertEqual(len(result['flights']), 31)
        self.assertIn(0, [f['stops'] for f in result['flights']])
        self.assertIn(1, [f['stops'] for f in result['flights']])
        for call in self.app.searcher.search_airports.call_args_list:
            self.assertEqual(call.kwargs['itinerary_type'], 'all')
        _, selected = self.app.selected({'search_id': 'saved', 'alternative_id': result['alternative_id'], 'flight_index': 25})
        self.assertEqual(selected, result['flights'][25])
        with self.assertRaises(DataError):
            self.app.selected({'search_id': 'saved', 'alternative_id': 'invented', 'flight_index': 0})

    def test_alternative_ticket_lookup_uses_its_own_search_context_and_original_budget(self):
        flight = self.normalize()
        flight['booking_query'] = {**QUERY, 'departure_id': 'BOS,PVD', 'arrival_id': 'ORD,MDW',
                                   'outbound_date': '2026-10-25', 'max_price': 999}
        self.app.tickets.check = Mock(return_value={'status': 'unavailable', 'eligible': None})
        self.app.analysis_for = Mock(return_value={'journey': True})
        self.app.selected_analysis({'result': {'query': QUERY}}, flight)
        sent = self.app.tickets.check.call_args.args[1]
        self.assertEqual(sent['arrival_id'], 'ORD,MDW')
        self.assertEqual(sent['outbound_date'], '2026-10-25')
        self.assertEqual(sent['max_price'], 250)

    def test_cached_alternatives_drop_newly_identified_separate_tickets(self):
        original = self.normalize(direct())
        candidate = self.normalize()
        self.app.searcher.search_airports = Mock(return_value={'flights': [candidate], 'cached': False, 'fetched_at': '2026-10-23'})
        session = {'result': {'query': QUERY, 'flights': [original]}, 'alternatives': {}}
        first = self.app.alternatives_for(session, original)
        self.assertEqual(len(first['flights']), 1)
        session['ticket_checks'] = {journey_key(candidate): {'status': 'separate_tickets', 'eligible': False}}
        second = self.app.alternatives_for(session, original)
        self.assertEqual(second['flights'], [])
        self.assertNotEqual(first['alternative_id'], second['alternative_id'])
        self.assertEqual(session['alternative_sets'][second['alternative_id']], [])
        self.assertEqual(self.app.searcher.search_airports.call_count, 3)

    def test_assistant_analysis_contains_both_legs_and_ticket_evidence(self):
        flight = self.normalize()
        session = {'result': {'query': QUERY, 'flights': [flight], 'fetched_at': '2026-10-23'},
                   'created': time.monotonic(), 'turns': {}, 'lock': threading.Lock()}
        self.app.sessions['saved'] = session
        evidence = {'source': 'BTS + FlightAware AeroAPI', 'journey': True,
                    'ticket': {'status': 'unavailable', 'eligible': None},
                    'connection': flight['layovers'][0], 'legs': [
                        {'flight': leg, 'history': {'raw_details': 'omitted from model'},
                         'analysis': {'traveler': {'summary_text': leg['flight_number']},
                                      'cautions': [], 'selected_flight_verified': False}}
                        for leg in flight['legs']]}
        self.app.selected_analysis = Mock(return_value=evidence)
        def respond(client, model, context, previous, question, execute):
            result = execute('get_flight_analysis')
            self.assertEqual([l['flight']['flight_number'] for l in result['legs']], ['UA 1', 'UA 2'])
            self.assertEqual(result['ticket']['status'], 'unavailable')
            self.assertNotIn('raw_details', json.dumps(result))
            return {'answer': 'Both flights checked.'}, []
        with patch('delay_aware.application.answer', side_effect=respond):
            result = self.app.chat({'search_id': 'saved', 'flight_index': 0, 'message': 'Assess both flights'})
        self.assertEqual(result['answer'], 'Both flights checked.')

    def test_missing_history_reply_covers_each_leg(self):
        client = Mock()
        client.lock = threading.Lock()
        client.chat.side_effect = [
            {'tool_calls': [{'function': {'name': 'get_flight_history', 'arguments': {}}}]},
            {'content': 'No historical records exist anywhere.'}]
        result, _ = answer(client, 'gpt-oss:20b', {}, [], 'Check both flights', Mock(return_value={
            'source': 'BTS', 'available': False, 'journey': True,
            'legs': [{'flight': l, 'history': {'available': False}} for l in self.normalize()['legs']]}))
        self.assertIn('UA 1', result['answer'])
        self.assertIn('UA 2', result['answer'])
        self.assertNotIn('anywhere', result['answer'])


if __name__ == '__main__':
    unittest.main()
