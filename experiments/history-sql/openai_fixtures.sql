-- Isolated target demo database only; run migrations before this fixture.
begin;
delete from
    boba.records
where 1=1
    and session_id in ('openai-tasks', 'openai-tasks-before-completion', 'openai-error-source', 'openai-error-missing', 'openai-error-chain', 'openai-scope', 'openai-tools', 'openai-error-stale-schema', 'openai-compact', 'openai-task-compact', 'openai-media', 'openai-error-unloaded', 'openai-error-category', 'openai-error-parameter');
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
    scope,
    key,
    operation,
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
    r.scope,
    r.key,
    r.operation,
    r.copied_from_id,
    r.created_at,
    r.version,
    r.payload
from
    jsonb_populate_recordset(null::boba.records, $records$
[
  {
    "session_id": "openai-tasks",
    "seq": 1,
    "record_id": "model",
    "operation_id": "op-model",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "model",
    "category": "setting",
    "role": null,
    "key": "model",
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "value": {
        "model": "test-model"
      },
      "provider": "demo",
      "profile_id": "general"
    },
    "scope": "session"
  },
  {
    "session_id": "openai-tasks",
    "seq": 2,
    "record_id": "sampling",
    "operation_id": "op-sampling",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "sampling",
    "category": "setting",
    "role": null,
    "key": "sampling",
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "value": {
        "temperature": 0.2
      }
    },
    "scope": "session"
  },
  {
    "session_id": "openai-tasks",
    "seq": 3,
    "record_id": "system",
    "operation_id": "op-system",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "system_prompt_added",
    "category": "prompt",
    "role": "system",
    "key": "core",
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "content": [
        {
          "kind": "text",
          "text": "You are a helpful assistant.\r\n"
        }
      ],
      "source_uri": null
    },
    "scope": "session"
  },
  {
    "session_id": "openai-tasks",
    "seq": 4,
    "record_id": "add-mcp__workspace__run",
    "operation_id": "op-add-mcp__workspace__run",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "tool_added",
    "category": "tool_catalog",
    "role": null,
    "key": "mcp__workspace__run",
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "tool": {
        "description": "Demo tool"
      }
    }
  },
  {
    "session_id": "openai-tasks",
    "seq": 5,
    "record_id": "schema-mcp__workspace__run",
    "operation_id": "op-schema-mcp__workspace__run",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "tool_schema",
    "category": "tool_schema",
    "role": null,
    "key": "mcp__workspace__run",
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "catalog_revision": "add-mcp__workspace__run",
      "tool": {
        "description": "Demo tool",
        "parameters": {
          "type": "object",
          "properties": {}
        },
        "deferred": false
      }
    }
  },
  {
    "session_id": "openai-tasks",
    "seq": 6,
    "record_id": "add-task_status",
    "operation_id": "op-add-task_status",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "tool_added",
    "category": "tool_catalog",
    "role": null,
    "key": "task_status",
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "tool": {
        "description": "Demo tool"
      }
    }
  },
  {
    "session_id": "openai-tasks",
    "seq": 7,
    "record_id": "schema-task_status",
    "operation_id": "op-schema-task_status",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "tool_schema",
    "category": "tool_schema",
    "role": null,
    "key": "task_status",
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "catalog_revision": "add-task_status",
      "tool": {
        "description": "Demo tool",
        "parameters": {
          "type": "object",
          "properties": {}
        },
        "deferred": false
      }
    }
  },
  {
    "session_id": "openai-tasks",
    "seq": 8,
    "record_id": "user-start",
    "operation_id": "op-user-start",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "user_message",
    "category": "message",
    "role": "user",
    "key": null,
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "content": [
        {
          "kind": "text",
          "text": "Start three tasks.\nПривет  世界"
        }
      ]
    },
    "scope": "session"
  },
  {
    "session_id": "openai-tasks",
    "seq": 9,
    "record_id": "attempt-answer-1",
    "operation_id": "op-attempt-answer-1",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "llm_request_attempt",
    "category": "operational",
    "role": null,
    "key": null,
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "level": "warning",
      "content": [
        {
          "kind": "text",
          "text": "Модель временно недоступна. Повтор через 2 секунды."
        }
      ],
      "attempt": 1,
      "status": "completed",
      "started_at": "2026-11-16T09:00:00Z",
      "method": "POST",
      "url": "https://example.test/inference",
      "status_code": 200,
      "duration_ms": 10,
      "error": null,
      "retry_after_ms": 2000,
      "request_body": null,
      "response_body": null
    }
  },
  {
    "session_id": "openai-tasks",
    "seq": 10,
    "record_id": "answer-1",
    "operation_id": "answer-1",
    "operation_position": 0,
    "parent_operation_id": "op-attempt-answer-1",
    "kind": "assistant_message",
    "category": "message",
    "role": "assistant",
    "key": null,
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "content": [
        {
          "kind": "text",
          "text": "Starting tasks."
        },
        {
          "kind": "tool_call_reference",
          "call_id": "call-1"
        },
        {
          "kind": "tool_call_reference",
          "call_id": "call-2"
        },
        {
          "kind": "tool_call_reference",
          "call_id": "call-3"
        }
      ],
      "completion": "complete"
    },
    "scope": "session"
  },
  {
    "session_id": "openai-tasks",
    "seq": 11,
    "record_id": "invoke-call-1",
    "operation_id": "answer-1",
    "operation_position": 1,
    "parent_operation_id": "op-attempt-answer-1",
    "kind": "tool_call",
    "category": "tool_call",
    "role": "assistant",
    "key": "call-1",
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "assistant_record_id": "answer-1",
      "name": "mcp__workspace__run",
      "arguments": {
        "task_id": "task-1"
      },
      "task_id": "task-1"
    }
  },
  {
    "session_id": "openai-tasks",
    "seq": 12,
    "record_id": "invoke-call-2",
    "operation_id": "answer-1",
    "operation_position": 2,
    "parent_operation_id": "op-attempt-answer-1",
    "kind": "tool_call",
    "category": "tool_call",
    "role": "assistant",
    "key": "call-2",
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "assistant_record_id": "answer-1",
      "name": "mcp__workspace__run",
      "arguments": {
        "task_id": "task-2"
      },
      "task_id": "task-2"
    }
  },
  {
    "session_id": "openai-tasks",
    "seq": 13,
    "record_id": "invoke-call-3",
    "operation_id": "answer-1",
    "operation_position": 3,
    "parent_operation_id": "op-attempt-answer-1",
    "kind": "tool_call",
    "category": "tool_call",
    "role": "assistant",
    "key": "call-3",
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "assistant_record_id": "answer-1",
      "name": "mcp__workspace__run",
      "arguments": {
        "task_id": "task-3"
      },
      "task_id": "task-3"
    }
  },
  {
    "session_id": "openai-tasks",
    "seq": 14,
    "record_id": "pending-call-1",
    "operation_id": "answer-1",
    "operation_position": 4,
    "parent_operation_id": "op-attempt-answer-1",
    "kind": "tool_result",
    "category": "tool_result",
    "role": "tool",
    "key": "call-1",
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "call_record_id": "invoke-call-1",
      "status": "pending",
      "state": {
        "task_id": "task-1",
        "revision": 1,
        "description": "task-1",
        "status": "pending",
        "execution_id": null,
        "child_session_id": null,
        "output_uri": null,
        "result": null,
        "error": null
      },
      "source_record_id": null,
      "content": [
        {
          "kind": "text",
          "text": "call-1: pending"
        }
      ]
    }
  },
  {
    "session_id": "openai-tasks",
    "seq": 15,
    "record_id": "pending-call-2",
    "operation_id": "answer-1",
    "operation_position": 5,
    "parent_operation_id": "op-attempt-answer-1",
    "kind": "tool_result",
    "category": "tool_result",
    "role": "tool",
    "key": "call-2",
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "call_record_id": "invoke-call-2",
      "status": "pending",
      "state": {
        "task_id": "task-2",
        "revision": 1,
        "description": "task-2",
        "status": "pending",
        "execution_id": null,
        "child_session_id": null,
        "output_uri": null,
        "result": null,
        "error": null
      },
      "source_record_id": null,
      "content": [
        {
          "kind": "text",
          "text": "call-2: pending"
        }
      ]
    }
  },
  {
    "session_id": "openai-tasks",
    "seq": 16,
    "record_id": "pending-call-3",
    "operation_id": "answer-1",
    "operation_position": 6,
    "parent_operation_id": "op-attempt-answer-1",
    "kind": "tool_result",
    "category": "tool_result",
    "role": "tool",
    "key": "call-3",
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "call_record_id": "invoke-call-3",
      "status": "pending",
      "state": {
        "task_id": "task-3",
        "revision": 1,
        "description": "task-3",
        "status": "pending",
        "execution_id": null,
        "child_session_id": null,
        "output_uri": null,
        "result": null,
        "error": null
      },
      "source_record_id": null,
      "content": [
        {
          "kind": "text",
          "text": "call-3: pending"
        }
      ]
    }
  },
  {
    "session_id": "openai-tasks",
    "seq": 17,
    "record_id": "user-0",
    "operation_id": "op-user-0",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "user_message",
    "category": "message",
    "role": "user",
    "key": null,
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "content": [
        {
          "kind": "text",
          "text": "Next user input 0"
        }
      ]
    },
    "scope": "session"
  },
  {
    "session_id": "openai-tasks",
    "seq": 18,
    "record_id": "user-1",
    "operation_id": "op-user-1",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "user_message",
    "category": "message",
    "role": "user",
    "key": null,
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "content": [
        {
          "kind": "text",
          "text": "Next user input 1"
        }
      ]
    },
    "scope": "session"
  },
  {
    "session_id": "openai-tasks",
    "seq": 19,
    "record_id": "user-2",
    "operation_id": "op-user-2",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "user_message",
    "category": "message",
    "role": "user",
    "key": null,
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "content": [
        {
          "kind": "text",
          "text": "Next user input 2"
        }
      ]
    },
    "scope": "session"
  },
  {
    "session_id": "openai-tasks",
    "seq": 20,
    "record_id": "memory",
    "operation_id": "op-memory",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "memory_added",
    "category": "context",
    "role": "user",
    "key": "memory:format",
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "content": [
        {
          "kind": "text",
          "text": "Preserve source formatting."
        }
      ],
      "source_uri": "https://example.org/workspaces/boba/memory/project"
    },
    "scope": "session"
  },
  {
    "session_id": "openai-tasks",
    "seq": 21,
    "record_id": "notice",
    "operation_id": "op-notice",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "notice",
    "category": "operational",
    "role": null,
    "key": null,
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "level": "info",
      "content": [
        {
          "kind": "text",
          "text": "Diagnostic outside the prompt."
        }
      ]
    }
  },
  {
    "session_id": "openai-tasks",
    "seq": 22,
    "record_id": "running-1",
    "operation_id": "op-running-1",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "tool_result",
    "category": "tool_result",
    "role": "tool",
    "key": "call-1",
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "call_record_id": "invoke-call-1",
      "status": "running",
      "state": {
        "task_id": "task-1",
        "revision": 2,
        "description": "task-1",
        "status": "running",
        "execution_id": null,
        "child_session_id": null,
        "output_uri": null,
        "result": null,
        "error": null
      },
      "source_record_id": null,
      "content": [
        {
          "kind": "text",
          "text": "call-1: running"
        }
      ]
    }
  },
  {
    "session_id": "openai-tasks",
    "seq": 23,
    "record_id": "failed-2",
    "operation_id": "op-failed-2",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "tool_result",
    "category": "tool_result",
    "role": "tool",
    "key": "call-2",
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "call_record_id": "invoke-call-2",
      "status": "failed",
      "state": {
        "task_id": "task-2",
        "revision": 2,
        "description": "task-2",
        "status": "failed",
        "execution_id": null,
        "child_session_id": null,
        "output_uri": null,
        "result": null,
        "error": "Demo execution failed"
      },
      "source_record_id": null,
      "content": [
        {
          "kind": "text",
          "text": "call-2: failed"
        }
      ]
    }
  },
  {
    "session_id": "openai-tasks",
    "seq": 24,
    "record_id": "completed-3",
    "operation_id": "op-completed-3",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "tool_result",
    "category": "tool_result",
    "role": "tool",
    "key": "call-3",
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "call_record_id": "invoke-call-3",
      "status": "completed",
      "state": {
        "task_id": "task-3",
        "revision": 2,
        "description": "task-3",
        "status": "completed",
        "execution_id": null,
        "child_session_id": null,
        "output_uri": null,
        "result": null,
        "error": null
      },
      "source_record_id": null,
      "content": [
        {
          "kind": "text",
          "text": "call-3: completed"
        }
      ]
    }
  },
  {
    "session_id": "openai-tasks",
    "seq": 25,
    "record_id": "attempt-poll-answer",
    "operation_id": "op-attempt-poll-answer",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "llm_request_attempt",
    "category": "operational",
    "role": null,
    "key": null,
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "level": "warning",
      "content": [
        {
          "kind": "text",
          "text": "Модель временно недоступна. Повтор через 2 секунды."
        }
      ],
      "attempt": 1,
      "status": "completed",
      "started_at": "2026-11-16T09:00:00Z",
      "method": "POST",
      "url": "https://example.test/inference",
      "status_code": 200,
      "duration_ms": 10,
      "error": null,
      "retry_after_ms": 2000,
      "request_body": null,
      "response_body": null
    }
  },
  {
    "session_id": "openai-tasks",
    "seq": 26,
    "record_id": "poll-answer",
    "operation_id": "poll-answer",
    "operation_position": 0,
    "parent_operation_id": "op-attempt-poll-answer",
    "kind": "assistant_message",
    "category": "message",
    "role": "assistant",
    "key": null,
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "content": [
        {
          "kind": "tool_call_reference",
          "call_id": "poll"
        }
      ],
      "completion": "complete"
    },
    "scope": "session"
  },
  {
    "session_id": "openai-tasks",
    "seq": 27,
    "record_id": "invoke-poll",
    "operation_id": "poll-answer",
    "operation_position": 1,
    "parent_operation_id": "op-attempt-poll-answer",
    "kind": "tool_call",
    "category": "tool_call",
    "role": "assistant",
    "key": "poll",
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "assistant_record_id": "poll-answer",
      "name": "task_status",
      "arguments": {
        "task_id": null
      },
      "task_id": null
    }
  },
  {
    "session_id": "openai-tasks",
    "seq": 28,
    "record_id": "polled-1",
    "operation_id": "observe-poll",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "tool_result",
    "category": "tool_result",
    "role": "tool",
    "key": "call-1",
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "call_record_id": "invoke-call-1",
      "status": "running",
      "state": {
        "task_id": "task-1",
        "revision": 2,
        "description": "task-1",
        "status": "running",
        "execution_id": null,
        "child_session_id": null,
        "output_uri": null,
        "result": null,
        "error": null
      },
      "source_record_id": null,
      "content": [
        {
          "kind": "text",
          "text": "call-1: running"
        }
      ]
    }
  },
  {
    "session_id": "openai-tasks",
    "seq": 29,
    "record_id": "poll-result",
    "operation_id": "observe-poll",
    "operation_position": 1,
    "parent_operation_id": null,
    "kind": "tool_result",
    "category": "tool_result",
    "role": "tool",
    "key": "poll",
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "call_record_id": "invoke-poll",
      "status": "completed",
      "state": null,
      "source_record_id": "polled-1",
      "content": []
    }
  },
  {
    "session_id": "openai-tasks",
    "seq": 30,
    "record_id": "completed-1",
    "operation_id": "op-completed-1",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "tool_result",
    "category": "tool_result",
    "role": "tool",
    "key": "call-1",
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "call_record_id": "invoke-call-1",
      "status": "completed",
      "state": {
        "task_id": "task-1",
        "revision": 3,
        "description": "task-1",
        "status": "completed",
        "execution_id": null,
        "child_session_id": null,
        "output_uri": null,
        "result": null,
        "error": null
      },
      "source_record_id": null,
      "content": [
        {
          "kind": "text",
          "text": "call-1: completed"
        }
      ]
    }
  },
  {
    "session_id": "openai-tasks-before-completion",
    "seq": 1,
    "record_id": "model",
    "operation_id": "op-model",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "model",
    "category": "setting",
    "role": null,
    "key": "model",
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "value": {
        "model": "test-model"
      },
      "provider": "demo",
      "profile_id": "general"
    },
    "scope": "session"
  },
  {
    "session_id": "openai-tasks-before-completion",
    "seq": 2,
    "record_id": "sampling",
    "operation_id": "op-sampling",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "sampling",
    "category": "setting",
    "role": null,
    "key": "sampling",
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "value": {
        "temperature": 0.2
      }
    },
    "scope": "session"
  },
  {
    "session_id": "openai-tasks-before-completion",
    "seq": 3,
    "record_id": "system",
    "operation_id": "op-system",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "system_prompt_added",
    "category": "prompt",
    "role": "system",
    "key": "core",
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "content": [
        {
          "kind": "text",
          "text": "You are a helpful assistant.\r\n"
        }
      ],
      "source_uri": null
    },
    "scope": "session"
  },
  {
    "session_id": "openai-tasks-before-completion",
    "seq": 4,
    "record_id": "add-mcp__workspace__run",
    "operation_id": "op-add-mcp__workspace__run",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "tool_added",
    "category": "tool_catalog",
    "role": null,
    "key": "mcp__workspace__run",
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "tool": {
        "description": "Demo tool"
      }
    }
  },
  {
    "session_id": "openai-tasks-before-completion",
    "seq": 5,
    "record_id": "schema-mcp__workspace__run",
    "operation_id": "op-schema-mcp__workspace__run",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "tool_schema",
    "category": "tool_schema",
    "role": null,
    "key": "mcp__workspace__run",
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "catalog_revision": "add-mcp__workspace__run",
      "tool": {
        "description": "Demo tool",
        "parameters": {
          "type": "object",
          "properties": {}
        },
        "deferred": false
      }
    }
  },
  {
    "session_id": "openai-tasks-before-completion",
    "seq": 6,
    "record_id": "add-task_status",
    "operation_id": "op-add-task_status",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "tool_added",
    "category": "tool_catalog",
    "role": null,
    "key": "task_status",
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "tool": {
        "description": "Demo tool"
      }
    }
  },
  {
    "session_id": "openai-tasks-before-completion",
    "seq": 7,
    "record_id": "schema-task_status",
    "operation_id": "op-schema-task_status",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "tool_schema",
    "category": "tool_schema",
    "role": null,
    "key": "task_status",
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "catalog_revision": "add-task_status",
      "tool": {
        "description": "Demo tool",
        "parameters": {
          "type": "object",
          "properties": {}
        },
        "deferred": false
      }
    }
  },
  {
    "session_id": "openai-tasks-before-completion",
    "seq": 8,
    "record_id": "user-start",
    "operation_id": "op-user-start",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "user_message",
    "category": "message",
    "role": "user",
    "key": null,
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "content": [
        {
          "kind": "text",
          "text": "Start three tasks.\nПривет  世界"
        }
      ]
    },
    "scope": "session"
  },
  {
    "session_id": "openai-tasks-before-completion",
    "seq": 9,
    "record_id": "attempt-answer-1",
    "operation_id": "op-attempt-answer-1",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "llm_request_attempt",
    "category": "operational",
    "role": null,
    "key": null,
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "level": "warning",
      "content": [
        {
          "kind": "text",
          "text": "Модель временно недоступна. Повтор через 2 секунды."
        }
      ],
      "attempt": 1,
      "status": "completed",
      "started_at": "2026-11-16T09:00:00Z",
      "method": "POST",
      "url": "https://example.test/inference",
      "status_code": 200,
      "duration_ms": 10,
      "error": null,
      "retry_after_ms": 2000,
      "request_body": null,
      "response_body": null
    }
  },
  {
    "session_id": "openai-tasks-before-completion",
    "seq": 10,
    "record_id": "answer-1",
    "operation_id": "answer-1",
    "operation_position": 0,
    "parent_operation_id": "op-attempt-answer-1",
    "kind": "assistant_message",
    "category": "message",
    "role": "assistant",
    "key": null,
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "content": [
        {
          "kind": "text",
          "text": "Starting tasks."
        },
        {
          "kind": "tool_call_reference",
          "call_id": "call-1"
        },
        {
          "kind": "tool_call_reference",
          "call_id": "call-2"
        },
        {
          "kind": "tool_call_reference",
          "call_id": "call-3"
        }
      ],
      "completion": "complete"
    },
    "scope": "session"
  },
  {
    "session_id": "openai-tasks-before-completion",
    "seq": 11,
    "record_id": "invoke-call-1",
    "operation_id": "answer-1",
    "operation_position": 1,
    "parent_operation_id": "op-attempt-answer-1",
    "kind": "tool_call",
    "category": "tool_call",
    "role": "assistant",
    "key": "call-1",
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "assistant_record_id": "answer-1",
      "name": "mcp__workspace__run",
      "arguments": {
        "task_id": "task-1"
      },
      "task_id": "task-1"
    }
  },
  {
    "session_id": "openai-tasks-before-completion",
    "seq": 12,
    "record_id": "invoke-call-2",
    "operation_id": "answer-1",
    "operation_position": 2,
    "parent_operation_id": "op-attempt-answer-1",
    "kind": "tool_call",
    "category": "tool_call",
    "role": "assistant",
    "key": "call-2",
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "assistant_record_id": "answer-1",
      "name": "mcp__workspace__run",
      "arguments": {
        "task_id": "task-2"
      },
      "task_id": "task-2"
    }
  },
  {
    "session_id": "openai-tasks-before-completion",
    "seq": 13,
    "record_id": "invoke-call-3",
    "operation_id": "answer-1",
    "operation_position": 3,
    "parent_operation_id": "op-attempt-answer-1",
    "kind": "tool_call",
    "category": "tool_call",
    "role": "assistant",
    "key": "call-3",
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "assistant_record_id": "answer-1",
      "name": "mcp__workspace__run",
      "arguments": {
        "task_id": "task-3"
      },
      "task_id": "task-3"
    }
  },
  {
    "session_id": "openai-tasks-before-completion",
    "seq": 14,
    "record_id": "pending-call-1",
    "operation_id": "answer-1",
    "operation_position": 4,
    "parent_operation_id": "op-attempt-answer-1",
    "kind": "tool_result",
    "category": "tool_result",
    "role": "tool",
    "key": "call-1",
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "call_record_id": "invoke-call-1",
      "status": "pending",
      "state": {
        "task_id": "task-1",
        "revision": 1,
        "description": "task-1",
        "status": "pending",
        "execution_id": null,
        "child_session_id": null,
        "output_uri": null,
        "result": null,
        "error": null
      },
      "source_record_id": null,
      "content": [
        {
          "kind": "text",
          "text": "call-1: pending"
        }
      ]
    }
  },
  {
    "session_id": "openai-tasks-before-completion",
    "seq": 15,
    "record_id": "pending-call-2",
    "operation_id": "answer-1",
    "operation_position": 5,
    "parent_operation_id": "op-attempt-answer-1",
    "kind": "tool_result",
    "category": "tool_result",
    "role": "tool",
    "key": "call-2",
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "call_record_id": "invoke-call-2",
      "status": "pending",
      "state": {
        "task_id": "task-2",
        "revision": 1,
        "description": "task-2",
        "status": "pending",
        "execution_id": null,
        "child_session_id": null,
        "output_uri": null,
        "result": null,
        "error": null
      },
      "source_record_id": null,
      "content": [
        {
          "kind": "text",
          "text": "call-2: pending"
        }
      ]
    }
  },
  {
    "session_id": "openai-tasks-before-completion",
    "seq": 16,
    "record_id": "pending-call-3",
    "operation_id": "answer-1",
    "operation_position": 6,
    "parent_operation_id": "op-attempt-answer-1",
    "kind": "tool_result",
    "category": "tool_result",
    "role": "tool",
    "key": "call-3",
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "call_record_id": "invoke-call-3",
      "status": "pending",
      "state": {
        "task_id": "task-3",
        "revision": 1,
        "description": "task-3",
        "status": "pending",
        "execution_id": null,
        "child_session_id": null,
        "output_uri": null,
        "result": null,
        "error": null
      },
      "source_record_id": null,
      "content": [
        {
          "kind": "text",
          "text": "call-3: pending"
        }
      ]
    }
  },
  {
    "session_id": "openai-tasks-before-completion",
    "seq": 17,
    "record_id": "user-0",
    "operation_id": "op-user-0",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "user_message",
    "category": "message",
    "role": "user",
    "key": null,
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "content": [
        {
          "kind": "text",
          "text": "Next user input 0"
        }
      ]
    },
    "scope": "session"
  },
  {
    "session_id": "openai-tasks-before-completion",
    "seq": 18,
    "record_id": "user-1",
    "operation_id": "op-user-1",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "user_message",
    "category": "message",
    "role": "user",
    "key": null,
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "content": [
        {
          "kind": "text",
          "text": "Next user input 1"
        }
      ]
    },
    "scope": "session"
  },
  {
    "session_id": "openai-tasks-before-completion",
    "seq": 19,
    "record_id": "user-2",
    "operation_id": "op-user-2",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "user_message",
    "category": "message",
    "role": "user",
    "key": null,
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "content": [
        {
          "kind": "text",
          "text": "Next user input 2"
        }
      ]
    },
    "scope": "session"
  },
  {
    "session_id": "openai-tasks-before-completion",
    "seq": 20,
    "record_id": "memory",
    "operation_id": "op-memory",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "memory_added",
    "category": "context",
    "role": "user",
    "key": "memory:format",
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "content": [
        {
          "kind": "text",
          "text": "Preserve source formatting."
        }
      ],
      "source_uri": "https://example.org/workspaces/boba/memory/project"
    },
    "scope": "session"
  },
  {
    "session_id": "openai-tasks-before-completion",
    "seq": 21,
    "record_id": "notice",
    "operation_id": "op-notice",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "notice",
    "category": "operational",
    "role": null,
    "key": null,
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "level": "info",
      "content": [
        {
          "kind": "text",
          "text": "Diagnostic outside the prompt."
        }
      ]
    }
  },
  {
    "session_id": "openai-tasks-before-completion",
    "seq": 22,
    "record_id": "running-1",
    "operation_id": "op-running-1",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "tool_result",
    "category": "tool_result",
    "role": "tool",
    "key": "call-1",
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "call_record_id": "invoke-call-1",
      "status": "running",
      "state": {
        "task_id": "task-1",
        "revision": 2,
        "description": "task-1",
        "status": "running",
        "execution_id": null,
        "child_session_id": null,
        "output_uri": null,
        "result": null,
        "error": null
      },
      "source_record_id": null,
      "content": [
        {
          "kind": "text",
          "text": "call-1: running"
        }
      ]
    }
  },
  {
    "session_id": "openai-tasks-before-completion",
    "seq": 23,
    "record_id": "failed-2",
    "operation_id": "op-failed-2",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "tool_result",
    "category": "tool_result",
    "role": "tool",
    "key": "call-2",
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "call_record_id": "invoke-call-2",
      "status": "failed",
      "state": {
        "task_id": "task-2",
        "revision": 2,
        "description": "task-2",
        "status": "failed",
        "execution_id": null,
        "child_session_id": null,
        "output_uri": null,
        "result": null,
        "error": "Demo execution failed"
      },
      "source_record_id": null,
      "content": [
        {
          "kind": "text",
          "text": "call-2: failed"
        }
      ]
    }
  },
  {
    "session_id": "openai-tasks-before-completion",
    "seq": 24,
    "record_id": "completed-3",
    "operation_id": "op-completed-3",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "tool_result",
    "category": "tool_result",
    "role": "tool",
    "key": "call-3",
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "call_record_id": "invoke-call-3",
      "status": "completed",
      "state": {
        "task_id": "task-3",
        "revision": 2,
        "description": "task-3",
        "status": "completed",
        "execution_id": null,
        "child_session_id": null,
        "output_uri": null,
        "result": null,
        "error": null
      },
      "source_record_id": null,
      "content": [
        {
          "kind": "text",
          "text": "call-3: completed"
        }
      ]
    }
  },
  {
    "session_id": "openai-tasks-before-completion",
    "seq": 25,
    "record_id": "attempt-poll-answer",
    "operation_id": "op-attempt-poll-answer",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "llm_request_attempt",
    "category": "operational",
    "role": null,
    "key": null,
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "level": "warning",
      "content": [
        {
          "kind": "text",
          "text": "Модель временно недоступна. Повтор через 2 секунды."
        }
      ],
      "attempt": 1,
      "status": "completed",
      "started_at": "2026-11-16T09:00:00Z",
      "method": "POST",
      "url": "https://example.test/inference",
      "status_code": 200,
      "duration_ms": 10,
      "error": null,
      "retry_after_ms": 2000,
      "request_body": null,
      "response_body": null
    }
  },
  {
    "session_id": "openai-tasks-before-completion",
    "seq": 26,
    "record_id": "poll-answer",
    "operation_id": "poll-answer",
    "operation_position": 0,
    "parent_operation_id": "op-attempt-poll-answer",
    "kind": "assistant_message",
    "category": "message",
    "role": "assistant",
    "key": null,
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "content": [
        {
          "kind": "tool_call_reference",
          "call_id": "poll"
        }
      ],
      "completion": "complete"
    },
    "scope": "session"
  },
  {
    "session_id": "openai-tasks-before-completion",
    "seq": 27,
    "record_id": "invoke-poll",
    "operation_id": "poll-answer",
    "operation_position": 1,
    "parent_operation_id": "op-attempt-poll-answer",
    "kind": "tool_call",
    "category": "tool_call",
    "role": "assistant",
    "key": "poll",
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "assistant_record_id": "poll-answer",
      "name": "task_status",
      "arguments": {
        "task_id": null
      },
      "task_id": null
    }
  },
  {
    "session_id": "openai-tasks-before-completion",
    "seq": 28,
    "record_id": "polled-1",
    "operation_id": "observe-poll",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "tool_result",
    "category": "tool_result",
    "role": "tool",
    "key": "call-1",
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "call_record_id": "invoke-call-1",
      "status": "running",
      "state": {
        "task_id": "task-1",
        "revision": 2,
        "description": "task-1",
        "status": "running",
        "execution_id": null,
        "child_session_id": null,
        "output_uri": null,
        "result": null,
        "error": null
      },
      "source_record_id": null,
      "content": [
        {
          "kind": "text",
          "text": "call-1: running"
        }
      ]
    }
  },
  {
    "session_id": "openai-tasks-before-completion",
    "seq": 29,
    "record_id": "poll-result",
    "operation_id": "observe-poll",
    "operation_position": 1,
    "parent_operation_id": null,
    "kind": "tool_result",
    "category": "tool_result",
    "role": "tool",
    "key": "poll",
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "call_record_id": "invoke-poll",
      "status": "completed",
      "state": null,
      "source_record_id": "polled-1",
      "content": []
    }
  },
  {
    "session_id": "openai-error-source",
    "seq": 1,
    "record_id": "model",
    "operation_id": "op-model",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "model",
    "category": "setting",
    "role": null,
    "key": "model",
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "value": {
        "model": "test-model"
      },
      "provider": "demo",
      "profile_id": "general"
    },
    "scope": "session"
  },
  {
    "session_id": "openai-error-source",
    "seq": 2,
    "record_id": "sampling",
    "operation_id": "op-sampling",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "sampling",
    "category": "setting",
    "role": null,
    "key": "sampling",
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "value": {
        "temperature": 0.2
      }
    },
    "scope": "session"
  },
  {
    "session_id": "openai-error-source",
    "seq": 3,
    "record_id": "system",
    "operation_id": "op-system",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "system_prompt_added",
    "category": "prompt",
    "role": "system",
    "key": "core",
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "content": [
        {
          "kind": "text",
          "text": "You are a helpful assistant.\r\n"
        }
      ],
      "source_uri": null
    },
    "scope": "session"
  },
  {
    "session_id": "openai-error-source",
    "seq": 4,
    "record_id": "add-mcp__workspace__run",
    "operation_id": "op-add-mcp__workspace__run",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "tool_added",
    "category": "tool_catalog",
    "role": null,
    "key": "mcp__workspace__run",
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "tool": {
        "description": "Demo tool"
      }
    }
  },
  {
    "session_id": "openai-error-source",
    "seq": 5,
    "record_id": "schema-mcp__workspace__run",
    "operation_id": "op-schema-mcp__workspace__run",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "tool_schema",
    "category": "tool_schema",
    "role": null,
    "key": "mcp__workspace__run",
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "catalog_revision": "add-mcp__workspace__run",
      "tool": {
        "description": "Demo tool",
        "parameters": {
          "type": "object",
          "properties": {}
        },
        "deferred": false
      }
    }
  },
  {
    "session_id": "openai-error-source",
    "seq": 6,
    "record_id": "add-task_status",
    "operation_id": "op-add-task_status",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "tool_added",
    "category": "tool_catalog",
    "role": null,
    "key": "task_status",
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "tool": {
        "description": "Demo tool"
      }
    }
  },
  {
    "session_id": "openai-error-source",
    "seq": 7,
    "record_id": "schema-task_status",
    "operation_id": "op-schema-task_status",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "tool_schema",
    "category": "tool_schema",
    "role": null,
    "key": "task_status",
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "catalog_revision": "add-task_status",
      "tool": {
        "description": "Demo tool",
        "parameters": {
          "type": "object",
          "properties": {}
        },
        "deferred": false
      }
    }
  },
  {
    "session_id": "openai-error-source",
    "seq": 8,
    "record_id": "user-start",
    "operation_id": "op-user-start",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "user_message",
    "category": "message",
    "role": "user",
    "key": null,
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "content": [
        {
          "kind": "text",
          "text": "Start three tasks.\nПривет  世界"
        }
      ]
    },
    "scope": "session"
  },
  {
    "session_id": "openai-error-source",
    "seq": 9,
    "record_id": "attempt-answer-1",
    "operation_id": "op-attempt-answer-1",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "llm_request_attempt",
    "category": "operational",
    "role": null,
    "key": null,
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "level": "warning",
      "content": [
        {
          "kind": "text",
          "text": "Модель временно недоступна. Повтор через 2 секунды."
        }
      ],
      "attempt": 1,
      "status": "completed",
      "started_at": "2026-11-16T09:00:00Z",
      "method": "POST",
      "url": "https://example.test/inference",
      "status_code": 200,
      "duration_ms": 10,
      "error": null,
      "retry_after_ms": 2000,
      "request_body": null,
      "response_body": null
    }
  },
  {
    "session_id": "openai-error-source",
    "seq": 10,
    "record_id": "answer-1",
    "operation_id": "answer-1",
    "operation_position": 0,
    "parent_operation_id": "op-attempt-answer-1",
    "kind": "assistant_message",
    "category": "message",
    "role": "assistant",
    "key": null,
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "content": [
        {
          "kind": "text",
          "text": "Starting tasks."
        },
        {
          "kind": "tool_call_reference",
          "call_id": "call-1"
        },
        {
          "kind": "tool_call_reference",
          "call_id": "call-2"
        },
        {
          "kind": "tool_call_reference",
          "call_id": "call-3"
        }
      ],
      "completion": "complete"
    },
    "scope": "session"
  },
  {
    "session_id": "openai-error-source",
    "seq": 11,
    "record_id": "invoke-call-1",
    "operation_id": "answer-1",
    "operation_position": 1,
    "parent_operation_id": "op-attempt-answer-1",
    "kind": "tool_call",
    "category": "tool_call",
    "role": "assistant",
    "key": "call-1",
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "assistant_record_id": "answer-1",
      "name": "mcp__workspace__run",
      "arguments": {
        "task_id": "task-1"
      },
      "task_id": "task-1"
    }
  },
  {
    "session_id": "openai-error-source",
    "seq": 12,
    "record_id": "invoke-call-2",
    "operation_id": "answer-1",
    "operation_position": 2,
    "parent_operation_id": "op-attempt-answer-1",
    "kind": "tool_call",
    "category": "tool_call",
    "role": "assistant",
    "key": "call-2",
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "assistant_record_id": "answer-1",
      "name": "mcp__workspace__run",
      "arguments": {
        "task_id": "task-2"
      },
      "task_id": "task-2"
    }
  },
  {
    "session_id": "openai-error-source",
    "seq": 13,
    "record_id": "invoke-call-3",
    "operation_id": "answer-1",
    "operation_position": 3,
    "parent_operation_id": "op-attempt-answer-1",
    "kind": "tool_call",
    "category": "tool_call",
    "role": "assistant",
    "key": "call-3",
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "assistant_record_id": "answer-1",
      "name": "mcp__workspace__run",
      "arguments": {
        "task_id": "task-3"
      },
      "task_id": "task-3"
    }
  },
  {
    "session_id": "openai-error-source",
    "seq": 14,
    "record_id": "pending-call-1",
    "operation_id": "answer-1",
    "operation_position": 4,
    "parent_operation_id": "op-attempt-answer-1",
    "kind": "tool_result",
    "category": "tool_result",
    "role": "tool",
    "key": "call-1",
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "call_record_id": "invoke-call-1",
      "status": "pending",
      "state": {
        "task_id": "task-1",
        "revision": 1,
        "description": "task-1",
        "status": "pending",
        "execution_id": null,
        "child_session_id": null,
        "output_uri": null,
        "result": null,
        "error": null
      },
      "source_record_id": null,
      "content": [
        {
          "kind": "text",
          "text": "call-1: pending"
        }
      ]
    }
  },
  {
    "session_id": "openai-error-source",
    "seq": 15,
    "record_id": "pending-call-2",
    "operation_id": "answer-1",
    "operation_position": 5,
    "parent_operation_id": "op-attempt-answer-1",
    "kind": "tool_result",
    "category": "tool_result",
    "role": "tool",
    "key": "call-2",
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "call_record_id": "invoke-call-2",
      "status": "pending",
      "state": {
        "task_id": "task-2",
        "revision": 1,
        "description": "task-2",
        "status": "pending",
        "execution_id": null,
        "child_session_id": null,
        "output_uri": null,
        "result": null,
        "error": null
      },
      "source_record_id": null,
      "content": [
        {
          "kind": "text",
          "text": "call-2: pending"
        }
      ]
    }
  },
  {
    "session_id": "openai-error-source",
    "seq": 16,
    "record_id": "pending-call-3",
    "operation_id": "answer-1",
    "operation_position": 6,
    "parent_operation_id": "op-attempt-answer-1",
    "kind": "tool_result",
    "category": "tool_result",
    "role": "tool",
    "key": "call-3",
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "call_record_id": "invoke-call-3",
      "status": "pending",
      "state": {
        "task_id": "task-3",
        "revision": 1,
        "description": "task-3",
        "status": "pending",
        "execution_id": null,
        "child_session_id": null,
        "output_uri": null,
        "result": null,
        "error": null
      },
      "source_record_id": null,
      "content": [
        {
          "kind": "text",
          "text": "call-3: pending"
        }
      ]
    }
  },
  {
    "session_id": "openai-error-source",
    "seq": 17,
    "record_id": "user-0",
    "operation_id": "op-user-0",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "user_message",
    "category": "message",
    "role": "user",
    "key": null,
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "content": [
        {
          "kind": "text",
          "text": "Next user input 0"
        }
      ]
    },
    "scope": "session"
  },
  {
    "session_id": "openai-error-source",
    "seq": 18,
    "record_id": "user-1",
    "operation_id": "op-user-1",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "user_message",
    "category": "message",
    "role": "user",
    "key": null,
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "content": [
        {
          "kind": "text",
          "text": "Next user input 1"
        }
      ]
    },
    "scope": "session"
  },
  {
    "session_id": "openai-error-source",
    "seq": 19,
    "record_id": "user-2",
    "operation_id": "op-user-2",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "user_message",
    "category": "message",
    "role": "user",
    "key": null,
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "content": [
        {
          "kind": "text",
          "text": "Next user input 2"
        }
      ]
    },
    "scope": "session"
  },
  {
    "session_id": "openai-error-source",
    "seq": 20,
    "record_id": "memory",
    "operation_id": "op-memory",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "memory_added",
    "category": "context",
    "role": "user",
    "key": "memory:format",
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "content": [
        {
          "kind": "text",
          "text": "Preserve source formatting."
        }
      ],
      "source_uri": "https://example.org/workspaces/boba/memory/project"
    },
    "scope": "session"
  },
  {
    "session_id": "openai-error-source",
    "seq": 21,
    "record_id": "notice",
    "operation_id": "op-notice",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "notice",
    "category": "operational",
    "role": null,
    "key": null,
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "level": "info",
      "content": [
        {
          "kind": "text",
          "text": "Diagnostic outside the prompt."
        }
      ]
    }
  },
  {
    "session_id": "openai-error-source",
    "seq": 22,
    "record_id": "running-1",
    "operation_id": "op-running-1",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "tool_result",
    "category": "tool_result",
    "role": "tool",
    "key": "call-1",
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "call_record_id": "invoke-call-1",
      "status": "running",
      "state": {
        "task_id": "task-1",
        "revision": 2,
        "description": "task-1",
        "status": "running",
        "execution_id": null,
        "child_session_id": null,
        "output_uri": null,
        "result": null,
        "error": null
      },
      "source_record_id": null,
      "content": [
        {
          "kind": "text",
          "text": "call-1: running"
        }
      ]
    }
  },
  {
    "session_id": "openai-error-source",
    "seq": 23,
    "record_id": "failed-2",
    "operation_id": "op-failed-2",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "tool_result",
    "category": "tool_result",
    "role": "tool",
    "key": "call-2",
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "call_record_id": "invoke-call-2",
      "status": "failed",
      "state": {
        "task_id": "task-2",
        "revision": 2,
        "description": "task-2",
        "status": "failed",
        "execution_id": null,
        "child_session_id": null,
        "output_uri": null,
        "result": null,
        "error": "Demo execution failed"
      },
      "source_record_id": null,
      "content": [
        {
          "kind": "text",
          "text": "call-2: failed"
        }
      ]
    }
  },
  {
    "session_id": "openai-error-source",
    "seq": 24,
    "record_id": "completed-3",
    "operation_id": "op-completed-3",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "tool_result",
    "category": "tool_result",
    "role": "tool",
    "key": "call-3",
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "call_record_id": "invoke-call-3",
      "status": "completed",
      "state": {
        "task_id": "task-3",
        "revision": 2,
        "description": "task-3",
        "status": "completed",
        "execution_id": null,
        "child_session_id": null,
        "output_uri": null,
        "result": null,
        "error": null
      },
      "source_record_id": null,
      "content": [
        {
          "kind": "text",
          "text": "call-3: completed"
        }
      ]
    }
  },
  {
    "session_id": "openai-error-source",
    "seq": 25,
    "record_id": "attempt-poll-answer",
    "operation_id": "op-attempt-poll-answer",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "llm_request_attempt",
    "category": "operational",
    "role": null,
    "key": null,
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "level": "warning",
      "content": [
        {
          "kind": "text",
          "text": "Модель временно недоступна. Повтор через 2 секунды."
        }
      ],
      "attempt": 1,
      "status": "completed",
      "started_at": "2026-11-16T09:00:00Z",
      "method": "POST",
      "url": "https://example.test/inference",
      "status_code": 200,
      "duration_ms": 10,
      "error": null,
      "retry_after_ms": 2000,
      "request_body": null,
      "response_body": null
    }
  },
  {
    "session_id": "openai-error-source",
    "seq": 26,
    "record_id": "poll-answer",
    "operation_id": "poll-answer",
    "operation_position": 0,
    "parent_operation_id": "op-attempt-poll-answer",
    "kind": "assistant_message",
    "category": "message",
    "role": "assistant",
    "key": null,
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "content": [
        {
          "kind": "tool_call_reference",
          "call_id": "poll"
        }
      ],
      "completion": "complete"
    },
    "scope": "session"
  },
  {
    "session_id": "openai-error-source",
    "seq": 27,
    "record_id": "invoke-poll",
    "operation_id": "poll-answer",
    "operation_position": 1,
    "parent_operation_id": "op-attempt-poll-answer",
    "kind": "tool_call",
    "category": "tool_call",
    "role": "assistant",
    "key": "poll",
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "assistant_record_id": "poll-answer",
      "name": "task_status",
      "arguments": {
        "task_id": null
      },
      "task_id": null
    }
  },
  {
    "session_id": "openai-error-source",
    "seq": 28,
    "record_id": "polled-1",
    "operation_id": "observe-poll",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "tool_result",
    "category": "tool_result",
    "role": "tool",
    "key": "call-1",
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "call_record_id": "invoke-call-1",
      "status": "running",
      "state": {
        "task_id": "task-1",
        "revision": 2,
        "description": "task-1",
        "status": "running",
        "execution_id": null,
        "child_session_id": null,
        "output_uri": null,
        "result": null,
        "error": null
      },
      "source_record_id": null,
      "content": [
        {
          "kind": "text",
          "text": "call-1: running"
        }
      ]
    }
  },
  {
    "session_id": "openai-error-source",
    "seq": 29,
    "record_id": "poll-result",
    "operation_id": "observe-poll",
    "operation_position": 1,
    "parent_operation_id": null,
    "kind": "tool_result",
    "category": "tool_result",
    "role": "tool",
    "key": "poll",
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "call_record_id": "invoke-poll",
      "status": "completed",
      "state": null,
      "source_record_id": "absent-source",
      "content": []
    }
  },
  {
    "session_id": "openai-error-source",
    "seq": 30,
    "record_id": "completed-1",
    "operation_id": "op-completed-1",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "tool_result",
    "category": "tool_result",
    "role": "tool",
    "key": "call-1",
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "call_record_id": "invoke-call-1",
      "status": "completed",
      "state": {
        "task_id": "task-1",
        "revision": 3,
        "description": "task-1",
        "status": "completed",
        "execution_id": null,
        "child_session_id": null,
        "output_uri": null,
        "result": null,
        "error": null
      },
      "source_record_id": null,
      "content": [
        {
          "kind": "text",
          "text": "call-1: completed"
        }
      ]
    }
  },
  {
    "session_id": "openai-error-missing",
    "seq": 1,
    "record_id": "model",
    "operation_id": "op-model",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "model",
    "category": "setting",
    "role": null,
    "key": "model",
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "value": {
        "model": "test-model"
      },
      "provider": "demo",
      "profile_id": "general"
    },
    "scope": "session"
  },
  {
    "session_id": "openai-error-missing",
    "seq": 2,
    "record_id": "sampling",
    "operation_id": "op-sampling",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "sampling",
    "category": "setting",
    "role": null,
    "key": "sampling",
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "value": {
        "temperature": 0.2
      }
    },
    "scope": "session"
  },
  {
    "session_id": "openai-error-missing",
    "seq": 3,
    "record_id": "system",
    "operation_id": "op-system",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "system_prompt_added",
    "category": "prompt",
    "role": "system",
    "key": "core",
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "content": [
        {
          "kind": "text",
          "text": "You are a helpful assistant.\r\n"
        }
      ],
      "source_uri": null
    },
    "scope": "session"
  },
  {
    "session_id": "openai-error-missing",
    "seq": 4,
    "record_id": "add-mcp__workspace__run",
    "operation_id": "op-add-mcp__workspace__run",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "tool_added",
    "category": "tool_catalog",
    "role": null,
    "key": "mcp__workspace__run",
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "tool": {
        "description": "Demo tool"
      }
    }
  },
  {
    "session_id": "openai-error-missing",
    "seq": 5,
    "record_id": "schema-mcp__workspace__run",
    "operation_id": "op-schema-mcp__workspace__run",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "tool_schema",
    "category": "tool_schema",
    "role": null,
    "key": "mcp__workspace__run",
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "catalog_revision": "add-mcp__workspace__run",
      "tool": {
        "description": "Demo tool",
        "parameters": {
          "type": "object",
          "properties": {}
        },
        "deferred": false
      }
    }
  },
  {
    "session_id": "openai-error-missing",
    "seq": 6,
    "record_id": "add-task_status",
    "operation_id": "op-add-task_status",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "tool_added",
    "category": "tool_catalog",
    "role": null,
    "key": "task_status",
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "tool": {
        "description": "Demo tool"
      }
    }
  },
  {
    "session_id": "openai-error-missing",
    "seq": 7,
    "record_id": "schema-task_status",
    "operation_id": "op-schema-task_status",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "tool_schema",
    "category": "tool_schema",
    "role": null,
    "key": "task_status",
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "catalog_revision": "add-task_status",
      "tool": {
        "description": "Demo tool",
        "parameters": {
          "type": "object",
          "properties": {}
        },
        "deferred": false
      }
    }
  },
  {
    "session_id": "openai-error-missing",
    "seq": 8,
    "record_id": "user-start",
    "operation_id": "op-user-start",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "user_message",
    "category": "message",
    "role": "user",
    "key": null,
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "content": [
        {
          "kind": "text",
          "text": "Start three tasks.\nПривет  世界"
        }
      ]
    },
    "scope": "session"
  },
  {
    "session_id": "openai-error-missing",
    "seq": 9,
    "record_id": "attempt-answer-1",
    "operation_id": "op-attempt-answer-1",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "llm_request_attempt",
    "category": "operational",
    "role": null,
    "key": null,
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "level": "warning",
      "content": [
        {
          "kind": "text",
          "text": "Модель временно недоступна. Повтор через 2 секунды."
        }
      ],
      "attempt": 1,
      "status": "completed",
      "started_at": "2026-11-16T09:00:00Z",
      "method": "POST",
      "url": "https://example.test/inference",
      "status_code": 200,
      "duration_ms": 10,
      "error": null,
      "retry_after_ms": 2000,
      "request_body": null,
      "response_body": null
    }
  },
  {
    "session_id": "openai-error-missing",
    "seq": 10,
    "record_id": "answer-1",
    "operation_id": "answer-1",
    "operation_position": 0,
    "parent_operation_id": "op-attempt-answer-1",
    "kind": "assistant_message",
    "category": "message",
    "role": "assistant",
    "key": null,
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "content": [
        {
          "kind": "text",
          "text": "Starting tasks."
        },
        {
          "kind": "tool_call_reference",
          "call_id": "call-1"
        },
        {
          "kind": "tool_call_reference",
          "call_id": "call-2"
        },
        {
          "kind": "tool_call_reference",
          "call_id": "call-3"
        }
      ],
      "completion": "complete"
    },
    "scope": "session"
  },
  {
    "session_id": "openai-error-missing",
    "seq": 11,
    "record_id": "invoke-call-1",
    "operation_id": "answer-1",
    "operation_position": 1,
    "parent_operation_id": "op-attempt-answer-1",
    "kind": "tool_call",
    "category": "tool_call",
    "role": "assistant",
    "key": "call-1",
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "assistant_record_id": "answer-1",
      "name": "mcp__workspace__run",
      "arguments": {
        "task_id": "task-1"
      },
      "task_id": "task-1"
    }
  },
  {
    "session_id": "openai-error-missing",
    "seq": 12,
    "record_id": "invoke-call-2",
    "operation_id": "answer-1",
    "operation_position": 2,
    "parent_operation_id": "op-attempt-answer-1",
    "kind": "tool_call",
    "category": "tool_call",
    "role": "assistant",
    "key": "call-2",
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "assistant_record_id": "answer-1",
      "name": "mcp__workspace__run",
      "arguments": {
        "task_id": "task-2"
      },
      "task_id": "task-2"
    }
  },
  {
    "session_id": "openai-error-missing",
    "seq": 13,
    "record_id": "invoke-call-3",
    "operation_id": "answer-1",
    "operation_position": 3,
    "parent_operation_id": "op-attempt-answer-1",
    "kind": "tool_call",
    "category": "tool_call",
    "role": "assistant",
    "key": "call-3",
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "assistant_record_id": "answer-1",
      "name": "mcp__workspace__run",
      "arguments": {
        "task_id": "task-3"
      },
      "task_id": "task-3"
    }
  },
  {
    "session_id": "openai-error-missing",
    "seq": 17,
    "record_id": "user-0",
    "operation_id": "op-user-0",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "user_message",
    "category": "message",
    "role": "user",
    "key": null,
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "content": [
        {
          "kind": "text",
          "text": "Next user input 0"
        }
      ]
    },
    "scope": "session"
  },
  {
    "session_id": "openai-error-missing",
    "seq": 18,
    "record_id": "user-1",
    "operation_id": "op-user-1",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "user_message",
    "category": "message",
    "role": "user",
    "key": null,
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "content": [
        {
          "kind": "text",
          "text": "Next user input 1"
        }
      ]
    },
    "scope": "session"
  },
  {
    "session_id": "openai-error-missing",
    "seq": 19,
    "record_id": "user-2",
    "operation_id": "op-user-2",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "user_message",
    "category": "message",
    "role": "user",
    "key": null,
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "content": [
        {
          "kind": "text",
          "text": "Next user input 2"
        }
      ]
    },
    "scope": "session"
  },
  {
    "session_id": "openai-error-missing",
    "seq": 20,
    "record_id": "memory",
    "operation_id": "op-memory",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "memory_added",
    "category": "context",
    "role": "user",
    "key": "memory:format",
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "content": [
        {
          "kind": "text",
          "text": "Preserve source formatting."
        }
      ],
      "source_uri": "https://example.org/workspaces/boba/memory/project"
    },
    "scope": "session"
  },
  {
    "session_id": "openai-error-missing",
    "seq": 21,
    "record_id": "notice",
    "operation_id": "op-notice",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "notice",
    "category": "operational",
    "role": null,
    "key": null,
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "level": "info",
      "content": [
        {
          "kind": "text",
          "text": "Diagnostic outside the prompt."
        }
      ]
    }
  },
  {
    "session_id": "openai-error-missing",
    "seq": 25,
    "record_id": "attempt-poll-answer",
    "operation_id": "op-attempt-poll-answer",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "llm_request_attempt",
    "category": "operational",
    "role": null,
    "key": null,
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "level": "warning",
      "content": [
        {
          "kind": "text",
          "text": "Модель временно недоступна. Повтор через 2 секунды."
        }
      ],
      "attempt": 1,
      "status": "completed",
      "started_at": "2026-11-16T09:00:00Z",
      "method": "POST",
      "url": "https://example.test/inference",
      "status_code": 200,
      "duration_ms": 10,
      "error": null,
      "retry_after_ms": 2000,
      "request_body": null,
      "response_body": null
    }
  },
  {
    "session_id": "openai-error-missing",
    "seq": 26,
    "record_id": "poll-answer",
    "operation_id": "poll-answer",
    "operation_position": 0,
    "parent_operation_id": "op-attempt-poll-answer",
    "kind": "assistant_message",
    "category": "message",
    "role": "assistant",
    "key": null,
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "content": [
        {
          "kind": "tool_call_reference",
          "call_id": "poll"
        }
      ],
      "completion": "complete"
    },
    "scope": "session"
  },
  {
    "session_id": "openai-error-missing",
    "seq": 27,
    "record_id": "invoke-poll",
    "operation_id": "poll-answer",
    "operation_position": 1,
    "parent_operation_id": "op-attempt-poll-answer",
    "kind": "tool_call",
    "category": "tool_call",
    "role": "assistant",
    "key": "poll",
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "assistant_record_id": "poll-answer",
      "name": "task_status",
      "arguments": {
        "task_id": null
      },
      "task_id": null
    }
  },
  {
    "session_id": "openai-error-chain",
    "seq": 1,
    "record_id": "model",
    "operation_id": "op-model",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "model",
    "category": "setting",
    "role": null,
    "key": "model",
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "value": {
        "model": "test-model"
      },
      "provider": "demo",
      "profile_id": "general"
    },
    "scope": "session"
  },
  {
    "session_id": "openai-error-chain",
    "seq": 2,
    "record_id": "sampling",
    "operation_id": "op-sampling",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "sampling",
    "category": "setting",
    "role": null,
    "key": "sampling",
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "value": {
        "temperature": 0.2
      }
    },
    "scope": "session"
  },
  {
    "session_id": "openai-error-chain",
    "seq": 3,
    "record_id": "system",
    "operation_id": "op-system",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "system_prompt_added",
    "category": "prompt",
    "role": "system",
    "key": "core",
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "content": [
        {
          "kind": "text",
          "text": "You are a helpful assistant.\r\n"
        }
      ],
      "source_uri": null
    },
    "scope": "session"
  },
  {
    "session_id": "openai-error-chain",
    "seq": 4,
    "record_id": "add-mcp__workspace__run",
    "operation_id": "op-add-mcp__workspace__run",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "tool_added",
    "category": "tool_catalog",
    "role": null,
    "key": "mcp__workspace__run",
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "tool": {
        "description": "Demo tool"
      }
    }
  },
  {
    "session_id": "openai-error-chain",
    "seq": 5,
    "record_id": "schema-mcp__workspace__run",
    "operation_id": "op-schema-mcp__workspace__run",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "tool_schema",
    "category": "tool_schema",
    "role": null,
    "key": "mcp__workspace__run",
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "catalog_revision": "add-mcp__workspace__run",
      "tool": {
        "description": "Demo tool",
        "parameters": {
          "type": "object",
          "properties": {}
        },
        "deferred": false
      }
    }
  },
  {
    "session_id": "openai-error-chain",
    "seq": 6,
    "record_id": "add-task_status",
    "operation_id": "op-add-task_status",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "tool_added",
    "category": "tool_catalog",
    "role": null,
    "key": "task_status",
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "tool": {
        "description": "Demo tool"
      }
    }
  },
  {
    "session_id": "openai-error-chain",
    "seq": 7,
    "record_id": "schema-task_status",
    "operation_id": "op-schema-task_status",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "tool_schema",
    "category": "tool_schema",
    "role": null,
    "key": "task_status",
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "catalog_revision": "add-task_status",
      "tool": {
        "description": "Demo tool",
        "parameters": {
          "type": "object",
          "properties": {}
        },
        "deferred": false
      }
    }
  },
  {
    "session_id": "openai-error-chain",
    "seq": 8,
    "record_id": "user-start",
    "operation_id": "op-user-start",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "user_message",
    "category": "message",
    "role": "user",
    "key": null,
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "content": [
        {
          "kind": "text",
          "text": "Start three tasks.\nПривет  世界"
        }
      ]
    },
    "scope": "session"
  },
  {
    "session_id": "openai-error-chain",
    "seq": 9,
    "record_id": "attempt-answer-1",
    "operation_id": "op-attempt-answer-1",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "llm_request_attempt",
    "category": "operational",
    "role": null,
    "key": null,
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "level": "warning",
      "content": [
        {
          "kind": "text",
          "text": "Модель временно недоступна. Повтор через 2 секунды."
        }
      ],
      "attempt": 1,
      "status": "completed",
      "started_at": "2026-11-16T09:00:00Z",
      "method": "POST",
      "url": "https://example.test/inference",
      "status_code": 200,
      "duration_ms": 10,
      "error": null,
      "retry_after_ms": 2000,
      "request_body": null,
      "response_body": null
    }
  },
  {
    "session_id": "openai-error-chain",
    "seq": 10,
    "record_id": "answer-1",
    "operation_id": "answer-1",
    "operation_position": 0,
    "parent_operation_id": "op-attempt-answer-1",
    "kind": "assistant_message",
    "category": "message",
    "role": "assistant",
    "key": null,
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "content": [
        {
          "kind": "text",
          "text": "Starting tasks."
        },
        {
          "kind": "tool_call_reference",
          "call_id": "call-1"
        },
        {
          "kind": "tool_call_reference",
          "call_id": "call-2"
        },
        {
          "kind": "tool_call_reference",
          "call_id": "call-3"
        }
      ],
      "completion": "complete"
    },
    "scope": "session"
  },
  {
    "session_id": "openai-error-chain",
    "seq": 11,
    "record_id": "invoke-call-1",
    "operation_id": "answer-1",
    "operation_position": 1,
    "parent_operation_id": "op-attempt-answer-1",
    "kind": "tool_call",
    "category": "tool_call",
    "role": "assistant",
    "key": "call-1",
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "assistant_record_id": "answer-1",
      "name": "mcp__workspace__run",
      "arguments": {
        "task_id": "task-1"
      },
      "task_id": "task-1"
    }
  },
  {
    "session_id": "openai-error-chain",
    "seq": 12,
    "record_id": "invoke-call-2",
    "operation_id": "answer-1",
    "operation_position": 2,
    "parent_operation_id": "op-attempt-answer-1",
    "kind": "tool_call",
    "category": "tool_call",
    "role": "assistant",
    "key": "call-2",
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "assistant_record_id": "answer-1",
      "name": "mcp__workspace__run",
      "arguments": {
        "task_id": "task-2"
      },
      "task_id": "task-2"
    }
  },
  {
    "session_id": "openai-error-chain",
    "seq": 13,
    "record_id": "invoke-call-3",
    "operation_id": "answer-1",
    "operation_position": 3,
    "parent_operation_id": "op-attempt-answer-1",
    "kind": "tool_call",
    "category": "tool_call",
    "role": "assistant",
    "key": "call-3",
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "assistant_record_id": "answer-1",
      "name": "mcp__workspace__run",
      "arguments": {
        "task_id": "task-3"
      },
      "task_id": "task-3"
    }
  },
  {
    "session_id": "openai-error-chain",
    "seq": 14,
    "record_id": "pending-call-1",
    "operation_id": "answer-1",
    "operation_position": 4,
    "parent_operation_id": "op-attempt-answer-1",
    "kind": "tool_result",
    "category": "tool_result",
    "role": "tool",
    "key": "call-1",
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "call_record_id": "invoke-call-1",
      "status": "pending",
      "state": {
        "task_id": "task-1",
        "revision": 1,
        "description": "task-1",
        "status": "pending",
        "execution_id": null,
        "child_session_id": null,
        "output_uri": null,
        "result": null,
        "error": null
      },
      "source_record_id": null,
      "content": [
        {
          "kind": "text",
          "text": "call-1: pending"
        }
      ]
    }
  },
  {
    "session_id": "openai-error-chain",
    "seq": 15,
    "record_id": "pending-call-2",
    "operation_id": "answer-1",
    "operation_position": 5,
    "parent_operation_id": "op-attempt-answer-1",
    "kind": "tool_result",
    "category": "tool_result",
    "role": "tool",
    "key": "call-2",
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "call_record_id": "invoke-call-2",
      "status": "pending",
      "state": {
        "task_id": "task-2",
        "revision": 1,
        "description": "task-2",
        "status": "pending",
        "execution_id": null,
        "child_session_id": null,
        "output_uri": null,
        "result": null,
        "error": null
      },
      "source_record_id": null,
      "content": [
        {
          "kind": "text",
          "text": "call-2: pending"
        }
      ]
    }
  },
  {
    "session_id": "openai-error-chain",
    "seq": 16,
    "record_id": "pending-call-3",
    "operation_id": "answer-1",
    "operation_position": 6,
    "parent_operation_id": "op-attempt-answer-1",
    "kind": "tool_result",
    "category": "tool_result",
    "role": "tool",
    "key": "call-3",
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "call_record_id": "invoke-call-3",
      "status": "pending",
      "state": {
        "task_id": "task-3",
        "revision": 1,
        "description": "task-3",
        "status": "pending",
        "execution_id": null,
        "child_session_id": null,
        "output_uri": null,
        "result": null,
        "error": null
      },
      "source_record_id": null,
      "content": [
        {
          "kind": "text",
          "text": "call-3: pending"
        }
      ]
    }
  },
  {
    "session_id": "openai-error-chain",
    "seq": 17,
    "record_id": "user-0",
    "operation_id": "op-user-0",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "user_message",
    "category": "message",
    "role": "user",
    "key": null,
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "content": [
        {
          "kind": "text",
          "text": "Next user input 0"
        }
      ]
    },
    "scope": "session"
  },
  {
    "session_id": "openai-error-chain",
    "seq": 18,
    "record_id": "user-1",
    "operation_id": "op-user-1",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "user_message",
    "category": "message",
    "role": "user",
    "key": null,
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "content": [
        {
          "kind": "text",
          "text": "Next user input 1"
        }
      ]
    },
    "scope": "session"
  },
  {
    "session_id": "openai-error-chain",
    "seq": 19,
    "record_id": "user-2",
    "operation_id": "op-user-2",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "user_message",
    "category": "message",
    "role": "user",
    "key": null,
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "content": [
        {
          "kind": "text",
          "text": "Next user input 2"
        }
      ]
    },
    "scope": "session"
  },
  {
    "session_id": "openai-error-chain",
    "seq": 20,
    "record_id": "memory",
    "operation_id": "op-memory",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "memory_added",
    "category": "context",
    "role": "user",
    "key": "memory:format",
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "content": [
        {
          "kind": "text",
          "text": "Preserve source formatting."
        }
      ],
      "source_uri": "https://example.org/workspaces/boba/memory/project"
    },
    "scope": "session"
  },
  {
    "session_id": "openai-error-chain",
    "seq": 21,
    "record_id": "notice",
    "operation_id": "op-notice",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "notice",
    "category": "operational",
    "role": null,
    "key": null,
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "level": "info",
      "content": [
        {
          "kind": "text",
          "text": "Diagnostic outside the prompt."
        }
      ]
    }
  },
  {
    "session_id": "openai-error-chain",
    "seq": 22,
    "record_id": "running-1",
    "operation_id": "op-running-1",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "tool_result",
    "category": "tool_result",
    "role": "tool",
    "key": "call-1",
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "call_record_id": "invoke-call-1",
      "status": "running",
      "state": {
        "task_id": "task-1",
        "revision": 2,
        "description": "task-1",
        "status": "running",
        "execution_id": null,
        "child_session_id": null,
        "output_uri": null,
        "result": null,
        "error": null
      },
      "source_record_id": null,
      "content": [
        {
          "kind": "text",
          "text": "call-1: running"
        }
      ]
    }
  },
  {
    "session_id": "openai-error-chain",
    "seq": 23,
    "record_id": "failed-2",
    "operation_id": "op-failed-2",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "tool_result",
    "category": "tool_result",
    "role": "tool",
    "key": "call-2",
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "call_record_id": "invoke-call-2",
      "status": "failed",
      "state": {
        "task_id": "task-2",
        "revision": 2,
        "description": "task-2",
        "status": "failed",
        "execution_id": null,
        "child_session_id": null,
        "output_uri": null,
        "result": null,
        "error": "Demo execution failed"
      },
      "source_record_id": null,
      "content": [
        {
          "kind": "text",
          "text": "call-2: failed"
        }
      ]
    }
  },
  {
    "session_id": "openai-error-chain",
    "seq": 24,
    "record_id": "completed-3",
    "operation_id": "op-completed-3",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "tool_result",
    "category": "tool_result",
    "role": "tool",
    "key": "call-3",
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "call_record_id": "invoke-call-3",
      "status": "completed",
      "state": {
        "task_id": "task-3",
        "revision": 2,
        "description": "task-3",
        "status": "completed",
        "execution_id": null,
        "child_session_id": null,
        "output_uri": null,
        "result": null,
        "error": null
      },
      "source_record_id": null,
      "content": [
        {
          "kind": "text",
          "text": "call-3: completed"
        }
      ]
    }
  },
  {
    "session_id": "openai-error-chain",
    "seq": 25,
    "record_id": "attempt-poll-answer",
    "operation_id": "op-attempt-poll-answer",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "llm_request_attempt",
    "category": "operational",
    "role": null,
    "key": null,
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "level": "warning",
      "content": [
        {
          "kind": "text",
          "text": "Модель временно недоступна. Повтор через 2 секунды."
        }
      ],
      "attempt": 1,
      "status": "completed",
      "started_at": "2026-11-16T09:00:00Z",
      "method": "POST",
      "url": "https://example.test/inference",
      "status_code": 200,
      "duration_ms": 10,
      "error": null,
      "retry_after_ms": 2000,
      "request_body": null,
      "response_body": null
    }
  },
  {
    "session_id": "openai-error-chain",
    "seq": 26,
    "record_id": "poll-answer",
    "operation_id": "poll-answer",
    "operation_position": 0,
    "parent_operation_id": "op-attempt-poll-answer",
    "kind": "assistant_message",
    "category": "message",
    "role": "assistant",
    "key": null,
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "content": [
        {
          "kind": "tool_call_reference",
          "call_id": "poll"
        }
      ],
      "completion": "complete"
    },
    "scope": "session"
  },
  {
    "session_id": "openai-error-chain",
    "seq": 27,
    "record_id": "invoke-poll",
    "operation_id": "poll-answer",
    "operation_position": 1,
    "parent_operation_id": "op-attempt-poll-answer",
    "kind": "tool_call",
    "category": "tool_call",
    "role": "assistant",
    "key": "poll",
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "assistant_record_id": "poll-answer",
      "name": "task_status",
      "arguments": {
        "task_id": null
      },
      "task_id": null
    }
  },
  {
    "session_id": "openai-error-chain",
    "seq": 28,
    "record_id": "polled-1",
    "operation_id": "observe-poll",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "tool_result",
    "category": "tool_result",
    "role": "tool",
    "key": "call-1",
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "call_record_id": "invoke-call-1",
      "status": "running",
      "state": null,
      "source_record_id": "running-1",
      "content": []
    }
  },
  {
    "session_id": "openai-error-chain",
    "seq": 29,
    "record_id": "poll-result",
    "operation_id": "observe-poll",
    "operation_position": 1,
    "parent_operation_id": null,
    "kind": "tool_result",
    "category": "tool_result",
    "role": "tool",
    "key": "poll",
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "call_record_id": "invoke-poll",
      "status": "completed",
      "state": null,
      "source_record_id": "polled-1",
      "content": []
    }
  },
  {
    "session_id": "openai-error-chain",
    "seq": 30,
    "record_id": "completed-1",
    "operation_id": "op-completed-1",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "tool_result",
    "category": "tool_result",
    "role": "tool",
    "key": "call-1",
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "call_record_id": "invoke-call-1",
      "status": "completed",
      "state": {
        "task_id": "task-1",
        "revision": 3,
        "description": "task-1",
        "status": "completed",
        "execution_id": null,
        "child_session_id": null,
        "output_uri": null,
        "result": null,
        "error": null
      },
      "source_record_id": null,
      "content": [
        {
          "kind": "text",
          "text": "call-1: completed"
        }
      ]
    }
  },
  {
    "session_id": "openai-scope",
    "seq": 1,
    "record_id": "model",
    "operation_id": "op-model",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "model",
    "category": "setting",
    "role": null,
    "key": "model",
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "value": {
        "model": "test-model"
      },
      "provider": "demo",
      "profile_id": "general"
    },
    "scope": "session"
  },
  {
    "session_id": "openai-scope",
    "seq": 2,
    "record_id": "sampling",
    "operation_id": "op-sampling",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "sampling",
    "category": "setting",
    "role": null,
    "key": "sampling",
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "value": {
        "temperature": 0.2
      }
    },
    "scope": "session"
  },
  {
    "session_id": "openai-scope",
    "seq": 3,
    "record_id": "system",
    "operation_id": "op-system",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "system_prompt_added",
    "category": "prompt",
    "role": "system",
    "key": "core",
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "content": [
        {
          "kind": "text",
          "text": "You are a helpful assistant.\r\n"
        }
      ],
      "source_uri": null
    },
    "scope": "session"
  },
  {
    "session_id": "openai-scope",
    "seq": 4,
    "record_id": "user-a",
    "operation_id": "user-a",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "user_message",
    "category": "message",
    "role": "user",
    "key": null,
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "content": [
        {
          "kind": "text",
          "text": "Question A"
        }
      ]
    },
    "scope": "session"
  },
  {
    "session_id": "openai-scope",
    "seq": 5,
    "record_id": "local-sampling",
    "operation_id": "user-a",
    "operation_position": 1,
    "parent_operation_id": null,
    "kind": "sampling",
    "category": "setting",
    "role": null,
    "key": "sampling",
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "value": {
        "temperature": 0.1
      }
    },
    "scope": "request"
  },
  {
    "session_id": "openai-scope",
    "seq": 6,
    "record_id": "user-b",
    "operation_id": "op-user-b",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "user_message",
    "category": "message",
    "role": "user",
    "key": null,
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "content": [
        {
          "kind": "text",
          "text": "Question B"
        }
      ]
    },
    "scope": "session"
  },
  {
    "session_id": "openai-scope",
    "seq": 7,
    "record_id": "new-default",
    "operation_id": "op-new-default",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "sampling",
    "category": "setting",
    "role": null,
    "key": "sampling",
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "value": {
        "temperature": 0.8
      }
    },
    "scope": "session"
  },
  {
    "session_id": "openai-scope",
    "seq": 8,
    "record_id": "remove-local",
    "operation_id": "remove-local",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "sampling_removed",
    "category": "setting",
    "role": null,
    "key": "sampling",
    "operation": "remove",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {},
    "scope": "request"
  },
  {
    "session_id": "openai-scope",
    "seq": 9,
    "record_id": "opaque-options",
    "operation_id": "op-opaque-options",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "provider_parameters",
    "category": "setting",
    "role": null,
    "key": "provider_parameters",
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "value": {
        "vendor_option": {
          "enabled": false,
          "weight": 0
        }
      }
    },
    "scope": "session"
  },
  {
    "session_id": "openai-scope",
    "seq": 10,
    "record_id": "custom-context",
    "operation_id": "op-custom-context",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "custom_context_kind",
    "category": "context",
    "role": "user",
    "key": "custom",
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "content": [
        {
          "kind": "text",
          "text": "Custom context category"
        }
      ],
      "source_uri": "https://example.org/workspaces/boba/memory/project"
    },
    "scope": "session"
  },
  {
    "session_id": "openai-scope",
    "seq": 11,
    "record_id": "isolated",
    "operation_id": "classify",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "assembly_context",
    "category": "assembly",
    "role": null,
    "key": "context",
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "mode": "isolated",
      "from_seq": null,
      "through_seq": null
    },
    "scope": "request"
  },
  {
    "session_id": "openai-scope",
    "seq": 12,
    "record_id": "classify-input",
    "operation_id": "classify",
    "operation_position": 1,
    "parent_operation_id": null,
    "kind": "request_message",
    "category": "message",
    "role": "user",
    "key": null,
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "content": [
        {
          "kind": "text",
          "text": "Classify the operation."
        }
      ]
    },
    "scope": "request"
  },
  {
    "session_id": "openai-scope",
    "seq": 13,
    "record_id": "range",
    "operation_id": "summary",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "assembly_context",
    "category": "assembly",
    "role": null,
    "key": "context",
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "mode": "conversation",
      "from_seq": 4,
      "through_seq": 4
    },
    "scope": "request"
  },
  {
    "session_id": "openai-scope",
    "seq": 14,
    "record_id": "summary-input",
    "operation_id": "summary",
    "operation_position": 1,
    "parent_operation_id": null,
    "kind": "request_message",
    "category": "message",
    "role": "user",
    "key": null,
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "content": [
        {
          "kind": "text",
          "text": "Summarize."
        }
      ]
    },
    "scope": "request"
  },
  {
    "session_id": "openai-tools",
    "seq": 1,
    "record_id": "model",
    "operation_id": "op-model",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "model",
    "category": "setting",
    "role": null,
    "key": "model",
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "value": {
        "model": "test-model"
      },
      "provider": "demo",
      "profile_id": "general"
    },
    "scope": "session"
  },
  {
    "session_id": "openai-tools",
    "seq": 2,
    "record_id": "sampling",
    "operation_id": "op-sampling",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "sampling",
    "category": "setting",
    "role": null,
    "key": "sampling",
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "value": {
        "temperature": 0.2
      }
    },
    "scope": "session"
  },
  {
    "session_id": "openai-tools",
    "seq": 3,
    "record_id": "system",
    "operation_id": "op-system",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "system_prompt_added",
    "category": "prompt",
    "role": "system",
    "key": "core",
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "content": [
        {
          "kind": "text",
          "text": "You are a helpful assistant.\r\n"
        }
      ],
      "source_uri": null
    },
    "scope": "session"
  },
  {
    "session_id": "openai-tools",
    "seq": 4,
    "record_id": "user",
    "operation_id": "op-user",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "user_message",
    "category": "message",
    "role": "user",
    "key": null,
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "content": [
        {
          "kind": "text",
          "text": "Use tools."
        }
      ]
    },
    "scope": "session"
  },
  {
    "session_id": "openai-tools",
    "seq": 5,
    "record_id": "add-mcp__demo__read",
    "operation_id": "op-add-mcp__demo__read",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "tool_added",
    "category": "tool_catalog",
    "role": null,
    "key": "mcp__demo__read",
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "tool": {
        "description": "Demo tool"
      }
    }
  },
  {
    "session_id": "openai-tools",
    "seq": 6,
    "record_id": "schema-mcp__demo__read",
    "operation_id": "op-schema-mcp__demo__read",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "tool_schema",
    "category": "tool_schema",
    "role": null,
    "key": "mcp__demo__read",
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "catalog_revision": "add-mcp__demo__read",
      "tool": {
        "description": "Demo tool",
        "parameters": {
          "type": "object",
          "properties": {}
        },
        "deferred": false
      }
    }
  },
  {
    "session_id": "openai-tools",
    "seq": 7,
    "record_id": "refresh-add",
    "operation_id": "op-refresh-add",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "tool_added",
    "category": "tool_catalog",
    "role": null,
    "key": "mcp__demo__read",
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "tool": {
        "description": "Updated description"
      }
    }
  },
  {
    "session_id": "openai-tools",
    "seq": 8,
    "record_id": "remove",
    "operation_id": "op-remove",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "tool_removed",
    "category": "tool_catalog",
    "role": null,
    "key": "mcp__demo__read",
    "operation": "remove",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {}
  },
  {
    "session_id": "openai-tools",
    "seq": 9,
    "record_id": "re-add",
    "operation_id": "op-re-add",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "tool_added",
    "category": "tool_catalog",
    "role": null,
    "key": "mcp__demo__read",
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "tool": {
        "description": "Available again"
      }
    }
  },
  {
    "session_id": "openai-tools",
    "seq": 10,
    "record_id": "new-schema",
    "operation_id": "op-new-schema",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "tool_schema",
    "category": "tool_schema",
    "role": null,
    "key": "mcp__demo__read",
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "catalog_revision": "re-add",
      "tool": {
        "description": "Получить список таблиц.",
        "parameters": {
          "type": "object",
          "properties": {},
          "additionalProperties": false
        },
        "deferred": false
      }
    }
  },
  {
    "session_id": "openai-error-stale-schema",
    "seq": 1,
    "record_id": "model",
    "operation_id": "op-model",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "model",
    "category": "setting",
    "role": null,
    "key": "model",
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "value": {
        "model": "test-model"
      },
      "provider": "demo",
      "profile_id": "general"
    },
    "scope": "session"
  },
  {
    "session_id": "openai-error-stale-schema",
    "seq": 2,
    "record_id": "sampling",
    "operation_id": "op-sampling",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "sampling",
    "category": "setting",
    "role": null,
    "key": "sampling",
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "value": {
        "temperature": 0.2
      }
    },
    "scope": "session"
  },
  {
    "session_id": "openai-error-stale-schema",
    "seq": 3,
    "record_id": "system",
    "operation_id": "op-system",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "system_prompt_added",
    "category": "prompt",
    "role": "system",
    "key": "core",
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "content": [
        {
          "kind": "text",
          "text": "You are a helpful assistant.\r\n"
        }
      ],
      "source_uri": null
    },
    "scope": "session"
  },
  {
    "session_id": "openai-error-stale-schema",
    "seq": 4,
    "record_id": "user",
    "operation_id": "op-user",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "user_message",
    "category": "message",
    "role": "user",
    "key": null,
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "content": [
        {
          "kind": "text",
          "text": "Use tools."
        }
      ]
    },
    "scope": "session"
  },
  {
    "session_id": "openai-error-stale-schema",
    "seq": 5,
    "record_id": "add-mcp__demo__read",
    "operation_id": "op-add-mcp__demo__read",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "tool_added",
    "category": "tool_catalog",
    "role": null,
    "key": "mcp__demo__read",
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "tool": {
        "description": "Demo tool"
      }
    }
  },
  {
    "session_id": "openai-error-stale-schema",
    "seq": 6,
    "record_id": "schema-mcp__demo__read",
    "operation_id": "op-schema-mcp__demo__read",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "tool_schema",
    "category": "tool_schema",
    "role": null,
    "key": "mcp__demo__read",
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "catalog_revision": "add-mcp__demo__read",
      "tool": {
        "description": "Demo tool",
        "parameters": {
          "type": "object",
          "properties": {}
        },
        "deferred": false
      }
    }
  },
  {
    "session_id": "openai-error-stale-schema",
    "seq": 7,
    "record_id": "refresh-add",
    "operation_id": "op-refresh-add",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "tool_added",
    "category": "tool_catalog",
    "role": null,
    "key": "mcp__demo__read",
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "tool": {
        "description": "Updated description"
      }
    }
  },
  {
    "session_id": "openai-error-stale-schema",
    "seq": 8,
    "record_id": "remove",
    "operation_id": "op-remove",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "tool_removed",
    "category": "tool_catalog",
    "role": null,
    "key": "mcp__demo__read",
    "operation": "remove",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {}
  },
  {
    "session_id": "openai-error-stale-schema",
    "seq": 9,
    "record_id": "re-add",
    "operation_id": "op-re-add",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "tool_added",
    "category": "tool_catalog",
    "role": null,
    "key": "mcp__demo__read",
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "tool": {
        "description": "Available again"
      }
    }
  },
  {
    "session_id": "openai-error-stale-schema",
    "seq": 10,
    "record_id": "new-schema",
    "operation_id": "op-new-schema",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "tool_schema",
    "category": "tool_schema",
    "role": null,
    "key": "mcp__demo__read",
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "catalog_revision": "add-mcp__demo__read",
      "tool": {
        "description": "Получить список таблиц.",
        "parameters": {
          "type": "object",
          "properties": {},
          "additionalProperties": false
        },
        "deferred": false
      }
    }
  },
  {
    "session_id": "openai-compact",
    "seq": 1,
    "record_id": "model",
    "operation_id": "op-model",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "model",
    "category": "setting",
    "role": null,
    "key": "model",
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "value": {
        "model": "test-model"
      },
      "provider": "demo",
      "profile_id": "general"
    },
    "scope": "session"
  },
  {
    "session_id": "openai-compact",
    "seq": 2,
    "record_id": "sampling",
    "operation_id": "op-sampling",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "sampling",
    "category": "setting",
    "role": null,
    "key": "sampling",
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "value": {
        "temperature": 0.2
      }
    },
    "scope": "session"
  },
  {
    "session_id": "openai-compact",
    "seq": 3,
    "record_id": "system",
    "operation_id": "op-system",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "system_prompt_added",
    "category": "prompt",
    "role": "system",
    "key": "core",
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "content": [
        {
          "kind": "text",
          "text": "You are a helpful assistant.\r\n"
        }
      ],
      "source_uri": null
    },
    "scope": "session"
  },
  {
    "session_id": "openai-compact",
    "seq": 4,
    "record_id": "old-user",
    "operation_id": "op-old-user",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "user_message",
    "category": "message",
    "role": "user",
    "key": null,
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "content": [
        {
          "kind": "text",
          "text": "Old dialogue"
        }
      ]
    },
    "scope": "session"
  },
  {
    "session_id": "openai-compact",
    "seq": 5,
    "record_id": "old-local",
    "operation_id": "inspect",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "request_message",
    "category": "message",
    "role": "user",
    "key": null,
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "content": [
        {
          "kind": "text",
          "text": "Request-scoped input before compaction."
        }
      ]
    },
    "scope": "request"
  },
  {
    "session_id": "openai-compact",
    "seq": 6,
    "record_id": "old-local-options",
    "operation_id": "inspect",
    "operation_position": 1,
    "parent_operation_id": null,
    "kind": "sampling",
    "category": "setting",
    "role": null,
    "key": "sampling",
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "value": {
        "temperature": 0.05
      }
    },
    "scope": "request"
  },
  {
    "session_id": "openai-compact",
    "seq": 7,
    "record_id": "compact",
    "operation_id": "op-compact",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "compact_message",
    "category": "compact",
    "role": "user",
    "key": null,
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "reason": "manual",
      "content": [
        {
          "kind": "text",
          "text": "Conversation summary."
        }
      ]
    }
  },
  {
    "session_id": "openai-compact",
    "seq": 8,
    "record_id": "copy-model",
    "operation_id": "op-copy-model",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "model",
    "category": "setting",
    "role": null,
    "key": "model",
    "operation": "set",
    "copied_from_id": "model",
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "value": {
        "model": "test-model"
      },
      "provider": "demo",
      "profile_id": "general"
    },
    "scope": "session"
  },
  {
    "session_id": "openai-compact",
    "seq": 9,
    "record_id": "copy-sampling",
    "operation_id": "op-copy-sampling",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "sampling",
    "category": "setting",
    "role": null,
    "key": "sampling",
    "operation": "set",
    "copied_from_id": "sampling",
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "value": {
        "temperature": 0.2
      }
    },
    "scope": "request"
  },
  {
    "session_id": "openai-compact",
    "seq": 10,
    "record_id": "copy-system",
    "operation_id": "op-copy-system",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "system_prompt_added",
    "category": "prompt",
    "role": "system",
    "key": "core",
    "operation": "set",
    "copied_from_id": "system",
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "content": [
        {
          "kind": "text",
          "text": "You are a helpful assistant.\r\n"
        }
      ],
      "source_uri": null
    },
    "scope": "session"
  },
  {
    "session_id": "openai-compact",
    "seq": 11,
    "record_id": "new-user",
    "operation_id": "op-new-user",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "user_message",
    "category": "message",
    "role": "user",
    "key": null,
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "content": [
        {
          "kind": "text",
          "text": "New dialogue"
        }
      ]
    },
    "scope": "session"
  },
  {
    "session_id": "openai-task-compact",
    "seq": 1,
    "record_id": "model",
    "operation_id": "op-model",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "model",
    "category": "setting",
    "role": null,
    "key": "model",
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "value": {
        "model": "test-model"
      },
      "provider": "demo",
      "profile_id": "general"
    },
    "scope": "session"
  },
  {
    "session_id": "openai-task-compact",
    "seq": 2,
    "record_id": "sampling",
    "operation_id": "op-sampling",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "sampling",
    "category": "setting",
    "role": null,
    "key": "sampling",
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "value": {
        "temperature": 0.2
      }
    },
    "scope": "session"
  },
  {
    "session_id": "openai-task-compact",
    "seq": 3,
    "record_id": "system",
    "operation_id": "op-system",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "system_prompt_added",
    "category": "prompt",
    "role": "system",
    "key": "core",
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "content": [
        {
          "kind": "text",
          "text": "You are a helpful assistant.\r\n"
        }
      ],
      "source_uri": null
    },
    "scope": "session"
  },
  {
    "session_id": "openai-task-compact",
    "seq": 4,
    "record_id": "add-mcp__workspace__run",
    "operation_id": "op-add-mcp__workspace__run",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "tool_added",
    "category": "tool_catalog",
    "role": null,
    "key": "mcp__workspace__run",
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "tool": {
        "description": "Demo tool"
      }
    }
  },
  {
    "session_id": "openai-task-compact",
    "seq": 5,
    "record_id": "schema-mcp__workspace__run",
    "operation_id": "op-schema-mcp__workspace__run",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "tool_schema",
    "category": "tool_schema",
    "role": null,
    "key": "mcp__workspace__run",
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "catalog_revision": "add-mcp__workspace__run",
      "tool": {
        "description": "Demo tool",
        "parameters": {
          "type": "object",
          "properties": {}
        },
        "deferred": false
      }
    }
  },
  {
    "session_id": "openai-task-compact",
    "seq": 6,
    "record_id": "add-task_status",
    "operation_id": "op-add-task_status",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "tool_added",
    "category": "tool_catalog",
    "role": null,
    "key": "task_status",
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "tool": {
        "description": "Demo tool"
      }
    }
  },
  {
    "session_id": "openai-task-compact",
    "seq": 7,
    "record_id": "schema-task_status",
    "operation_id": "op-schema-task_status",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "tool_schema",
    "category": "tool_schema",
    "role": null,
    "key": "task_status",
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "catalog_revision": "add-task_status",
      "tool": {
        "description": "Demo tool",
        "parameters": {
          "type": "object",
          "properties": {}
        },
        "deferred": false
      }
    }
  },
  {
    "session_id": "openai-task-compact",
    "seq": 8,
    "record_id": "user-start",
    "operation_id": "op-user-start",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "user_message",
    "category": "message",
    "role": "user",
    "key": null,
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "content": [
        {
          "kind": "text",
          "text": "Start three tasks.\nПривет  世界"
        }
      ]
    },
    "scope": "session"
  },
  {
    "session_id": "openai-task-compact",
    "seq": 9,
    "record_id": "attempt-answer-1",
    "operation_id": "op-attempt-answer-1",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "llm_request_attempt",
    "category": "operational",
    "role": null,
    "key": null,
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "level": "warning",
      "content": [
        {
          "kind": "text",
          "text": "Модель временно недоступна. Повтор через 2 секунды."
        }
      ],
      "attempt": 1,
      "status": "completed",
      "started_at": "2026-11-16T09:00:00Z",
      "method": "POST",
      "url": "https://example.test/inference",
      "status_code": 200,
      "duration_ms": 10,
      "error": null,
      "retry_after_ms": 2000,
      "request_body": null,
      "response_body": null
    }
  },
  {
    "session_id": "openai-task-compact",
    "seq": 10,
    "record_id": "answer-1",
    "operation_id": "answer-1",
    "operation_position": 0,
    "parent_operation_id": "op-attempt-answer-1",
    "kind": "assistant_message",
    "category": "message",
    "role": "assistant",
    "key": null,
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "content": [
        {
          "kind": "text",
          "text": "Starting tasks."
        },
        {
          "kind": "tool_call_reference",
          "call_id": "call-1"
        },
        {
          "kind": "tool_call_reference",
          "call_id": "call-2"
        },
        {
          "kind": "tool_call_reference",
          "call_id": "call-3"
        }
      ],
      "completion": "complete"
    },
    "scope": "session"
  },
  {
    "session_id": "openai-task-compact",
    "seq": 11,
    "record_id": "invoke-call-1",
    "operation_id": "answer-1",
    "operation_position": 1,
    "parent_operation_id": "op-attempt-answer-1",
    "kind": "tool_call",
    "category": "tool_call",
    "role": "assistant",
    "key": "call-1",
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "assistant_record_id": "answer-1",
      "name": "mcp__workspace__run",
      "arguments": {
        "task_id": "task-1"
      },
      "task_id": "task-1"
    }
  },
  {
    "session_id": "openai-task-compact",
    "seq": 12,
    "record_id": "invoke-call-2",
    "operation_id": "answer-1",
    "operation_position": 2,
    "parent_operation_id": "op-attempt-answer-1",
    "kind": "tool_call",
    "category": "tool_call",
    "role": "assistant",
    "key": "call-2",
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "assistant_record_id": "answer-1",
      "name": "mcp__workspace__run",
      "arguments": {
        "task_id": "task-2"
      },
      "task_id": "task-2"
    }
  },
  {
    "session_id": "openai-task-compact",
    "seq": 13,
    "record_id": "invoke-call-3",
    "operation_id": "answer-1",
    "operation_position": 3,
    "parent_operation_id": "op-attempt-answer-1",
    "kind": "tool_call",
    "category": "tool_call",
    "role": "assistant",
    "key": "call-3",
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "assistant_record_id": "answer-1",
      "name": "mcp__workspace__run",
      "arguments": {
        "task_id": "task-3"
      },
      "task_id": "task-3"
    }
  },
  {
    "session_id": "openai-task-compact",
    "seq": 14,
    "record_id": "pending-call-1",
    "operation_id": "answer-1",
    "operation_position": 4,
    "parent_operation_id": "op-attempt-answer-1",
    "kind": "tool_result",
    "category": "tool_result",
    "role": "tool",
    "key": "call-1",
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "call_record_id": "invoke-call-1",
      "status": "pending",
      "state": {
        "task_id": "task-1",
        "revision": 1,
        "description": "task-1",
        "status": "pending",
        "execution_id": null,
        "child_session_id": null,
        "output_uri": null,
        "result": null,
        "error": null
      },
      "source_record_id": null,
      "content": [
        {
          "kind": "text",
          "text": "call-1: pending"
        }
      ]
    }
  },
  {
    "session_id": "openai-task-compact",
    "seq": 15,
    "record_id": "pending-call-2",
    "operation_id": "answer-1",
    "operation_position": 5,
    "parent_operation_id": "op-attempt-answer-1",
    "kind": "tool_result",
    "category": "tool_result",
    "role": "tool",
    "key": "call-2",
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "call_record_id": "invoke-call-2",
      "status": "pending",
      "state": {
        "task_id": "task-2",
        "revision": 1,
        "description": "task-2",
        "status": "pending",
        "execution_id": null,
        "child_session_id": null,
        "output_uri": null,
        "result": null,
        "error": null
      },
      "source_record_id": null,
      "content": [
        {
          "kind": "text",
          "text": "call-2: pending"
        }
      ]
    }
  },
  {
    "session_id": "openai-task-compact",
    "seq": 16,
    "record_id": "pending-call-3",
    "operation_id": "answer-1",
    "operation_position": 6,
    "parent_operation_id": "op-attempt-answer-1",
    "kind": "tool_result",
    "category": "tool_result",
    "role": "tool",
    "key": "call-3",
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "call_record_id": "invoke-call-3",
      "status": "pending",
      "state": {
        "task_id": "task-3",
        "revision": 1,
        "description": "task-3",
        "status": "pending",
        "execution_id": null,
        "child_session_id": null,
        "output_uri": null,
        "result": null,
        "error": null
      },
      "source_record_id": null,
      "content": [
        {
          "kind": "text",
          "text": "call-3: pending"
        }
      ]
    }
  },
  {
    "session_id": "openai-task-compact",
    "seq": 17,
    "record_id": "user-0",
    "operation_id": "op-user-0",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "user_message",
    "category": "message",
    "role": "user",
    "key": null,
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "content": [
        {
          "kind": "text",
          "text": "Next user input 0"
        }
      ]
    },
    "scope": "session"
  },
  {
    "session_id": "openai-task-compact",
    "seq": 18,
    "record_id": "user-1",
    "operation_id": "op-user-1",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "user_message",
    "category": "message",
    "role": "user",
    "key": null,
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "content": [
        {
          "kind": "text",
          "text": "Next user input 1"
        }
      ]
    },
    "scope": "session"
  },
  {
    "session_id": "openai-task-compact",
    "seq": 19,
    "record_id": "user-2",
    "operation_id": "op-user-2",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "user_message",
    "category": "message",
    "role": "user",
    "key": null,
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "content": [
        {
          "kind": "text",
          "text": "Next user input 2"
        }
      ]
    },
    "scope": "session"
  },
  {
    "session_id": "openai-task-compact",
    "seq": 20,
    "record_id": "memory",
    "operation_id": "op-memory",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "memory_added",
    "category": "context",
    "role": "user",
    "key": "memory:format",
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "content": [
        {
          "kind": "text",
          "text": "Preserve source formatting."
        }
      ],
      "source_uri": "https://example.org/workspaces/boba/memory/project"
    },
    "scope": "session"
  },
  {
    "session_id": "openai-task-compact",
    "seq": 21,
    "record_id": "notice",
    "operation_id": "op-notice",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "notice",
    "category": "operational",
    "role": null,
    "key": null,
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "level": "info",
      "content": [
        {
          "kind": "text",
          "text": "Diagnostic outside the prompt."
        }
      ]
    }
  },
  {
    "session_id": "openai-task-compact",
    "seq": 22,
    "record_id": "running-1",
    "operation_id": "op-running-1",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "tool_result",
    "category": "tool_result",
    "role": "tool",
    "key": "call-1",
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "call_record_id": "invoke-call-1",
      "status": "running",
      "state": {
        "task_id": "task-1",
        "revision": 2,
        "description": "task-1",
        "status": "running",
        "execution_id": null,
        "child_session_id": null,
        "output_uri": null,
        "result": null,
        "error": null
      },
      "source_record_id": null,
      "content": [
        {
          "kind": "text",
          "text": "call-1: running"
        }
      ]
    }
  },
  {
    "session_id": "openai-task-compact",
    "seq": 23,
    "record_id": "failed-2",
    "operation_id": "op-failed-2",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "tool_result",
    "category": "tool_result",
    "role": "tool",
    "key": "call-2",
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "call_record_id": "invoke-call-2",
      "status": "failed",
      "state": {
        "task_id": "task-2",
        "revision": 2,
        "description": "task-2",
        "status": "failed",
        "execution_id": null,
        "child_session_id": null,
        "output_uri": null,
        "result": null,
        "error": "Demo execution failed"
      },
      "source_record_id": null,
      "content": [
        {
          "kind": "text",
          "text": "call-2: failed"
        }
      ]
    }
  },
  {
    "session_id": "openai-task-compact",
    "seq": 24,
    "record_id": "completed-3",
    "operation_id": "op-completed-3",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "tool_result",
    "category": "tool_result",
    "role": "tool",
    "key": "call-3",
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "call_record_id": "invoke-call-3",
      "status": "completed",
      "state": {
        "task_id": "task-3",
        "revision": 2,
        "description": "task-3",
        "status": "completed",
        "execution_id": null,
        "child_session_id": null,
        "output_uri": null,
        "result": null,
        "error": null
      },
      "source_record_id": null,
      "content": [
        {
          "kind": "text",
          "text": "call-3: completed"
        }
      ]
    }
  },
  {
    "session_id": "openai-task-compact",
    "seq": 25,
    "record_id": "attempt-poll-answer",
    "operation_id": "op-attempt-poll-answer",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "llm_request_attempt",
    "category": "operational",
    "role": null,
    "key": null,
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "level": "warning",
      "content": [
        {
          "kind": "text",
          "text": "Модель временно недоступна. Повтор через 2 секунды."
        }
      ],
      "attempt": 1,
      "status": "completed",
      "started_at": "2026-11-16T09:00:00Z",
      "method": "POST",
      "url": "https://example.test/inference",
      "status_code": 200,
      "duration_ms": 10,
      "error": null,
      "retry_after_ms": 2000,
      "request_body": null,
      "response_body": null
    }
  },
  {
    "session_id": "openai-task-compact",
    "seq": 26,
    "record_id": "poll-answer",
    "operation_id": "poll-answer",
    "operation_position": 0,
    "parent_operation_id": "op-attempt-poll-answer",
    "kind": "assistant_message",
    "category": "message",
    "role": "assistant",
    "key": null,
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "content": [
        {
          "kind": "tool_call_reference",
          "call_id": "poll"
        }
      ],
      "completion": "complete"
    },
    "scope": "session"
  },
  {
    "session_id": "openai-task-compact",
    "seq": 27,
    "record_id": "invoke-poll",
    "operation_id": "poll-answer",
    "operation_position": 1,
    "parent_operation_id": "op-attempt-poll-answer",
    "kind": "tool_call",
    "category": "tool_call",
    "role": "assistant",
    "key": "poll",
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "assistant_record_id": "poll-answer",
      "name": "task_status",
      "arguments": {
        "task_id": null
      },
      "task_id": null
    }
  },
  {
    "session_id": "openai-task-compact",
    "seq": 28,
    "record_id": "polled-1",
    "operation_id": "observe-poll",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "tool_result",
    "category": "tool_result",
    "role": "tool",
    "key": "call-1",
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "call_record_id": "invoke-call-1",
      "status": "running",
      "state": {
        "task_id": "task-1",
        "revision": 2,
        "description": "task-1",
        "status": "running",
        "execution_id": null,
        "child_session_id": null,
        "output_uri": null,
        "result": null,
        "error": null
      },
      "source_record_id": null,
      "content": [
        {
          "kind": "text",
          "text": "call-1: running"
        }
      ]
    }
  },
  {
    "session_id": "openai-task-compact",
    "seq": 29,
    "record_id": "poll-result",
    "operation_id": "observe-poll",
    "operation_position": 1,
    "parent_operation_id": null,
    "kind": "tool_result",
    "category": "tool_result",
    "role": "tool",
    "key": "poll",
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "call_record_id": "invoke-poll",
      "status": "completed",
      "state": null,
      "source_record_id": "polled-1",
      "content": []
    }
  },
  {
    "session_id": "openai-task-compact",
    "seq": 30,
    "record_id": "compact",
    "operation_id": "op-compact",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "compact_message",
    "category": "compact",
    "role": "user",
    "key": null,
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "reason": "manual",
      "content": [
        {
          "kind": "text",
          "text": "Tasks are running."
        }
      ]
    }
  },
  {
    "session_id": "openai-task-compact",
    "seq": 31,
    "record_id": "copy-model",
    "operation_id": "copy-op-model",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "model",
    "category": "setting",
    "role": null,
    "key": "model",
    "operation": "set",
    "copied_from_id": "model",
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "value": {
        "model": "test-model"
      },
      "provider": "demo",
      "profile_id": "general"
    },
    "scope": "session"
  },
  {
    "session_id": "openai-task-compact",
    "seq": 32,
    "record_id": "copy-sampling",
    "operation_id": "copy-op-sampling",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "sampling",
    "category": "setting",
    "role": null,
    "key": "sampling",
    "operation": "set",
    "copied_from_id": "sampling",
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "value": {
        "temperature": 0.2
      }
    },
    "scope": "request"
  },
  {
    "session_id": "openai-task-compact",
    "seq": 33,
    "record_id": "copy-system",
    "operation_id": "copy-op-system",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "system_prompt_added",
    "category": "prompt",
    "role": "system",
    "key": "core",
    "operation": "set",
    "copied_from_id": "system",
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "content": [
        {
          "kind": "text",
          "text": "You are a helpful assistant.\r\n"
        }
      ],
      "source_uri": null
    },
    "scope": "session"
  },
  {
    "session_id": "openai-task-compact",
    "seq": 34,
    "record_id": "copy-add-mcp__workspace__run",
    "operation_id": "copy-op-add-mcp__workspace__run",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "tool_added",
    "category": "tool_catalog",
    "role": null,
    "key": "mcp__workspace__run",
    "operation": "set",
    "copied_from_id": "add-mcp__workspace__run",
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "tool": {
        "description": "Demo tool"
      }
    }
  },
  {
    "session_id": "openai-task-compact",
    "seq": 35,
    "record_id": "copy-schema-mcp__workspace__run",
    "operation_id": "copy-op-schema-mcp__workspace__run",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "tool_schema",
    "category": "tool_schema",
    "role": null,
    "key": "mcp__workspace__run",
    "operation": "set",
    "copied_from_id": "schema-mcp__workspace__run",
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "catalog_revision": "copy-add-mcp__workspace__run",
      "tool": {
        "description": "Demo tool",
        "parameters": {
          "type": "object",
          "properties": {}
        },
        "deferred": false
      }
    }
  },
  {
    "session_id": "openai-task-compact",
    "seq": 36,
    "record_id": "copy-add-task_status",
    "operation_id": "copy-op-add-task_status",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "tool_added",
    "category": "tool_catalog",
    "role": null,
    "key": "task_status",
    "operation": "set",
    "copied_from_id": "add-task_status",
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "tool": {
        "description": "Demo tool"
      }
    }
  },
  {
    "session_id": "openai-task-compact",
    "seq": 37,
    "record_id": "copy-schema-task_status",
    "operation_id": "copy-op-schema-task_status",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "tool_schema",
    "category": "tool_schema",
    "role": null,
    "key": "task_status",
    "operation": "set",
    "copied_from_id": "schema-task_status",
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "catalog_revision": "copy-add-task_status",
      "tool": {
        "description": "Demo tool",
        "parameters": {
          "type": "object",
          "properties": {}
        },
        "deferred": false
      }
    }
  },
  {
    "session_id": "openai-task-compact",
    "seq": 38,
    "record_id": "copy-user-start",
    "operation_id": "copy-op-user-start",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "user_message",
    "category": "message",
    "role": "user",
    "key": null,
    "operation": "set",
    "copied_from_id": "user-start",
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "content": [
        {
          "kind": "text",
          "text": "Start three tasks.\nПривет  世界"
        }
      ]
    },
    "scope": "session"
  },
  {
    "session_id": "openai-task-compact",
    "seq": 39,
    "record_id": "copy-answer-1",
    "operation_id": "copy-answer-1",
    "operation_position": 0,
    "parent_operation_id": "op-attempt-answer-1",
    "kind": "assistant_message",
    "category": "message",
    "role": "assistant",
    "key": null,
    "operation": "set",
    "copied_from_id": "answer-1",
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "content": [
        {
          "kind": "text",
          "text": "Starting tasks."
        },
        {
          "kind": "tool_call_reference",
          "call_id": "call-1"
        },
        {
          "kind": "tool_call_reference",
          "call_id": "call-2"
        },
        {
          "kind": "tool_call_reference",
          "call_id": "call-3"
        }
      ],
      "completion": "complete"
    },
    "scope": "session"
  },
  {
    "session_id": "openai-task-compact",
    "seq": 40,
    "record_id": "copy-invoke-call-1",
    "operation_id": "copy-answer-1",
    "operation_position": 1,
    "parent_operation_id": "op-attempt-answer-1",
    "kind": "tool_call",
    "category": "tool_call",
    "role": "assistant",
    "key": "call-1",
    "operation": "set",
    "copied_from_id": "invoke-call-1",
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "assistant_record_id": "copy-answer-1",
      "name": "mcp__workspace__run",
      "arguments": {
        "task_id": "task-1"
      },
      "task_id": "task-1"
    }
  },
  {
    "session_id": "openai-task-compact",
    "seq": 41,
    "record_id": "copy-invoke-call-2",
    "operation_id": "copy-answer-1",
    "operation_position": 2,
    "parent_operation_id": "op-attempt-answer-1",
    "kind": "tool_call",
    "category": "tool_call",
    "role": "assistant",
    "key": "call-2",
    "operation": "set",
    "copied_from_id": "invoke-call-2",
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "assistant_record_id": "copy-answer-1",
      "name": "mcp__workspace__run",
      "arguments": {
        "task_id": "task-2"
      },
      "task_id": "task-2"
    }
  },
  {
    "session_id": "openai-task-compact",
    "seq": 42,
    "record_id": "copy-invoke-call-3",
    "operation_id": "copy-answer-1",
    "operation_position": 3,
    "parent_operation_id": "op-attempt-answer-1",
    "kind": "tool_call",
    "category": "tool_call",
    "role": "assistant",
    "key": "call-3",
    "operation": "set",
    "copied_from_id": "invoke-call-3",
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "assistant_record_id": "copy-answer-1",
      "name": "mcp__workspace__run",
      "arguments": {
        "task_id": "task-3"
      },
      "task_id": "task-3"
    }
  },
  {
    "session_id": "openai-task-compact",
    "seq": 43,
    "record_id": "copy-pending-call-1",
    "operation_id": "copy-answer-1",
    "operation_position": 4,
    "parent_operation_id": "op-attempt-answer-1",
    "kind": "tool_result",
    "category": "tool_result",
    "role": "tool",
    "key": "call-1",
    "operation": "set",
    "copied_from_id": "pending-call-1",
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "call_record_id": "copy-invoke-call-1",
      "status": "pending",
      "state": {
        "task_id": "task-1",
        "revision": 1,
        "description": "task-1",
        "status": "pending",
        "execution_id": null,
        "child_session_id": null,
        "output_uri": null,
        "result": null,
        "error": null
      },
      "source_record_id": null,
      "content": [
        {
          "kind": "text",
          "text": "call-1: pending"
        }
      ]
    }
  },
  {
    "session_id": "openai-task-compact",
    "seq": 44,
    "record_id": "copy-pending-call-2",
    "operation_id": "copy-answer-1",
    "operation_position": 5,
    "parent_operation_id": "op-attempt-answer-1",
    "kind": "tool_result",
    "category": "tool_result",
    "role": "tool",
    "key": "call-2",
    "operation": "set",
    "copied_from_id": "pending-call-2",
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "call_record_id": "copy-invoke-call-2",
      "status": "pending",
      "state": {
        "task_id": "task-2",
        "revision": 1,
        "description": "task-2",
        "status": "pending",
        "execution_id": null,
        "child_session_id": null,
        "output_uri": null,
        "result": null,
        "error": null
      },
      "source_record_id": null,
      "content": [
        {
          "kind": "text",
          "text": "call-2: pending"
        }
      ]
    }
  },
  {
    "session_id": "openai-task-compact",
    "seq": 45,
    "record_id": "copy-pending-call-3",
    "operation_id": "copy-answer-1",
    "operation_position": 6,
    "parent_operation_id": "op-attempt-answer-1",
    "kind": "tool_result",
    "category": "tool_result",
    "role": "tool",
    "key": "call-3",
    "operation": "set",
    "copied_from_id": "pending-call-3",
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "call_record_id": "copy-invoke-call-3",
      "status": "pending",
      "state": {
        "task_id": "task-3",
        "revision": 1,
        "description": "task-3",
        "status": "pending",
        "execution_id": null,
        "child_session_id": null,
        "output_uri": null,
        "result": null,
        "error": null
      },
      "source_record_id": null,
      "content": [
        {
          "kind": "text",
          "text": "call-3: pending"
        }
      ]
    }
  },
  {
    "session_id": "openai-task-compact",
    "seq": 46,
    "record_id": "copy-user-0",
    "operation_id": "copy-op-user-0",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "user_message",
    "category": "message",
    "role": "user",
    "key": null,
    "operation": "set",
    "copied_from_id": "user-0",
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "content": [
        {
          "kind": "text",
          "text": "Next user input 0"
        }
      ]
    },
    "scope": "session"
  },
  {
    "session_id": "openai-task-compact",
    "seq": 47,
    "record_id": "copy-user-1",
    "operation_id": "copy-op-user-1",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "user_message",
    "category": "message",
    "role": "user",
    "key": null,
    "operation": "set",
    "copied_from_id": "user-1",
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "content": [
        {
          "kind": "text",
          "text": "Next user input 1"
        }
      ]
    },
    "scope": "session"
  },
  {
    "session_id": "openai-task-compact",
    "seq": 48,
    "record_id": "copy-user-2",
    "operation_id": "copy-op-user-2",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "user_message",
    "category": "message",
    "role": "user",
    "key": null,
    "operation": "set",
    "copied_from_id": "user-2",
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "content": [
        {
          "kind": "text",
          "text": "Next user input 2"
        }
      ]
    },
    "scope": "session"
  },
  {
    "session_id": "openai-task-compact",
    "seq": 49,
    "record_id": "copy-memory",
    "operation_id": "copy-op-memory",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "memory_added",
    "category": "context",
    "role": "user",
    "key": "memory:format",
    "operation": "set",
    "copied_from_id": "memory",
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "content": [
        {
          "kind": "text",
          "text": "Preserve source formatting."
        }
      ],
      "source_uri": "https://example.org/workspaces/boba/memory/project"
    },
    "scope": "session"
  },
  {
    "session_id": "openai-task-compact",
    "seq": 50,
    "record_id": "copy-running-1",
    "operation_id": "copy-op-running-1",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "tool_result",
    "category": "tool_result",
    "role": "tool",
    "key": "call-1",
    "operation": "set",
    "copied_from_id": "running-1",
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "call_record_id": "copy-invoke-call-1",
      "status": "running",
      "state": {
        "task_id": "task-1",
        "revision": 2,
        "description": "task-1",
        "status": "running",
        "execution_id": null,
        "child_session_id": null,
        "output_uri": null,
        "result": null,
        "error": null
      },
      "source_record_id": null,
      "content": [
        {
          "kind": "text",
          "text": "call-1: running"
        }
      ]
    }
  },
  {
    "session_id": "openai-task-compact",
    "seq": 51,
    "record_id": "copy-failed-2",
    "operation_id": "copy-op-failed-2",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "tool_result",
    "category": "tool_result",
    "role": "tool",
    "key": "call-2",
    "operation": "set",
    "copied_from_id": "failed-2",
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "call_record_id": "copy-invoke-call-2",
      "status": "failed",
      "state": {
        "task_id": "task-2",
        "revision": 2,
        "description": "task-2",
        "status": "failed",
        "execution_id": null,
        "child_session_id": null,
        "output_uri": null,
        "result": null,
        "error": "Demo execution failed"
      },
      "source_record_id": null,
      "content": [
        {
          "kind": "text",
          "text": "call-2: failed"
        }
      ]
    }
  },
  {
    "session_id": "openai-task-compact",
    "seq": 52,
    "record_id": "copy-completed-3",
    "operation_id": "copy-op-completed-3",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "tool_result",
    "category": "tool_result",
    "role": "tool",
    "key": "call-3",
    "operation": "set",
    "copied_from_id": "completed-3",
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "call_record_id": "copy-invoke-call-3",
      "status": "completed",
      "state": {
        "task_id": "task-3",
        "revision": 2,
        "description": "task-3",
        "status": "completed",
        "execution_id": null,
        "child_session_id": null,
        "output_uri": null,
        "result": null,
        "error": null
      },
      "source_record_id": null,
      "content": [
        {
          "kind": "text",
          "text": "call-3: completed"
        }
      ]
    }
  },
  {
    "session_id": "openai-task-compact",
    "seq": 53,
    "record_id": "copy-poll-answer",
    "operation_id": "copy-poll-answer",
    "operation_position": 0,
    "parent_operation_id": "op-attempt-poll-answer",
    "kind": "assistant_message",
    "category": "message",
    "role": "assistant",
    "key": null,
    "operation": "set",
    "copied_from_id": "poll-answer",
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "content": [
        {
          "kind": "tool_call_reference",
          "call_id": "poll"
        }
      ],
      "completion": "complete"
    },
    "scope": "session"
  },
  {
    "session_id": "openai-task-compact",
    "seq": 54,
    "record_id": "copy-invoke-poll",
    "operation_id": "copy-poll-answer",
    "operation_position": 1,
    "parent_operation_id": "op-attempt-poll-answer",
    "kind": "tool_call",
    "category": "tool_call",
    "role": "assistant",
    "key": "poll",
    "operation": "set",
    "copied_from_id": "invoke-poll",
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "assistant_record_id": "copy-poll-answer",
      "name": "task_status",
      "arguments": {
        "task_id": null
      },
      "task_id": null
    }
  },
  {
    "session_id": "openai-task-compact",
    "seq": 55,
    "record_id": "copy-polled-1",
    "operation_id": "copy-observe-poll",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "tool_result",
    "category": "tool_result",
    "role": "tool",
    "key": "call-1",
    "operation": "set",
    "copied_from_id": "polled-1",
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "call_record_id": "copy-invoke-call-1",
      "status": "running",
      "state": {
        "task_id": "task-1",
        "revision": 2,
        "description": "task-1",
        "status": "running",
        "execution_id": null,
        "child_session_id": null,
        "output_uri": null,
        "result": null,
        "error": null
      },
      "source_record_id": null,
      "content": [
        {
          "kind": "text",
          "text": "call-1: running"
        }
      ]
    }
  },
  {
    "session_id": "openai-task-compact",
    "seq": 56,
    "record_id": "copy-poll-result",
    "operation_id": "copy-observe-poll",
    "operation_position": 1,
    "parent_operation_id": null,
    "kind": "tool_result",
    "category": "tool_result",
    "role": "tool",
    "key": "poll",
    "operation": "set",
    "copied_from_id": "poll-result",
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "call_record_id": "copy-invoke-poll",
      "status": "completed",
      "state": null,
      "source_record_id": "copy-polled-1",
      "content": []
    }
  },
  {
    "session_id": "openai-media",
    "seq": 1,
    "record_id": "model",
    "operation_id": "op-model",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "model",
    "category": "setting",
    "role": null,
    "key": "model",
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "value": {
        "model": "test-model"
      },
      "provider": "demo",
      "profile_id": "general"
    },
    "scope": "session"
  },
  {
    "session_id": "openai-media",
    "seq": 2,
    "record_id": "sampling",
    "operation_id": "op-sampling",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "sampling",
    "category": "setting",
    "role": null,
    "key": "sampling",
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "value": {
        "temperature": 0.2
      }
    },
    "scope": "session"
  },
  {
    "session_id": "openai-media",
    "seq": 3,
    "record_id": "system",
    "operation_id": "op-system",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "system_prompt_added",
    "category": "prompt",
    "role": "system",
    "key": "core",
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "content": [
        {
          "kind": "text",
          "text": "You are a helpful assistant.\r\n"
        }
      ],
      "source_uri": null
    },
    "scope": "session"
  },
  {
    "session_id": "openai-media",
    "seq": 4,
    "record_id": "mixed-user",
    "operation_id": "op-mixed-user",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "user_message",
    "category": "message",
    "role": "user",
    "key": null,
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "content": [
        {
          "kind": "text",
          "text": "Read this:\n"
        },
        {
          "kind": "resource",
          "name": "source.py",
          "uri": "file:///workspace/source.py",
          "media_type": "text/plain",
          "content": [
            {
              "kind": "text",
              "text": "line 1\r\nline 2\n"
            }
          ],
          "line_start": null,
          "line_end": null,
          "truncated": false
        },
        {
          "kind": "image",
          "media_type": "image/png",
          "data": "aGVsbG8="
        }
      ]
    },
    "scope": "session"
  },
  {
    "session_id": "openai-error-unloaded",
    "seq": 1,
    "record_id": "model",
    "operation_id": "op-model",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "model",
    "category": "setting",
    "role": null,
    "key": "model",
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "value": {
        "model": "test-model"
      },
      "provider": "demo",
      "profile_id": "general"
    },
    "scope": "session"
  },
  {
    "session_id": "openai-error-unloaded",
    "seq": 2,
    "record_id": "sampling",
    "operation_id": "op-sampling",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "sampling",
    "category": "setting",
    "role": null,
    "key": "sampling",
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "value": {
        "temperature": 0.2
      }
    },
    "scope": "session"
  },
  {
    "session_id": "openai-error-unloaded",
    "seq": 3,
    "record_id": "system",
    "operation_id": "op-system",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "system_prompt_added",
    "category": "prompt",
    "role": "system",
    "key": "core",
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "content": [
        {
          "kind": "text",
          "text": "You are a helpful assistant.\r\n"
        }
      ],
      "source_uri": null
    },
    "scope": "session"
  },
  {
    "session_id": "openai-error-unloaded",
    "seq": 4,
    "record_id": "mixed-user",
    "operation_id": "op-mixed-user",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "user_message",
    "category": "message",
    "role": "user",
    "key": null,
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "content": [
        {
          "kind": "text",
          "text": "Read this:\n"
        },
        {
          "kind": "resource",
          "name": "source.py",
          "uri": "file:///workspace/source.py",
          "media_type": "text/plain",
          "content": null,
          "line_start": null,
          "line_end": null,
          "truncated": false
        },
        {
          "kind": "image",
          "media_type": "image/png",
          "data": "aGVsbG8="
        }
      ]
    },
    "scope": "session"
  },
  {
    "session_id": "openai-error-category",
    "seq": 1,
    "record_id": "model",
    "operation_id": "op-model",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "model",
    "category": "setting",
    "role": null,
    "key": "model",
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "value": {
        "model": "test-model"
      },
      "provider": "demo",
      "profile_id": "general"
    },
    "scope": "session"
  },
  {
    "session_id": "openai-error-category",
    "seq": 2,
    "record_id": "sampling",
    "operation_id": "op-sampling",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "sampling",
    "category": "setting",
    "role": null,
    "key": "sampling",
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "value": {
        "temperature": 0.2
      }
    },
    "scope": "session"
  },
  {
    "session_id": "openai-error-category",
    "seq": 3,
    "record_id": "system",
    "operation_id": "op-system",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "system_prompt_added",
    "category": "prompt",
    "role": "system",
    "key": "core",
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "content": [
        {
          "kind": "text",
          "text": "You are a helpful assistant.\r\n"
        }
      ],
      "source_uri": null
    },
    "scope": "session"
  },
  {
    "session_id": "openai-error-category",
    "seq": 4,
    "record_id": "mixed-user",
    "operation_id": "op-mixed-user",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "user_message",
    "category": "unknown",
    "role": "user",
    "key": null,
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "content": [
        {
          "kind": "text",
          "text": "Read this:\n"
        },
        {
          "kind": "resource",
          "name": "source.py",
          "uri": "file:///workspace/source.py",
          "media_type": "text/plain",
          "content": [
            {
              "kind": "text",
              "text": "line 1\r\nline 2\n"
            }
          ],
          "line_start": null,
          "line_end": null,
          "truncated": false
        },
        {
          "kind": "image",
          "media_type": "image/png",
          "data": "aGVsbG8="
        }
      ]
    },
    "scope": "session"
  },
  {
    "session_id": "openai-error-parameter",
    "seq": 1,
    "record_id": "model",
    "operation_id": "op-model",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "model",
    "category": "setting",
    "role": null,
    "key": "model",
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "value": {
        "model": "test-model"
      },
      "provider": "demo",
      "profile_id": "general"
    },
    "scope": "session"
  },
  {
    "session_id": "openai-error-parameter",
    "seq": 2,
    "record_id": "sampling",
    "operation_id": "op-sampling",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "sampling",
    "category": "setting",
    "role": null,
    "key": "sampling",
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "value": {
        "temperature": 0.2
      }
    },
    "scope": "session"
  },
  {
    "session_id": "openai-error-parameter",
    "seq": 3,
    "record_id": "system",
    "operation_id": "op-system",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "system_prompt_added",
    "category": "prompt",
    "role": "system",
    "key": "core",
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "content": [
        {
          "kind": "text",
          "text": "You are a helpful assistant.\r\n"
        }
      ],
      "source_uri": null
    },
    "scope": "session"
  },
  {
    "session_id": "openai-error-parameter",
    "seq": 4,
    "record_id": "mixed-user",
    "operation_id": "op-mixed-user",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "user_message",
    "category": "message",
    "role": "user",
    "key": null,
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "content": [
        {
          "kind": "text",
          "text": "Read this:\n"
        },
        {
          "kind": "resource",
          "name": "source.py",
          "uri": "file:///workspace/source.py",
          "media_type": "text/plain",
          "content": [
            {
              "kind": "text",
              "text": "line 1\r\nline 2\n"
            }
          ],
          "line_start": null,
          "line_end": null,
          "truncated": false
        },
        {
          "kind": "image",
          "media_type": "image/png",
          "data": "aGVsbG8="
        }
      ]
    },
    "scope": "session"
  },
  {
    "session_id": "openai-error-parameter",
    "seq": 5,
    "record_id": "duplicate-temperature",
    "operation_id": "op-duplicate-temperature",
    "operation_position": 0,
    "parent_operation_id": null,
    "kind": "provider_parameters",
    "category": "setting",
    "role": null,
    "key": "provider_parameters",
    "operation": "set",
    "copied_from_id": null,
    "created_at": "2026-11-16T09:00:00Z",
    "version": "target-demo",
    "payload": {
      "value": {
        "temperature": 0.9
      }
    },
    "scope": "session"
  }
]
$records$::jsonb) r;
commit;
