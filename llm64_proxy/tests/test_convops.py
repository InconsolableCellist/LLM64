#!/usr/bin/env python3
"""The history verbs: /redo, /retcon and /fork lean on these three
ConversationManager operations. Run: python3 tests/test_convops.py"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.conversation import ConversationManager

failures = []


def check(name, got, want):
    if got != want:
        failures.append(f"{name}:\n  got  {got!r}\n  want {want!r}")


class _Cfg:
    def __init__(self, d):
        self.data_dir = d


with tempfile.TemporaryDirectory() as td:
    cm = ConversationManager(_Cfg(td))
    cm.new_conversation()
    cm.add_message('user', 'hello')
    cm.add_message('assistant', 'hi there')
    cm.add_message('user', 'tell me a story')
    cm.add_message('assistant', 'once upon a time')
    cm.set_meta('adv_state', '{"hp":9}')

    # redo: the reply goes, the user line stays
    check("drop_last_reply removes the reply",
          cm.drop_last_reply(), True)
    check("...leaving the user line last",
          cm.get_messages()[-1],
          {'role': 'user', 'content': 'tell me a story'})
    check("drop_last_reply refuses when the user spoke last",
          cm.drop_last_reply(), False)

    # retcon: the whole exchange goes
    cm.add_message('assistant', 'a different take')
    check("drop_last_exchange returns the user line",
          cm.drop_last_exchange(), 'tell me a story')
    check("...and the earlier exchange survives",
          [m['content'] for m in cm.get_messages()],
          ['hello', 'hi there'])

    # fork: a new id, same history and meta, original untouched on disk
    old_id = cm.current_id
    new_id = cm.fork_conversation()
    check("fork mints a new id", new_id != old_id, True)
    check("fork keeps the messages",
          [m['content'] for m in cm.get_messages()],
          ['hello', 'hi there'])
    check("fork keeps the meta", cm.get_meta('adv_state'), '{"hp":9}')
    check("fork marks the title",
          cm.current_conversation['title'].endswith(' (fork)'), True)
    check("the original is still on disk",
          cm.load_conversation(old_id), True)
    check("...without the fork suffix",
          cm.current_conversation['title'].endswith(' (fork)'), False)

    # empty conversation edge cases
    cm2 = ConversationManager(_Cfg(td))
    check("retcon with no conversation", cm2.drop_last_exchange(), '')
    check("redo with no conversation", cm2.drop_last_reply(), False)
    check("fork with no conversation", cm2.fork_conversation(), 0)

if failures:
    print("\n\n".join(failures))
    sys.exit(1)
print("all conversation-op tests pass")
