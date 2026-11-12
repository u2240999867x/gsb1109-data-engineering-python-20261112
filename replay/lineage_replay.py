"""Replayable diagnosis of HOCON layered parsing binding timing (pyhocon 0.3.63).

Run:  .venv/bin/python replay/lineage_replay.py

Three layered-loading mechanisms share one example:
  1. cross-key substitution ........ ${part.col}
  2. same-key self-reference append  part.col = ${part.col} "_v2"
  3. optional layer missing ........ view.filter = ${?part.filter}
"""
from pyhocon import ConfigFactory, ConfigTree
from pyhocon.config_parser import ConfigParser
from pyhocon.config_tree import ConfigValues, ConfigSubstitution


def load_unresolved(text):
    return ConfigFactory.parse_string(text, resolve=False)


def stack_layers(layers, resolve_each=False):
    """Merge layers bottom -> top, mirroring partition backfill."""
    acc = None
    for text in layers:
        tree = ConfigFactory.parse_string(text, resolve=resolve_each)
        acc = tree if acc is None else ConfigTree.merge_configs(acc, tree)
    if not resolve_each:
        ConfigParser.resolve_substitutions(acc)
    return acc


def is_self_append(tree, dotted_path):
    """Minimal discriminating observation, valid ONLY on an unresolved tree."""
    node = tree
    for key in dotted_path.split('.'):
        node = node[key]
    return isinstance(node, ConfigValues) and any(
        isinstance(token, ConfigSubstitution) for token in node.tokens)


def main():
    print("=" * 70)
    print("1. BINDING TIMING: substitution tokens bind at RESOLVE, not at merge")
    print("=" * 70)
    layer1 = 'part.col = "uid"\nview.proj = ${part.col}\n'
    layer2 = 'part.col = "user_id"\n'
    eager = stack_layers([layer1, layer2], resolve_each=True)
    deferred = stack_layers([layer1, layer2], resolve_each=False)
    print("resolve each layer before merge :", eager.as_plain_ordered_dict())
    print("merge unresolved, resolve once   :", deferred.as_plain_ordered_dict())
    assert eager['view']['proj'] == 'uid'          # token bound before the rename landed
    assert deferred['view']['proj'] == 'user_id'   # token bound after the rename landed

    print()
    print("=" * 70)
    print("2. EQUAL-DISPLAY PAIR: two histories, identical final dictionary")
    print("=" * 70)
    base = 'part.col = "uid"\n'
    literal_override = 'part.col = "uid _v2"\n'
    self_append = 'part.col = ${part.col} "_v2"\n'
    history_a = [base, literal_override]
    history_b = [base, self_append]
    product_a = stack_layers(history_a)
    product_b = stack_layers(history_b)
    print("history A (literal override) :", product_a.as_plain_ordered_dict())
    print("history B (self append)      :", product_b.as_plain_ordered_dict())
    assert product_a.as_plain_ordered_dict() == product_b.as_plain_ordered_dict()

    print()
    print("=" * 70)
    print("3. ADD ONE LAYER BETWEEN THEM -> the equal pair diverges")
    print("=" * 70)
    backfilled_mid = 'part.col = "mid"\n'
    replayed_a = stack_layers([base, backfilled_mid, literal_override])
    replayed_b = stack_layers([base, backfilled_mid, self_append])
    print("A + mid layer (literal wins)      :", replayed_a.as_plain_ordered_dict())
    print("B + mid layer (token re-binds)    :", replayed_b.as_plain_ordered_dict())
    assert replayed_a['part']['col'] == 'uid _v2'
    assert replayed_b['part']['col'] == 'mid _v2'

    # The same contrast with a cross-key substitution and a TOP override.
    print()
    print("-- cross-key variant: rename inserted at the top --")
    hist_c = ['part.col = "uid"\nview.proj = "uid"\n']
    hist_d = ['part.col = "uid"\nview.proj = ${part.col}\n']
    rename_top = 'part.col = "device_id"\n'
    assert stack_layers(hist_c + [rename_top])['view']['proj'] == 'uid'
    assert stack_layers(hist_d + [rename_top])['view']['proj'] == 'device_id'
    print("literal view.proj stays 'uid'; substitution view.proj follows 'device_id'")

    print()
    print("=" * 70)
    print("4. OPTIONAL LAYER MISSING: ${?x} resolves to None and DELETES the key")
    print("=" * 70)
    opt_present = 'part.filter = "dt=2026"\nview.filter = ${?part.filter}\nview.keep = 1\n'
    opt_absent = 'view.filter = ${?part.filter}\nview.keep = 1\n'
    present = stack_layers([base, opt_present])
    absent = stack_layers([base, opt_absent])
    print("optional layer present:", present.as_plain_ordered_dict())
    print("optional layer missing:", absent.as_plain_ordered_dict())
    assert present['view']['filter'] == 'dt=2026'
    assert 'filter' not in absent['view'] and 'keep' in absent['view']

    print()
    print("=" * 70)
    print("5. MINIMAL DISCRIMINATING OBSERVATION (before resolve only)")
    print("=" * 70)
    unresolved_a = ConfigTree.merge_configs(load_unresolved(base),
                                            load_unresolved(literal_override))
    unresolved_b = ConfigTree.merge_configs(load_unresolved(base),
                                            load_unresolved(self_append))
    obs_a = is_self_append(unresolved_a, 'part.col')
    obs_b = is_self_append(unresolved_b, 'part.col')
    print("unresolved literal-override tree self-append? ", obs_a)
    print("unresolved self-append tree self-append?       ", obs_b)
    assert obs_a is False and obs_b is True

    print()
    print("=" * 70)
    print("6. PRODUCT-ONLY DICTIONARY CANNOT RESTORE COLUMN LINEAGE")
    print("=" * 70)
    product = stack_layers([base, self_append, opt_absent]).as_plain_ordered_dict()
    rebuilt = ConfigFactory.from_dict(product)
    print("final plain product :", product)
    print("from_dict rebuilt   :", rebuilt.as_plain_ordered_dict())
    print("rebuilt carries history? ", hasattr(rebuilt, 'history'))
    assert not hasattr(rebuilt, 'history')
    ConfigTree.merge_configs(rebuilt, load_unresolved('part.col = "device_id"\n'))
    print("backfill on product can only blind-overwrite:", rebuilt.as_plain_ordered_dict())

    print()
    print("ALL ASSERTIONS PASSED")


if __name__ == '__main__':
    main()
