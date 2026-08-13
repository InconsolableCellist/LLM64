#!/usr/bin/env python3
"""Home Assistant derivation. Run: python3 tests/test_homeassist.py

Naming cases are regressions from a live instance: three rows called
"Door", two garage doors that could not be told apart, and a graph that
showed solar generating at midnight.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.homeassist import (
    render_number,
    BLACK, CYAN, GREEN, RED, DKGREY, YELLOW, LTGREY,
    COLS, PAIRS, ROWS,
    Row, Screen, build_blocks, cell, entities_of, fmt_state, mat,
    name_screen, render_view, resample, scale_to_band, action_for,
)


def S(entity_id, state, friendly=None, device_class=None, unit=None, **extra):
    attrs = {'friendly_name': friendly} if friendly is not None else {}
    if device_class:
        attrs['device_class'] = device_class
    if unit:
        attrs['unit_of_measurement'] = unit
    attrs.update(extra)
    return {'entity_id': entity_id, 'state': state, 'attributes': attrs}


def states_of(*entries):
    return {e['entity_id']: e for e in entries}


def no_area(_):
    return None


# ---------------------------------------------------------------- cells

def test_cell_encodes_reverse_in_bit7():
    assert cell('A') == 0x41
    assert cell('A', reverse=True) == 0xC1


def test_cell_replaces_anything_the_font_lacks_with_a_space():
    assert cell('é') == 0x20
    assert cell('\n') == 0x20


def test_mat_packs_foreground_high_background_low():
    # a cell has an ink and a ground: two colours, not one over black
    assert mat(1, 6) == 0x16
    assert mat(CYAN) == 0x30


def test_ink_span_snaps_to_the_two_column_matrix():
    r = Row()
    r.ink(4, 4, RED)
    assert r.colors[2] == mat(RED) and r.colors[3] == mat(RED)
    assert r.colors[1] == mat(LTGREY) and r.colors[4] == mat(LTGREY)


# --------------------------------------------------------------- naming

def test_three_doors_stay_distinguishable():
    """Stripping the area left three rows called "Door"."""
    st = states_of(
        S('binary_sensor.kitchen_door', 'off', 'Kitchen Door Window/door is open', 'door'),
        S('binary_sensor.garage_door', 'off', 'Garage Door is open', 'door'),
        S('binary_sensor.basement_door', 'off', 'Basement Door is open', 'door'),
    )
    area = {'binary_sensor.kitchen_door': 'Kitchen',
            'binary_sensor.garage_door': 'Garage',
            'binary_sensor.basement_door': 'Basement'}
    labels = name_screen(list(st), st, lambda e: area.get(e))
    assert len(set(labels.values())) == 3
    assert all(len(v.split()) >= 2 for v in labels.values()), labels
    assert set(labels.values()) == {'Kitchen Door', 'Garage Door', 'Basement Door'}


def test_siblings_shorten_by_the_same_amount():
    """Greedy naming let one door take the short name, siblings the long."""
    st = states_of(
        S('cover.g1', 'closed', 'Garage Door Opener Garage Door Single', 'garage'),
        S('cover.g2', 'closed', 'Garage Door Opener Garage Door Double', 'garage'),
    )
    labels = name_screen(list(st), st, no_area)
    assert labels['cover.g1'] != labels['cover.g2']
    # both keep the distinguishing tail
    assert labels['cover.g1'].endswith('Single')
    assert labels['cover.g2'].endswith('Double')


def test_words_shared_by_every_sibling_are_dropped():
    """"Multisensor" distinguishes neither; the last word must survive."""
    st = states_of(
        S('sensor.a', '47', 'Basement Multisensor Humidity'),
        S('sensor.b', '47', 'Family Room Multisensor Humidity'),
    )
    labels = name_screen(list(st), st, no_area)
    assert labels['sensor.a'] == 'Basement Humidity'
    assert labels['sensor.b'] == 'Family Room Humidity'


def test_unrelated_sensors_are_not_forced_to_shorten_alike():
    """Not siblings just because both lack a device_class."""
    st = states_of(
        S('sensor.h', '47', 'Basement Multisensor Humidity'),
        S('sensor.i', '7', 'Basement Multisensor Illuminance'),
        S('sensor.u', '0', 'Basement Multisensor Ultraviolet'),
    )
    labels = name_screen(list(st), st, no_area)
    assert len(set(labels.values())) == 3
    for v in labels.values():
        assert v.islower() is False, f'fell through to the entity id: {v}'


def test_labels_are_unique_even_when_names_collide_entirely():
    st = states_of(
        S('switch.a', 'off', 'Base Station'),
        S('switch.b', 'off', 'Base Station'),
    )
    labels = name_screen(list(st), st, no_area)
    assert labels['switch.a'] != labels['switch.b']


def test_labels_never_exceed_the_column_width():
    st = states_of(
        S('sensor.x', '1', 'An Extremely Long Friendly Name That Will Not Fit At All'),
    )
    labels = name_screen(list(st), st, no_area, width=24)
    assert len(labels['sensor.x']) <= 24


def test_missing_friendly_name_falls_back_to_the_entity_id():
    st = states_of(S('vacuum.floorbold', 'unavailable'))
    labels = name_screen(list(st), st, no_area)
    assert labels['vacuum.floorbold']


def test_explicit_override_wins():
    st = states_of(S('sensor.radon_bq_per_m3', '46', 'Radon Bq Per M3', unit='Bq/m3'))
    labels = name_screen(list(st), st, no_area,
                         overrides={'sensor.radon_bq_per_m3': 'Radon'})
    assert labels['sensor.radon_bq_per_m3'] == 'Radon'


# ---------------------------------------------------------------- state

def test_binary_sensor_reads_by_device_class():
    for dc, state, text, role in [
        ('door', 'on', 'OPEN', 'bad'),
        ('door', 'off', 'shut', 'good'),
        ('motion', 'on', 'MOTION', 'warn'),
        ('motion', 'off', 'clear', 'dim'),
        # inverted polarity: for connectivity it is OFF that is alarming
        ('connectivity', 'off', 'DOWN', 'bad'),
        ('connectivity', 'on', 'up', 'good'),
        (None, 'on', 'ON', 'warn'),
    ]:
        got, ink = fmt_state(state, {'device_class': dc} if dc else {}, 'binary_sensor')
        assert (got, ink) == (text, role), f'{dc} {state} -> {got} {ink}'



def test_unavailable_is_never_rendered_as_a_value():
    for bad in ('unavailable', 'unknown', None):
        assert fmt_state(bad, {}, 'sensor') == ('n/a', 'dim')


def test_numbers_carry_their_unit_and_stay_inside_the_column():
    text, role = fmt_state('922', {'unit_of_measurement': 'W'}, 'sensor')
    assert text == '922W' and role == 'num'
    text, _ = fmt_state('83.0', {'unit_of_measurement': '°F'}, 'sensor')
    assert text == '83F'
    assert len(fmt_state('123456789', {'unit_of_measurement': 'W'}, 'sensor')[0]) <= 8


def test_climate_shows_the_setpoint_not_the_mode():
    text, _ = fmt_state('cool', {'temperature': 86}, 'climate')
    assert text == '86'


def test_cover_open_is_alarming_and_closed_is_not():
    assert fmt_state('closed', {}, 'cover') == ('shut', 'good')
    assert fmt_state('open', {}, 'cover')[1] == 'bad'


def test_action_is_derived_from_the_domain():
    assert action_for('climate') == 'EDIT_CLIMATE'
    assert action_for('light') == 'EDIT_LIGHT'
    assert action_for('cover') == 'CONFIRM'
    assert action_for('switch') == 'TOGGLE'
    assert action_for('sensor') is None


# -------------------------------------------------------------- history

def test_resample_uses_time_not_sample_index():
    """Samples cluster where the value moves; index mapping made solar
    generate at midnight."""
    pts = [(0.0, 0.0)] + [(90.0 + i, 100.0) for i in range(50)] + [(100.0, 100.0)]
    grid = resample(pts, 10)
    # the first 90% of the TIME is the zero, so most bins are zero
    assert grid[0] == 0.0
    assert sum(1 for v in grid if v == 0.0) >= 6, grid


def test_resample_holds_the_last_value_across_a_gap():
    grid = resample([(0.0, 5.0), (100.0, 9.0)], 5)
    assert grid[0] == 5.0
    assert grid[-1] == 9.0
    assert all(v in (5.0, 9.0) for v in grid)


def test_resample_survives_no_data():
    assert resample([], 4) == [0.0, 0.0, 0.0, 0.0]


def test_scale_to_band_inverts_because_y_grows_downward():
    ys = scale_to_band([0.0, 10.0], 8, lo=0.0, hi=10.0)
    assert ys[0] == 7      # smallest value sits at the bottom
    assert ys[1] == 0      # largest at the top


def test_scale_to_band_clamps_outliers():
    ys = scale_to_band([-5.0, 50.0], 8, lo=0.0, hi=10.0)
    assert min(ys) >= 0 and max(ys) <= 7


# ---------------------------------------------------------------- cards

def test_unknown_card_type_degrades_to_rows():
    """An unknown card still shows its entities."""
    view = {'cards': [{'type': 'some-custom:fancy-card',
                       'entities': ['switch.a', 'switch.b']}]}
    blocks = build_blocks(view)
    assert blocks == [('SECTION', (None, ['switch.a', 'switch.b']))]


def test_card_types_map_to_their_screens():
    view = {'cards': [
        {'type': 'thermostat', 'entity': 'climate.t'},
        {'type': 'history-graph', 'entities': ['sensor.p']},
        {'type': 'logbook', 'entities': ['binary_sensor.d']},
    ]}
    kinds = {k for k, _ in build_blocks(view)}
    assert 'EDIT_CLIMATE' in kinds
    assert 'PLOT' in kinds
    assert 'SKIP' not in kinds          # logbook contributes nothing


def test_heading_with_nothing_under_it_is_dropped():
    view = {'sections': [{'cards': [
        {'type': 'heading', 'heading': 'Where'},
        {'type': 'map', 'entities': ['device_tracker.phone']},
    ]}]}
    assert build_blocks(view) == []


def test_logbook_contributes_its_entities_as_rows():
    """No room for the history, but the entities still belong."""
    view = {'sections': [{'cards': [
        {'type': 'heading', 'heading': 'Activity'},
        {'type': 'logbook', 'entities': ['binary_sensor.motion']},
    ]}]}
    assert build_blocks(view) == [('SECTION', ('Activity', ['binary_sensor.motion']))]


def test_a_graphed_entity_is_still_listed_as_a_row():
    """The graph is extra; the row still carries the value."""
    view = {'cards': [{'type': 'history-graph', 'entities': ['sensor.power']}]}
    blocks = build_blocks(view)
    assert ('PLOT', 'sensor.power') in blocks
    assert ('SECTION', (None, ['sensor.power'])) in blocks


def test_entities_are_found_however_the_card_nests_them():
    card = {'type': 'grid', 'cards': [
        {'type': 'tile', 'entity': 'light.a'},
        {'type': 'entities', 'entities': [
            'switch.b', {'entity': 'sensor.c'}]},
    ]}
    assert entities_of(card) == ['light.a', 'switch.b', 'sensor.c']


def test_entities_are_deduplicated_in_order():
    card = {'entities': ['a.x', 'b.y', 'a.x']}
    assert entities_of(card) == ['a.x', 'b.y']


# --------------------------------------------------------------- screen

def _demo_view():
    return {'sections': [{'cards': [
        {'type': 'heading', 'heading': 'Security'},
        {'type': 'entities', 'entities': [
            'binary_sensor.front_door', 'switch.flag_lamp']},
    ]}]}


def _demo_states():
    return states_of(
        S('binary_sensor.front_door', 'on', 'Front Door is open', 'door'),
        S('switch.flag_lamp', 'off', 'Flag Lamp'),
    )


def test_render_produces_a_full_screen():
    sc = render_view(_demo_view(), _demo_states(), no_area, title='Home')
    assert len(sc.rows) == ROWS
    for r in sc.rows:
        assert len(r.cells) == COLS
        assert len(r.colors) == PAIRS


def test_only_actionable_rows_get_a_hotkey():
    sc = render_view(_demo_view(), _demo_states(), no_area)
    acted = {v['entity'] for v in sc.keymap.values()}
    assert 'switch.flag_lamp' in acted
    assert 'binary_sensor.front_door' not in acted


def test_covers_are_marked_for_confirmation_by_domain():
    view = {'cards': [{'type': 'entities', 'entities': ['cover.garage']}]}
    st = states_of(S('cover.garage', 'closed', 'Garage Door', 'garage'))
    sc = render_view(view, st, no_area)
    entry = next(iter(sc.keymap.values()))
    assert entry['confirm'] is True


def test_switches_do_not_ask_first():
    sc = render_view(_demo_view(), _demo_states(), no_area)
    entry = next(v for v in sc.keymap.values() if v['entity'] == 'switch.flag_lamp')
    assert entry['confirm'] is False


def test_frames_never_exceed_the_wire_payload_limit():
    sc = render_view(_demo_view(), _demo_states(), no_area)
    frames = sc.frames(max_payload=512)
    assert frames
    for _, payload in frames:
        assert len(payload) <= 512
    # and every row is sent exactly once
    covered = []
    for first, payload in frames:
        covered.extend(range(first, first + payload[1]))
    assert covered == list(range(ROWS))


def test_row_bytes_are_colour_then_cells():
    r = Row()
    r.put(0, 'Hi')
    r.ink(0, 2, GREEN)
    blob = r.to_bytes()
    assert len(blob) == PAIRS + COLS
    assert blob[0] == mat(GREEN)
    assert blob[PAIRS] == ord('H')


def test_an_entity_shown_twice_gets_one_row_and_keeps_its_name():
    """Fed the same id twice, the namer collided it with itself."""
    view = {'sections': [
        {'cards': [{'type': 'heading', 'heading': 'Security'},
                   {'type': 'entities', 'entities': ['binary_sensor.front_door']}]},
        {'cards': [{'type': 'heading', 'heading': 'Activity'},
                   {'type': 'logbook', 'entities': ['binary_sensor.front_door',
                                                    'binary_sensor.motion']}]},
    ]}
    st = states_of(
        S('binary_sensor.front_door', 'off', 'Front Door is open', 'door'),
        S('binary_sensor.motion', 'off', 'Hall Motion', 'motion'),
    )
    sc = render_view(view, st, no_area)
    text = ''.join(chr(c & 0x7F) for r in sc.rows for c in r.cells)
    assert text.count('Front Door') == 1, 'entity rendered twice'
    assert 'Hall Motion' in text
    assert 'front door' not in text, 'fell through to the entity id'


def _big_view(n):
    return {'sections': [{'cards': [
        {'type': 'heading', 'heading': 'Everything'},
        {'type': 'entities', 'entities': [f'switch.s{i}' for i in range(n)]},
    ]}]}


def _big_states(n):
    return states_of(*[S(f'switch.s{i}', 'off', f'Switch Number {i}')
                       for i in range(n)])


def test_a_section_too_tall_for_one_pane_flows_into_the_next():
    """Downstairs is thirty entities under one heading. Keeping the
    section whole left the left pane empty and dropped the rest."""
    sc = render_view(_big_view(30), _big_states(30), no_area)
    left = [sc.rows[r].cells[4:28] for r in range(1, 22)]
    used = sum(1 for row in left if bytes(row).strip(b' '))
    assert used > 10, 'left pane is empty; the section jumped to pane two'


def test_everything_on_a_page_is_reachable_across_pages():
    n = 60
    seen = set()
    for page in range(4):
        sc = render_view(_big_view(n), _big_states(n), no_area, page=page)
        text = ''.join(chr(c & 0x7F) for r in sc.rows for c in r.cells)
        for i in range(n):
            if f'Switch Number {i}' in text:
                seen.add(i)
        if page + 1 >= sc.npages:
            break
    assert len(seen) == n, f'{n - len(seen)} entities unreachable'


def test_page_count_and_clamping():
    sc = render_view(_big_view(60), _big_states(60), no_area, page=99)
    assert sc.page == sc.npages - 1, 'page past the end should clamp'
    one = render_view(_big_view(5), _big_states(5), no_area)
    assert one.npages == 1


def test_a_heading_never_sits_alone_at_the_foot_of_a_pane():
    view = {'sections': [
        {'cards': [{'type': 'heading', 'heading': 'A'},
                   {'type': 'entities',
                    'entities': [f'switch.a{i}' for i in range(20)]}]},
        {'cards': [{'type': 'heading', 'heading': 'B'},
                   {'type': 'entities', 'entities': ['switch.b0']}]},
    ]}
    st = states_of(*([S(f'switch.a{i}', 'off', f'Alpha {i}') for i in range(20)]
                     + [S('switch.b0', 'off', 'Bravo Zero')]))
    sc = render_view(view, st, no_area)
    # 'B' must not be the last row of a pane with nothing under it
    row21 = ''.join(chr(c & 0x7F) for c in sc.rows[21].cells[:38])
    assert 'B' not in row21.strip() or 'Bravo' in ''.join(
        chr(c & 0x7F) for c in sc.rows[21].cells)


def test_paging_only_applies_to_the_overview():
    sc = render_view(_big_view(5), _big_states(5), no_area)
    assert sc.npages == 1 and sc.page == 0


def test_a_slider_gets_an_editor_not_a_toggle():
    assert action_for('input_number') == 'EDIT_NUMBER'
    assert action_for('number') == 'EDIT_NUMBER'


def test_number_editor_shows_value_range_and_step():
    st = states_of(S('input_number.sprinkler_minutes', '1.0',
                     'Sprinkler Minutes', unit='min',
                     min=1.0, max=30.0, step=1.0))
    sc = render_number('input_number.sprinkler_minutes', st, 'Sprinkler Minutes')
    text = ''.join(chr(c & 0x7F) for r in sc.rows for c in r.cells)
    assert 'RANGE' in text and 'step 1' in text
    assert '30' in text          # the top of the range is stated
    assert set('+-\r') <= set(sc.keymap)


def test_number_editor_marks_a_pending_value():
    st = states_of(S('input_number.x', '5', 'X', min=0.0, max=10.0, step=1.0))
    plain = render_number('input_number.x', st, 'X')
    dirty = render_number('input_number.x', st, 'X', pending=7.0)
    assert plain.rows[9].cells != dirty.rows[9].cells, 'pending is not flagged'


def test_a_missing_entity_does_not_break_the_screen():
    view = {'cards': [{'type': 'entities', 'entities': ['sensor.gone']}]}
    sc = render_view(view, {}, no_area)
    assert len(sc.rows) == ROWS


# ------------------------------------------------------------- harness

if __name__ == '__main__':
    failures = []
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith('test_') and callable(f)]
    for name, fn in tests:
        try:
            fn()
        except Exception as exc:                       # noqa: BLE001
            import traceback
            failures.append(f'{name}:\n{traceback.format_exc()}')
    if failures:
        print(f'FAIL ({len(failures)} of {len(tests)})\n')
        print('\n\n'.join(failures))
        sys.exit(1)
    print(f'all {len(tests)} home assistant tests pass')
