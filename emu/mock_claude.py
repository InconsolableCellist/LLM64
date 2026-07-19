#!/usr/bin/env python3
"""Mock `claude` CLI for the Claude Code e2e test.

Speaks just enough of the stream-json protocol the proxy's ClaudeSession
drives: answers the initialize control_request, and for each user turn
emits an assistant text block, a tool_use, a can_use_tool
control_request, then (after the host allows/denies) a closing text and
a result. Deterministic, no network.
"""
import sys
import json


def emit(obj):
    sys.stdout.write(json.dumps(obj) + '\n')
    sys.stdout.flush()


def main():
    emit({"type": "system", "subtype": "init",
          "permissionMode": "default", "session_id": "mock"})
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            d = json.loads(raw)
        except json.JSONDecodeError:
            continue

        t = d.get('type')
        if t == 'control_request' \
                and d.get('request', {}).get('subtype') == 'initialize':
            emit({"type": "control_response", "response": {
                "subtype": "success", "request_id": d.get('request_id'),
                "response": {"commands": []}}})
            continue

        if t == 'user':
            emit({"type": "assistant", "message": {"role": "assistant",
                  "content": [{"type": "text",
                               "text": "Sure, creating that file."}]}})
            emit({"type": "assistant", "message": {"role": "assistant",
                  "content": [{"type": "tool_use", "name": "Write",
                               "id": "t1",
                               "input": {"file_path": "hello.txt",
                                         "content": "hi"}}]}})
            emit({"type": "control_request", "request_id": "perm-1",
                  "request": {"subtype": "can_use_tool",
                              "tool_name": "Write",
                              "description": "hello.txt",
                              "input": {"file_path": "hello.txt",
                                        "content": "hi"}}})
            continue

        if t == 'control_response':
            resp = d.get('response', {}).get('response', {})
            allowed = resp.get('behavior') == 'allow'
            emit({"type": "assistant", "message": {"role": "assistant",
                  "content": [{"type": "text",
                               "text": ("Done, wrote hello.txt."
                                        if allowed
                                        else "Okay, skipped it.")}]}})
            emit({"type": "result", "subtype": "success",
                  "result": "ok"})
            continue

    return 0


if __name__ == '__main__':
    sys.exit(main())
