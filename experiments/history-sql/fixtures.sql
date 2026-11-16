-- ACP examples in the target history format. Replace only sql-acp-demo.
begin;

delete
from
    boba.records
where 1=1
    and session_id in ('sql-acp-demo', 'sql-acp-actions');

insert into boba.records (
    session_id,
    seq,
    record_id,
    operation_id,
    operation_position,
    parent_operation_id,
    kind,
    category,
    role,
    key,
    operation,
    scope,
    copied_from_id,
    created_at,
    version,
    payload
)
select
    r.session_id,
    r.seq,
    r.record_id,
    r.operation_id,
    r.operation_position,
    r.parent_operation_id,
    r.kind,
    r.category,
    r.role,
    r.key,
    r.operation,
    r.scope,
    r.copied_from_id,
    r.created_at,
    r.version,
    r.payload
from
    jsonb_to_recordset($records$[
  {
    "session_id": "sql-acp-demo",
    "seq": 1,
    "record_id": "model",
    "operation_id": "model",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "model",
    "category": "setting",
    "role": null,
    "key": "model",
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T00:00:00Z",
    "version": "test",
    "payload": {
      "value": {
        "model": "test"
      },
      "provider": "test",
      "profile_id": null
    },
    "scope": "session"
  },
  {
    "session_id": "sql-acp-demo",
    "seq": 2,
    "record_id": "sampling",
    "operation_id": "sampling",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "sampling",
    "category": "setting",
    "role": null,
    "key": "sampling",
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T00:00:00Z",
    "version": "test",
    "payload": {
      "value": {
        "temperature": 0.2
      }
    },
    "scope": "session"
  },
  {
    "session_id": "sql-acp-demo",
    "seq": 3,
    "record_id": "temperature",
    "operation_id": "temperature",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "parameter_option",
    "category": "operational",
    "role": null,
    "key": "temperature",
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T00:00:00Z",
    "version": "test",
    "payload": {
      "name": "Temperature",
      "description": null,
      "source_kinds": [
        "sampling",
        "sampling_removed"
      ],
      "source_key": "sampling",
      "value_path": [
        "value",
        "temperature"
      ],
      "choices": [
        {
          "value_id": "low",
          "name": "Low",
          "value": 0.2
        },
        {
          "value_id": "high",
          "name": "High",
          "value": 0.8
        }
      ]
    }
  },
  {
    "session_id": "sql-acp-demo",
    "seq": 4,
    "record_id": "user",
    "operation_id": "user",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "user_message",
    "category": "message",
    "role": "user",
    "key": null,
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T00:00:00Z",
    "version": "test",
    "payload": {
      "content": [
        {
          "kind": "text",
          "text": "Read file"
        }
      ]
    },
    "scope": "session"
  },
  {
    "session_id": "sql-acp-demo",
    "seq": 5,
    "record_id": "attempt",
    "operation_id": "attempt",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "llm_request_attempt",
    "category": "operational",
    "role": null,
    "key": null,
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T00:00:00Z",
    "version": "test",
    "payload": {
      "level": "info",
      "content": [
        {
          "kind": "text",
          "text": "Completed"
        }
      ],
      "attempt": 1,
      "status": "completed",
      "started_at": "2026-11-16T00:00:00Z",
      "method": "POST",
      "url": "https://example.test/chat/completions",
      "status_code": 200,
      "duration_ms": 10,
      "error": null,
      "retry_after_ms": null,
      "request_body": null,
      "response_body": null
    }
  },
  {
    "session_id": "sql-acp-demo",
    "seq": 6,
    "record_id": "assistant",
    "operation_id": "assistant",
    "operation_position": 0,
    "parent_operation_id": "attempt",
    "kind": "assistant_message",
    "category": "message",
    "role": "assistant",
    "key": null,
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T00:00:00Z",
    "version": "test",
    "payload": {
      "content": [
        {
          "kind": "tool_call_reference",
          "call_id": "call"
        }
      ],
      "completion": "complete"
    },
    "scope": "session"
  },
  {
    "session_id": "sql-acp-demo",
    "seq": 7,
    "record_id": "call-definition",
    "operation_id": "assistant",
    "operation_position": 1,
    "parent_operation_id": "attempt",
    "kind": "tool_call",
    "category": "tool_call",
    "role": "assistant",
    "key": "call",
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T00:00:00Z",
    "version": "test",
    "payload": {
      "assistant_record_id": "assistant",
      "name": "read",
      "arguments": {
        "path": "app.py"
      },
      "task_id": null
    }
  },
  {
    "session_id": "sql-acp-demo",
    "seq": 8,
    "record_id": "pending",
    "operation_id": "assistant",
    "operation_position": 2,
    "parent_operation_id": "attempt",
    "kind": "tool_result",
    "category": "tool_result",
    "role": "tool",
    "key": "call",
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T00:00:00Z",
    "version": "test",
    "payload": {
      "call_record_id": "call-definition",
      "status": "pending",
      "state": null,
      "source_record_id": null,
      "content": [
        {
          "kind": "text",
          "text": "Pending"
        }
      ]
    }
  },
  {
    "session_id": "sql-acp-demo",
    "seq": 9,
    "record_id": "permission",
    "operation_id": "permission",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "permission_request",
    "category": "operational",
    "role": null,
    "key": null,
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T00:00:00Z",
    "version": "test",
    "payload": {
      "related_message_id": "assistant",
      "call_id": "call",
      "title": "Read file",
      "description": null,
      "options": [
        {
          "option_id": "allow",
          "name": "Allow",
          "decision": "allow",
          "scope": "call"
        }
      ]
    }
  },
  {
    "session_id": "sql-acp-demo",
    "seq": 10,
    "record_id": "permission-response",
    "operation_id": "permission-response",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "permission_response",
    "category": "operational",
    "role": null,
    "key": null,
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T00:00:00Z",
    "version": "test",
    "payload": {
      "permission_request_id": "permission",
      "action": "selected",
      "option_id": "allow"
    }
  },
  {
    "session_id": "sql-acp-demo",
    "seq": 11,
    "record_id": "result",
    "operation_id": "result",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "tool_result",
    "category": "tool_result",
    "role": "tool",
    "key": "call",
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T00:00:00Z",
    "version": "test",
    "payload": {
      "call_record_id": "call-definition",
      "status": "completed",
      "state": null,
      "source_record_id": null,
      "content": [
        {
          "kind": "text",
          "text": "File content"
        }
      ]
    }
  },
  {
    "session_id": "sql-acp-demo",
    "seq": 12,
    "record_id": "usage",
    "operation_id": "assistant",
    "operation_position": 3,
    "parent_operation_id": "attempt",
    "kind": "token_usage",
    "category": "operational",
    "role": null,
    "key": null,
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T00:00:00Z",
    "version": "test",
    "payload": {
      "usage": {
        "input_tokens": 12,
        "output_tokens": 3,
        "reasoning_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0
      }
    }
  },
  {
    "session_id": "sql-acp-demo",
    "seq": 13,
    "record_id": "question",
    "operation_id": "question",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "elicitation_request",
    "category": "operational",
    "role": null,
    "key": null,
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T00:00:00Z",
    "version": "test",
    "payload": {
      "source": "agent.test",
      "message": "Continue?",
      "mode": "form",
      "related_message_id": null,
      "call_id": null,
      "requested_schema": {
        "type": "object",
        "properties": {
          "confirm": {
            "type": "boolean"
          }
        }
      },
      "url": null
    }
  },
  {
    "session_id": "sql-acp-demo",
    "seq": 14,
    "record_id": "question-response",
    "operation_id": "question-response",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "elicitation_response",
    "category": "operational",
    "role": null,
    "key": null,
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T00:00:00Z",
    "version": "test",
    "payload": {
      "elicitation_id": "question",
      "action": "accept",
      "content": {
        "confirm": true
      }
    }
  },
  {
    "session_id": "sql-acp-demo",
    "seq": 15,
    "record_id": "command-added",
    "operation_id": "command-added",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "command_added",
    "category": "operational",
    "role": null,
    "key": "compact",
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T00:00:00Z",
    "version": "test",
    "payload": {
      "description": "Compact",
      "owner": {
        "kind": "agent"
      },
      "input_hint": null
    }
  },
  {
    "session_id": "sql-acp-demo",
    "seq": 16,
    "record_id": "command",
    "operation_id": "command",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "slash_command",
    "category": "operational",
    "role": null,
    "key": null,
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T00:00:00Z",
    "version": "test",
    "payload": {
      "name": "compact",
      "text": "/compact",
      "arguments": {},
      "owner": {
        "kind": "agent"
      }
    }
  },
  {
    "session_id": "sql-acp-demo",
    "seq": 17,
    "record_id": "command-result",
    "operation_id": "command-result",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "slash_command_result",
    "category": "operational",
    "role": null,
    "key": null,
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T00:00:00Z",
    "version": "test",
    "payload": {
      "command_id": "command",
      "outcome": "succeeded",
      "content": [
        {
          "kind": "text",
          "text": "Done"
        }
      ],
      "structured_content": null,
      "error": null
    }
  },
  {
    "session_id": "sql-acp-demo",
    "seq": 18,
    "record_id": "warning",
    "operation_id": "warning",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "notice",
    "category": "operational",
    "role": null,
    "key": null,
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T00:00:00Z",
    "version": "test",
    "payload": {
      "level": "warning",
      "content": [
        {
          "kind": "text",
          "text": "Example warning"
        }
      ]
    }
  },
  {
    "session_id": "sql-acp-demo",
    "seq": 19,
    "record_id": "retry",
    "operation_id": "retry",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "llm_request_attempt",
    "category": "operational",
    "role": null,
    "key": null,
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T00:00:00Z",
    "version": "test",
    "payload": {
      "level": "warning",
      "content": [
        {
          "kind": "text",
          "text": "Retrying"
        }
      ],
      "attempt": 1,
      "status": "failed",
      "started_at": "2026-11-16T00:00:00Z",
      "method": "POST",
      "url": "https://example.test/chat/completions",
      "status_code": 503,
      "duration_ms": 10,
      "error": "busy",
      "retry_after_ms": null,
      "request_body": null,
      "response_body": null
    }
  },
  {
    "session_id": "sql-acp-demo",
    "seq": 20,
    "record_id": "turn-end",
    "operation_id": "turn-end",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "turn_end",
    "category": "operational",
    "role": null,
    "key": null,
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T00:00:00Z",
    "version": "test",
    "payload": {
      "turn_id": "turn",
      "outcome": "completed",
      "duration_ms": 100,
      "error": null
    }
  },
  {
    "session_id": "sql-acp-actions",
    "seq": 1,
    "record_id": "model",
    "operation_id": "model",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "model",
    "category": "setting",
    "role": null,
    "key": "model",
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T00:00:00Z",
    "version": "test",
    "payload": {
      "value": {
        "model": "test"
      },
      "provider": "test",
      "profile_id": null
    },
    "scope": "session"
  },
  {
    "session_id": "sql-acp-actions",
    "seq": 2,
    "record_id": "origin",
    "operation_id": "origin",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "notice",
    "category": "operational",
    "role": null,
    "key": null,
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T00:00:00Z",
    "version": "test",
    "payload": {
      "level": "info",
      "content": [
        {
          "kind": "text",
          "text": "Origin"
        }
      ]
    }
  },
  {
    "session_id": "sql-acp-actions",
    "seq": 3,
    "record_id": "assistant",
    "operation_id": "assistant",
    "operation_position": 0,
    "parent_operation_id": "origin",
    "kind": "assistant_message",
    "category": "message",
    "role": "assistant",
    "key": null,
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T00:00:00Z",
    "version": "test",
    "payload": {
      "content": [
        {
          "kind": "tool_call_reference",
          "call_id": "call"
        }
      ],
      "completion": "complete"
    },
    "scope": "session"
  },
  {
    "session_id": "sql-acp-actions",
    "seq": 4,
    "record_id": "call-definition",
    "operation_id": "assistant",
    "operation_position": 1,
    "parent_operation_id": "origin",
    "kind": "tool_call",
    "category": "tool_call",
    "role": "assistant",
    "key": "call",
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T00:00:00Z",
    "version": "test",
    "payload": {
      "assistant_record_id": "assistant",
      "name": "read",
      "arguments": {
        "path": "app.py"
      },
      "task_id": null
    }
  },
  {
    "session_id": "sql-acp-actions",
    "seq": 5,
    "record_id": "pending",
    "operation_id": "assistant",
    "operation_position": 2,
    "parent_operation_id": "origin",
    "kind": "tool_result",
    "category": "tool_result",
    "role": "tool",
    "key": "call",
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T00:00:00Z",
    "version": "test",
    "payload": {
      "call_record_id": "call-definition",
      "status": "pending",
      "state": null,
      "source_record_id": null,
      "content": [
        {
          "kind": "text",
          "text": "Pending"
        }
      ]
    }
  },
  {
    "session_id": "sql-acp-actions",
    "seq": 6,
    "record_id": "permission",
    "operation_id": "permission",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "permission_request",
    "category": "operational",
    "role": null,
    "key": null,
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T00:00:00Z",
    "version": "test",
    "payload": {
      "related_message_id": "assistant",
      "call_id": "call",
      "title": "Read file",
      "description": null,
      "options": [
        {
          "option_id": "allow",
          "name": "Allow",
          "decision": "allow",
          "scope": "call"
        }
      ]
    }
  },
  {
    "session_id": "sql-acp-actions",
    "seq": 7,
    "record_id": "form",
    "operation_id": "form",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "elicitation_request",
    "category": "operational",
    "role": null,
    "key": null,
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T00:00:00Z",
    "version": "test",
    "payload": {
      "source": "agent.test",
      "message": "Continue?",
      "mode": "form",
      "related_message_id": null,
      "call_id": null,
      "requested_schema": {
        "type": "object",
        "properties": {
          "confirm": {
            "type": "boolean"
          }
        }
      },
      "url": null
    }
  },
  {
    "session_id": "sql-acp-actions",
    "seq": 8,
    "record_id": "url",
    "operation_id": "url",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "elicitation_request",
    "category": "operational",
    "role": null,
    "key": null,
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T00:00:00Z",
    "version": "test",
    "payload": {
      "source": "agent.test",
      "message": "Authorize service",
      "mode": "url",
      "related_message_id": null,
      "call_id": null,
      "requested_schema": null,
      "url": "https://example.test/authorize"
    }
  }
]$records$::jsonb) as r(
        session_id text,
        seq bigint,
        record_id text,
        operation_id text,
        operation_position integer,
        parent_operation_id text,
        kind text,
        category text,
        role text,
        key text,
        operation text,
        scope text,
        copied_from_id text,
        created_at timestamptz,
        version text,
        payload jsonb
    );

commit;
